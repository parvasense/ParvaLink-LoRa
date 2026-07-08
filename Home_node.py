import machine, network, socket, utime, ujson

# ------------------------------------------------------------------ CONFIG ---
MY_ADDRESS    = 1       # this node's LoRa address
FARM_ADDRESS  = 2       # farm node's LoRa address
NETWORK_ID    = 5
LORA_BAND     = 865000000

WIFI_SSID     = "Airtel_sidd_7427"
WIFI_PASSWORD = "Air@81008"

# How long to wait before auto-resending an unconfirmed command
ACK_WAIT_BEFORE_RESEND_MS = 1500
# How many times to auto-resend before giving up (command stays PENDING, never FAILED)
MAX_AUTO_RESENDS          = 4

# ------------------------------------------------------------------- UART ---
uart = machine.UART(
    0,
    baudrate=115200,
    tx=machine.Pin(0),
    rx=machine.Pin(1),
    rxbuf=512    # large enough for full +RCV= lines without truncation
)

# -------------------------------------------------------------------- LED ---
led = machine.Pin("LED", machine.Pin.OUT)

def blink_led(times=2, delay_ms=150):
    for _ in range(times):
        led.value(1)
        utime.sleep_ms(delay_ms)
        led.value(0)
        utime.sleep_ms(delay_ms)

# ---------------------------------------------------- SHARED DATA STORE ---
# Holds the latest telemetry received from the farm node
latest_data = {
    "t": 0,
    "p": [
        {"a": 0.0, "v": 0.0},
        {"a": 0.0, "v": 0.0},
        {"a": 0.0, "v": 0.0}
    ],
    "T": 0,
    "H": 0,
    "m": 0,      # motor state: 1=ON, 0=OFF
    "rssi": 0,
    "snr":  0
}

# --------------------------------------------------- COMMAND TRACKING ---
# Each relay press gets a unique ID (1-65535, never 0).
# The farm node remembers the last ID it executed — duplicates are ignored.
# This means retries and button-spamming can never cause double relay fires.
next_cmd_id = 1
pending_cmd = None   # tracks the one in-flight command: {id, action, confirmed, status, last_sent, resends}

# --------------------------------------------------------- LORA HELPERS ---
def cmd(c, wait=800):
    # AT command helper — used ONLY during init.
    # Flushes RX first (safe at boot, nothing meaningful buffered yet).
    # Never use this for relay commands — it would discard arriving ACKs.
    while uart.any():
        uart.read()
    uart.write((c + "\r\n").encode())
    utime.sleep_ms(wait)
    r = b""
    while uart.any():
        chunk = uart.read()
        if chunk:
            r += chunk
    return r.decode('utf-8', 'ignore').strip()

def lora_init():
    print("Initialising LoRa (Home Node)...")
    utime.sleep_ms(2000)
    print(cmd("AT"))
    print(cmd("AT+RESET", 2000))
    print(cmd("AT+ADDRESS={}".format(MY_ADDRESS)))
    print(cmd("AT+NETWORKID={}".format(NETWORK_ID)))
    print(cmd("AT+BAND={}".format(LORA_BAND)))
    print(cmd("AT+PARAMETER=9,7,1,12"))
    print(cmd("AT+CRFOP=22"))
    print("Receiver Ready\n")

# --------------------------------------------------- RELAY COMMAND SEND ---
def send_relay_command(action, cmd_id):
    # Fire-and-forget: writes CMD,<id>,<action> directly to UART without
    # flushing RX first — so we never discard a concurrently-arriving ACK.
    # Correctness depends on the farm node's dedup logic, not on this TX succeeding.
    payload = "CMD,{},{}".format(cmd_id, action)
    line = "AT+SEND={},{},{}\r\n".format(FARM_ADDRESS, len(payload), payload)
    print("LoRa TX: {}".format(payload))
    uart.write(line.encode())

# ----------------------------------------------------------- LORA RX ---
def process_lora():
    # Reads all pending UART bytes and handles two payload types:
    #   - "ACK,<id>,<state>,<status>" -> resolves pending_cmd
    #   - JSON starting with '{'       -> updates latest_data telemetry
    global latest_data, pending_cmd

    if not uart.any():
        return

    # FIX: wait 20ms for the full line to arrive before reading.
    # At 115200 baud, a 40-byte line takes ~3ms — reading immediately
    # risks catching it mid-arrival and truncating the line.
    utime.sleep_ms(20)

    raw = b""
    while uart.any():
        chunk = uart.read()
        if chunk:
            raw += chunk
    if not raw:
        return

    text  = raw.decode('utf-8', 'ignore')
    lines = text.splitlines()

    for line in lines:
        line = line.strip()
        if "+RCV=" not in line:
            continue

        print("\n[LoRa RX]", line)

        # Split +RCV=<addr>,<len>,<payload>,<rssi>,<snr>
        try:
            header, length, rest = line.split(",", 2)
            payload, rssi_str, snr_str = rest.rsplit(",", 2)
        except ValueError:
            print("Parse error:", line)
            continue

        payload = payload.strip()

        # ---- ACK from farm node ----
        if payload.startswith("ACK,"):
            try:
                parts      = payload.split(",")
                ack_id     = int(parts[1])
                ack_state  = int(parts[2])
                ack_status = parts[3]
            except (IndexError, ValueError):
                print("  ACK parse error:", payload)
                continue

            print("  ACK id={} state={} status={}".format(ack_id, ack_state, ack_status))

            # Always update motor state from ACK — even if it's a late/stale one
            latest_data["m"] = ack_state

            if pending_cmd and ack_id == pending_cmd["id"]:
                # This ACK matches our in-flight command — resolve it
                pending_cmd["confirmed"] = True
                pending_cmd["status"]    = ack_status
                print("  -> Command {} resolved ({})".format(ack_id, ack_status))
            else:
                print("  -> ACK id mismatch or no pending command — motor state updated only")

            blink_led(2, 150)
            continue

        # ---- JSON telemetry from farm node ----
        try:
            data = ujson.loads(payload)
            data["rssi"] = int(rssi_str.strip())
            data["snr"]  = int(snr_str.strip())
            latest_data  = data
            print("  Phases:", data.get("p"))
            print("  Temp:", data.get("T"), "C  Hum:", data.get("H"), "%")
            print("  Motor:", data.get("m"))
            blink_led(2, 150)
        except Exception as e:
            print("JSON error:", e, "| raw payload:", payload)

# ------------------------------------------------- BACKGROUND RESEND ---
def check_pending_resend():
    # Called every main-loop tick.
    # If a command is unconfirmed and ACK_WAIT_BEFORE_RESEND_MS has passed,
    # resend the SAME cmd_id — farm node's dedup ignores true duplicates.
    # After MAX_AUTO_RESENDS, stops auto-resending but command stays PENDING.
    global pending_cmd

    if not pending_cmd or pending_cmd["confirmed"]:
        return

    now = utime.ticks_ms()
    if utime.ticks_diff(now, pending_cmd["last_sent"]) < ACK_WAIT_BEFORE_RESEND_MS:
        return

    if pending_cmd["resends"] >= MAX_AUTO_RESENDS:
        return   # stopped auto-resending — user can manually retry

    pending_cmd["resends"] += 1
    print("Auto-resend #{} for cmd id={}".format(pending_cmd["resends"], pending_cmd["id"]))
    send_relay_command(pending_cmd["action"], pending_cmd["id"])
    pending_cmd["last_sent"] = now

# ------------------------------------------------------------ WIFI ---
def wifi_connect(retries=3):
    wlan = network.WLAN(network.STA_IF)

    for attempt in range(1, retries + 1):
        print("Wi-Fi attempt {}/{}".format(attempt, retries))
        wlan.active(False)
        utime.sleep_ms(1000)
        wlan.active(True)
        utime.sleep_ms(1000)

        print("Connecting to:", WIFI_SSID)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)

        timeout = 40
        while not wlan.isconnected() and timeout > 0:
            status = wlan.status()
            print("Waiting... status:", status)
            if status in (202, 201, -1):
                print("Unrecoverable status, retrying...")
                break
            utime.sleep_ms(500)
            timeout -= 1

        if wlan.isconnected():
            ip = wlan.ifconfig()[0]
            print("Connected! IP:", ip)
            return ip

        print("Attempt {} failed. Status: {}".format(attempt, wlan.status()))
        wlan.disconnect()
        utime.sleep_ms(500)

    print("All Wi-Fi attempts failed.")
    return None

# ------------------------------------------------------- HTTP HANDLERS ---
def parse_query(path):
    # Extracts ?key=value pairs from a URL path
    params = {}
    if "?" in path:
        qs = path.split("?", 1)[1]
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = v
    return params

def handle_client(conn):
    global pending_cmd, next_cmd_id

    try:
        request = conn.recv(1024).decode("utf-8", "ignore")
        if not request:
            conn.close()
            return

        first_line = request.split("\r\n")[0]
        parts = first_line.split(" ")
        path  = parts[1] if len(parts) >= 2 else "/"

        # ---- /data — returns full telemetry JSON ----
        if path.startswith("/data"):
            body = ujson.dumps(latest_data)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Connection: close\r\n\r\n"
            ) + body

        # ---- /status — returns motor state + pending command info ----
        # App polls this after a /relay call to learn when the command resolves
        elif path.startswith("/status"):
            status_obj = {"m": latest_data.get("m", 0)}
            if pending_cmd:
                status_obj["pending_id"]     = pending_cmd["id"]
                status_obj["pending_action"] = pending_cmd["action"]
                status_obj["confirmed"]      = pending_cmd["confirmed"]
                status_obj["ack_status"]     = pending_cmd["status"]
            body = ujson.dumps(status_obj)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Connection: close\r\n\r\n"
            ) + body

        # ---- /ping — simple alive check ----
        elif path.startswith("/ping"):
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain\r\n"
                "Connection: close\r\n\r\n"   # FIX: was \r\n\r (missing \n)
                "pong"
            )

        # ---- /relay?id=1 (START) or /relay?id=2 (STOP) ----
        # Returns INSTANTLY with {"status":"PENDING","cmd_id":N}
        # App must poll /status to learn when it resolves
        elif path.startswith("/relay"):
            params = parse_query(path)
            try:
                relay_id = int(params.get("id", "0"))
            except ValueError:
                relay_id = 0

            if relay_id == 1:
                action = "RELAY1"
            elif relay_id == 2:
                action = "RELAY2"
            else:
                response = (
                    "HTTP/1.1 400 Bad Request\r\n"
                    "Connection: close\r\n\r\nUnknown relay id"
                )
                conn.sendall(response.encode())
                conn.close()
                return

            if pending_cmd and not pending_cmd["confirmed"] and pending_cmd["action"] == action:
                # Same action already in flight — reuse same cmd_id, just nudge a resend.
                # Safe because farm node ignores duplicate IDs without re-pulsing.
                print("Same action already pending (id={}) — resending same id".format(
                    pending_cmd["id"]))
                send_relay_command(pending_cmd["action"], pending_cmd["id"])
                pending_cmd["last_sent"] = utime.ticks_ms()
                body = ujson.dumps({"status": "PENDING", "cmd_id": pending_cmd["id"]})
            else:
                # FIX: clear stale confirmed command before tracking a new one
                # so /status doesn't keep reporting the old confirmed command forever
                if pending_cmd and pending_cmd["confirmed"]:
                    pending_cmd = None

                # Allocate new command ID — wraps 65535 → 1, never produces 0
                # (0 is reserved as "invalid" on the farm node side)
                cmd_id      = next_cmd_id
                next_cmd_id = (next_cmd_id % 65535) + 1   # FIX: was % 65536, could produce 0

                pending_cmd = {
                    "id":        cmd_id,
                    "action":    action,
                    "confirmed": False,
                    "status":    None,
                    "last_sent": utime.ticks_ms(),
                    "resends":   0
                }
                send_relay_command(action, cmd_id)
                body = ujson.dumps({"status": "PENDING", "cmd_id": cmd_id})

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Connection: close\r\n\r\n"
            ) + body

        else:
            response = "HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\nNot Found"

        conn.sendall(response.encode())

    except Exception as e:
        print("HTTP error:", e)
    finally:
        conn.close()

def start_server():
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(5)
    s.setblocking(False)
    print("HTTP server listening on port 80")
    return s

# -------------------------------------------------------------- MAIN ---
lora_init()
pico_ip     = wifi_connect()
server_sock = start_server() if pico_ip else None

while True:
    process_lora()            # check for incoming ACKs and telemetry
    check_pending_resend()    # auto-resend unconfirmed commands in background
    if server_sock:
        try:
            conn, addr = server_sock.accept()
            conn.setblocking(True)
            handle_client(conn)
        except OSError:
            pass
    utime.sleep_ms(10)