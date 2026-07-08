import machine, network, socket, utime, ujson

# ------------------------------------------------------------------ CONFIG ---
MY_ADDRESS    = 1
FARM_ADDRESS  = 2
NETWORK_ID    = 5
LORA_BAND     = 865000000

WIFI_SSID     = "Airtel_sidd_7427"
WIFI_PASSWORD = "Air@81008"

# ---- NEW: async resend tuning — replaces RELAY_CONFIRM_TIMEOUT_MS/RETRIES ----
# These no longer block anything. They just control how often an unconfirmed
# command gets re-transmitted in the background, and for how long, before
# we stop auto-resending (the command stays "PENDING" forever after that —
# never "FAILED" — the user can manually trigger a resend of the same ID).
ACK_WAIT_BEFORE_RESEND_MS = 1500
MAX_AUTO_RESENDS          = 4

# ------------------------------------------------------------------- UART ---
uart = machine.UART(
    0,
    baudrate=115200,
    tx=machine.Pin(0),
    rx=machine.Pin(1)
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
latest_data = {
    "t": 0,
    "p": [
        {"a": 0.0, "v": 0.0},
        {"a": 0.0, "v": 0.0},
        {"a": 0.0, "v": 0.0}
    ],
    "T": 0,
    "H": 0,
    "m": 0,
    "rssi": 0,
    "snr":  0
}

# ----------------------------------------------- NEW: COMMAND TRACKING ---
# Replaces _relay_in_progress. Tracks exactly one in-flight relay command
# by its unique id. The farm node is idempotent on this id (won't re-fire
# the relay for a repeat), so resends — automatic or user-triggered — are
# always safe, regardless of how long they take to resolve.
next_cmd_id = 1
pending_cmd = None   # dict: {id, action, confirmed, status, last_sent, resends}

# --------------------------------------------------------- LORA HELPERS ---
def cmd(c, wait=800):
    """AT command helper for INIT ONLY. Flushes RX first — safe at boot
    since nothing meaningful is buffered yet. Do not use this for sending
    relay commands (see send_relay_command below)."""
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

# --------------------------------------------------- NEW: COMMAND SEND ---
def send_relay_command(action, cmd_id):
    """
    Fire-and-forget send of CMD,<id>,<action>. Deliberately does NOT use
    cmd() — no RX flush — so we never throw away a concurrently-arriving
    ACK or telemetry packet just because we're issuing a relay command.
    Does not wait for or check the module's own local +OK/+ERR; that's
    fine, because correctness depends only on the farm node's idempotent
    handling of cmd_id, not on confirming this local TX succeeded.
    """
    payload = "CMD,{},{}".format(cmd_id, action)
    line = "AT+SEND={},{},{}\r\n".format(FARM_ADDRESS, len(payload), payload)
    print("LoRa TX: {}".format(payload))
    uart.write(line.encode())

# ----------------------------------------------------------- LORA RX ---
_new_packet_received = False   # freshness flag for telemetry display only

def process_lora():
    """
    Drains UART, handles two kinds of +RCV= payloads:
      - JSON telemetry (starts with '{')   -> updates latest_data
      - "ACK,<id>,<state>,<status>"        -> resolves pending_cmd by id
    """
    global latest_data, _new_packet_received, pending_cmd

    if not uart.any():
        return

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
        try:
            header, length, rest = line.split(",", 2)
            payload, rssi_str, snr_str = rest.rsplit(",", 2)
        except ValueError:
            print("Parse error:", line)
            continue

        payload = payload.strip()

        # ---------------- ACK,<id>,<state>,<status> ----------------
        if payload.startswith("ACK,"):
            try:
                parts = payload.split(",")
                ack_id     = int(parts[1])
                ack_state  = int(parts[2])
                ack_status = parts[3]
            except (IndexError, ValueError):
                print("  ACK parse error:", payload)
                continue

            print("  ACK id={} state={} status={}".format(
                ack_id, ack_state, ack_status))

            # Motor state from an ACK is always trustworthy, even if it's
            # not the id we're currently tracking (e.g. a very late ACK
            # for a superseded command) — reflect it either way.
            latest_data["m"] = ack_state

            if pending_cmd and ack_id == pending_cmd["id"]:
                pending_cmd["confirmed"] = True
                pending_cmd["status"]    = ack_status
                print("  -> Pending command {} resolved ({})".format(
                    ack_id, ack_status))
            else:
                print("  -> ACK id does not match current pending (or none pending) — state updated only")

            blink_led(2, 150)
            continue

        # ---------------- JSON telemetry ----------------
        try:
            data = ujson.loads(payload)
            data["rssi"] = int(rssi_str.strip())
            data["snr"]  = int(snr_str.strip())
            latest_data  = data
            _new_packet_received = True
            print("  Phases:", data.get("p"))
            print("  Temp:", data.get("T"), "C  Hum:", data.get("H"), "%")
            print("  Motor:", data.get("m"))
            blink_led(2, 150)
        except Exception as e:
            print("JSON error:", e, "| raw payload:", payload)

# -------------------------------------------- NEW: BACKGROUND RESEND ---
def check_pending_resend():
    """
    Called every main-loop tick. If a command is still unconfirmed and
    enough time has passed since the last send, resend the SAME id —
    never a new one — so the farm node's idempotency guard makes this
    completely safe no matter how many times it fires.
    After MAX_AUTO_RESENDS, stop resending automatically; the command
    stays PENDING (not FAILED) until either a late ACK resolves it or
    the user triggers a manual resend via another /relay press.
    """
    global pending_cmd

    if not pending_cmd or pending_cmd["confirmed"]:
        return

    now = utime.ticks_ms()
    if utime.ticks_diff(now, pending_cmd["last_sent"]) < ACK_WAIT_BEFORE_RESEND_MS:
        return

    if pending_cmd["resends"] >= MAX_AUTO_RESENDS:
        return   # stays PENDING — no further auto-action

    pending_cmd["resends"] += 1
    print("Auto-resend #{} for cmd id={}".format(
        pending_cmd["resends"], pending_cmd["id"]))
    send_relay_command(pending_cmd["action"], pending_cmd["id"])
    pending_cmd["last_sent"] = now

# ------------------------------------------------------------ WIFI INIT ---
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

        # ---------- /data ----------
        if path.startswith("/data"):
            body = ujson.dumps(latest_data)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Connection: close\r\n\r\n"
            ) + body

        # ---------- /status — NOW also reports pending command state ----------
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

        # ---------- /ping ----------
        elif path.startswith("/ping"):
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain\r\n"
                "Connection: close\r\n\r\npong"
            )

        # ---------- /relay — NOW non-blocking, returns instantly ----------
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
                # Same action already in flight — this is the user pressing
                # the same button again. Do NOT allocate a new id (that
                # would just be a second duplicate to track). Instead,
                # nudge an immediate resend of the SAME id — completely
                # safe thanks to farm-node idempotency, and satisfies
                # "prevent button spamming" without rejecting the press.
                print("Action already pending (id={}) — forcing resend, no new id".format(
                    pending_cmd["id"]))
                send_relay_command(pending_cmd["action"], pending_cmd["id"])
                pending_cmd["last_sent"] = utime.ticks_ms()
                body = ujson.dumps({"status": "PENDING", "cmd_id": pending_cmd["id"]})
            else:
                cmd_id = next_cmd_id
                next_cmd_id = (next_cmd_id + 1) % 65536
                pending_cmd = {
                    "id": cmd_id,
                    "action": action,
                    "confirmed": False,
                    "status": None,
                    "last_sent": utime.ticks_ms(),
                    "resends": 0
                }
                send_relay_command(action, cmd_id)
                body = ujson.dumps({"status": "PENDING", "cmd_id": cmd_id})

            # Returns IMMEDIATELY — no 8s/28s wait, no blocking the socket.
            # The app is expected to poll /status?... using cmd_id to learn
            # when it resolves, however long that actually takes.
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
    process_lora()
    check_pending_resend()      # NEW — background resend, never blocks
    if server_sock:
        try:
            conn, addr = server_sock.accept()
            conn.setblocking(True)
            handle_client(conn)
        except OSError:
            pass
    utime.sleep_ms(10)