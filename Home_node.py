import machine, network, socket, utime, ujson

# ------------------------------------------------------------------ CONFIG ---
MY_ADDRESS = 1
NETWORK_ID = 5
LORA_BAND  = 865000000

WIFI_SSID     = "Airtel_sidd_7427"
WIFI_PASSWORD = "Air@81008"

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
def process_lora():
    global latest_data
    if not uart.any():
        return
    utime.sleep_ms(250)
    raw = b""
    while uart.any():
        raw += uart.read()
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
            data["rssi"] = rssi_str.strip()
            data["snr"]  = snr_str.strip()
            latest_data  = data
            print("  Phases:", data.get("p"))
            print("  Temp:", data.get("T"), "C  Hum:", data.get("H"), "%")
            print("  Motor:", data.get("m"))
            blink_led(2, 150)
        except Exception as e:
            print("JSON error:", e, "| raw payload:", payload)

# ------------------------------------------------------------ WIFI INIT ---
def wifi_connect(retries=3):
    wlan = network.WLAN(network.STA_IF)

    for attempt in range(1, retries + 1):
        print(f"Wi-Fi attempt {attempt}/{retries}")

        # Full reset of the interface
        wlan.active(False)
        utime.sleep_ms(1000)          # Give radio time to fully power down
        wlan.active(True)
        utime.sleep_ms(1000)          # Give radio time to fully initialize

        print("Connecting to:", WIFI_SSID)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)

        timeout = 40                  # 40 × 500ms = 20 seconds
        while not wlan.isconnected() and timeout > 0:
            status = wlan.status()
            print("Waiting... status:", status)

            # Bail early on unrecoverable errors
            # network.STAT_WRONG_PASSWORD = 202, STAT_NO_AP_FOUND = 201
            if status in (202, 201, -1):
                print("Unrecoverable status, retrying...")
                break

            utime.sleep_ms(500)
            timeout -= 1

        if wlan.isconnected():
            ip = wlan.ifconfig()[0]
            print("Connected! IP:", ip)
            return ip

        print("Attempt failed. Status:", wlan.status())
        wlan.disconnect()
        utime.sleep_ms(500)

    print("All attempts failed.")
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

        # ---------- /status — motor state only ----------
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
                "Connection: close\r\n\r\npong"
            )

        # ---------- /relay ----------
        elif path.startswith("/relay"):
            params = parse_query(path)
            try:
                relay_id = int(params.get("id", "0"))
                state    = int(params.get("state", "1"))
            except ValueError:
                relay_id, state = 0, 1

            payload = ""
            if relay_id == 1 and state == 0:
                payload = "RELAY1"
            elif relay_id == 2 and state == 0:
                payload = "RELAY2"

            if payload:
                length        = len(payload)
                command       = "AT+SEND=2,{},{}".format(length, payload)
                print("LoRa TX Relay:", payload)
                response_lora = cmd(command, 2000)
                print("Response:", response_lora)
                blink_led(2, 150)

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain\r\n"
                "Connection: close\r\n\r\nOK"
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
    s.listen(3)
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
