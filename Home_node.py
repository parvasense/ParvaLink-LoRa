import machine, network, socket, utime, ujson

# ------------------------------------------------------------------ CONFIG ---
MY_ADDRESS = 1
NETWORK_ID = 5
LORA_BAND  = 865000000

WIFI_SSID     = "Airtel_sidd_7427"
WIFI_PASSWORD = "Air@81008"

# Relay confirmation settings
RELAY_CONFIRM_TIMEOUT_MS = 8000  # wait up to 8s for m to change
RELAY_MAX_RETRIES        = 2     # resend up to 2 more times if no confirm
RELAY_RETRY_DELAY_MS     = 2000  # wait 2s between retries

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

# ------------------------------------------------- RELAY BUSY LOCK ---
_relay_in_progress = False

# --------------------------------------------------------- LORA HELPERS ---
def cmd(c, wait=800):
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

# ----------------------------------------------------------- LORA RX ---
# Tracks whether a *new* packet has arrived since the last call.
_new_packet_received = False

def process_lora():

    global latest_data, _new_packet_received
    if not uart.any():
        return
    # FIX 1: No blocking sleep — read whatever is available right now
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
        try:
            data = ujson.loads(payload)
            # FIX 2: Cast to int so RSSI/SNR can be used in math
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

# ------------------------------------------------- RELAY WITH CONFIRM ---
def send_relay_with_confirm(payload, expected_m):

    global _new_packet_received

    # FIX 3: Reset at entry — discard any stale flag from main loop
    _new_packet_received = False

    length  = len(payload)
    command = "AT+SEND=2,{},{}".format(length, payload)

    for attempt in range(1, RELAY_MAX_RETRIES + 2):  # attempts: 1, 2, 3
        print("LoRa TX Relay: {} (attempt {}/{})".format(
            payload, attempt, RELAY_MAX_RETRIES + 1))

        resp = cmd(command, 2000)
        print("Response:", resp)

        if "+ERR" in resp:
            print("  TX error from module ({}), skipping wait — retrying".format(resp))
            utime.sleep_ms(500)
            continue

        if "+OK" not in resp:
            print("  TX rejected by module — skipping wait")
            utime.sleep_ms(500)
            continue

        blink_led(2, 150)

        # Reset flag AFTER successful TX so only post-TX packets confirm
        _new_packet_received = False
        m_before_tx = latest_data.get("m", -1)

        print("  Waiting for farm ACK (expect m={}, currently m={})...".format(
            expected_m, m_before_tx))

        deadline = utime.ticks_add(utime.ticks_ms(), RELAY_CONFIRM_TIMEOUT_MS)

        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            process_lora()
            if _new_packet_received and latest_data.get("m", -1) == expected_m:
                print("  CONFIRMED m={} after attempt {}".format(
                    expected_m, attempt))
                return True
            utime.sleep_ms(100)

        # Final check after timeout
        if _new_packet_received and latest_data.get("m", -1) == expected_m:
            print("  Confirmed on final check")
            return True

        if attempt <= RELAY_MAX_RETRIES:
            print("  Not confirmed — retrying in {}ms".format(
                RELAY_RETRY_DELAY_MS))
            utime.sleep_ms(RELAY_RETRY_DELAY_MS)

    print("  RELAY FAILED after {} attempts. m={} expected={}".format(
        RELAY_MAX_RETRIES + 1,
        latest_data.get("m", -1),
        expected_m
    ))
    return False

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

    global _relay_in_progress

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

        # ---------- /status ----------
        elif path.startswith("/status"):
            body = ujson.dumps({"m": latest_data.get("m", 0)})
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
                "Connection: close\r\n\rpong"
            )

        # ---------- /relay ----------
        elif path.startswith("/relay"):
            params = parse_query(path)
            try:
                relay_id = int(params.get("id", "0"))
                state    = int(params.get("state", "1"))
            except ValueError:
                relay_id, state = 0, 1

            # FIX 4: Reject duplicate while a relay cycle is in progress
            if _relay_in_progress:
                print("Relay busy — duplicate request rejected")
                response = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain\r\n"
                    "Connection: close\r\n\r\nBUSY"
                )
                conn.sendall(response.encode())
                conn.close()
                return

            _relay_in_progress = True
            confirmed = False

            try:
                if relay_id == 1 and state == 0:
                    # START — wait for m=1 confirmation
                    confirmed = send_relay_with_confirm("RELAY1", expected_m=1)

                elif relay_id == 2 and state == 0:
                    # STOP — wait for m=0 confirmation
                    confirmed = send_relay_with_confirm("RELAY2", expected_m=0)
            finally:
                # Always release lock even if an exception occurs
                _relay_in_progress = False

            result = "OK" if confirmed else "RETRY"
            print("Relay result sent to app:", result)

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain\r\n"
                "Connection: close\r\n\r\n" + result
            )

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
    # FIX 5: Increased listen backlog from 3 to 5.
    # With relay operations taking up to 24s, a backlog of 3 fills up
    # quickly and causes ECONNRESET on the app side.
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
    if server_sock:
        try:
            conn, addr = server_sock.accept()
            conn.setblocking(True)
            handle_client(conn)
        except OSError:
            pass
    utime.sleep_ms(10)