import machine, utime, ujson, dht

# ------------------------------------------------------------------ CONFIG ---
MY_ADDRESS       = 2
DEST_ADDRESS     = 1
NETWORK_ID       = 5
LORA_BAND        = 865000000
SEND_INTERVAL_MS = 5000

# ------------------------------------------------------------------- UART ---
uart = machine.UART(
    0,
    baudrate=115200,
    tx=machine.Pin(0),
    rx=machine.Pin(1)
)

# ------------------------------------------------------------------ DHT11 ---
sensor = dht.DHT11(machine.Pin(15))

# ----------------------------------------------------------------- RELAYS ---
# Active-LOW relay board: value(0) = ON, value(1) = OFF
relay1 = machine.Pin(16, machine.Pin.OUT, value=1)  # Motor ON pulse
relay2 = machine.Pin(17, machine.Pin.OUT, value=1)  # Motor OFF pulse

# -------------------------------------------------------------------- LED ---
led = machine.Pin("LED", machine.Pin.OUT)

def blink_led(times=3, delay_ms=200):
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
    "m": 0   # motor status: 1 = ON, 0 = OFF
}

# --------------------------------------------------------- LORA HELPERS ---
def cmd(c, wait=800):
    """AT command for init and periodic sensor TX only. Flushes UART first."""
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
    print("Initialising LoRa (Farm Node)...")
    utime.sleep_ms(2000)
    print(cmd("AT"))
    print(cmd("AT+RESET", 2000))
    print(cmd("AT+ADDRESS={}".format(MY_ADDRESS)))
    print(cmd("AT+NETWORKID={}".format(NETWORK_ID)))
    print(cmd("AT+BAND={}".format(LORA_BAND)))
    print(cmd("AT+PARAMETER=9,7,1,12"))
    print(cmd("AT+CRFOP=22"))
    print("LoRa Ready\n")

# --------------------------------------------------------- SENSOR + SEND ---
def read_sensor():
    try:
        sensor.measure()
        return sensor.temperature(), sensor.humidity()
    except Exception as e:
        print("DHT error:", e)
        return 0, 0

def send_lora():

    global latest_data, _rx_buf

    # Save any incoming bytes before cmd() flushes UART
    while uart.any():
        chunk = uart.read(64)
        if chunk:
            _rx_buf += chunk

    temp, hum = read_sensor()

    phases = [
        {"a": 10.5, "v": 435.6},
        {"a": 10.2, "v": 437.5},
        {"a": 10.8, "v": 438.7}
    ]

    latest_data.update({
        "t": utime.time(),
        "p": phases,
        "T": temp,
        "H": hum
    })

    payload  = ujson.dumps(latest_data, separators=(',', ':'))
    length   = len(payload)

    if length > 240:
        print("Payload too large:", length, "bytes — skipping")
        return

    command  = "AT+SEND={},{},{}".format(DEST_ADDRESS, length, payload)
    print("\nLoRa TX ({} B): {}".format(length, payload))
    response = cmd(command, 2500)
    print("Response:", response)
    blink_led(2, 150)

def send_lora_ack():

    global _rx_buf

    payload = ujson.dumps(latest_data, separators=(',', ':'))
    length  = len(payload)
    if length > 240:
        return

    command = "AT+SEND={},{},{}\r\n".format(DEST_ADDRESS, length, payload)
    print("  ACK TX ({} B): {}".format(length, payload))

    uart.write(command.encode())
    utime.sleep_ms(2000)

    resp = b""
    deadline = utime.ticks_add(utime.ticks_ms(), 500)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if uart.any():
            chunk = uart.read(64)
            if chunk:
                for line in chunk.split(b"\n"):
                    line_s = line.strip()
                    if b"+OK" in line_s or b"+ERR" in line_s:
                        resp += line_s
                    elif line_s:
                        _rx_buf += line_s + b"\n"
        utime.sleep_ms(10)

    print("  ACK Response:", resp.decode('utf-8', 'ignore').strip())

# --------------------------------------------------------- RELAY CONTROL ---
def pulse_relay(relay_pin):
    global _rx_buf
    relay_pin.value(0)  # ON
    deadline = utime.ticks_add(utime.ticks_ms(), 1000)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if uart.any():
            chunk = uart.read(64)
            if chunk:
                _rx_buf += chunk
        utime.sleep_ms(10)
    relay_pin.value(1)  # OFF

# ------------------------------------------------------------ RX BUFFER ---
_rx_buf = b""

def check_rx():
    global _rx_buf, latest_data

    while uart.any():
        chunk = uart.read(64)
        if chunk:
            _rx_buf += chunk

    while b"\n" in _rx_buf:
        line, _rx_buf = _rx_buf.split(b"\n", 1)
        line = line.strip().decode("utf-8", "ignore")

        if "+RCV=" not in line:
            continue

        print("[LoRa RX]", line)

        try:
            body  = line[len("+RCV="):]
            parts = body.split(",", 4)
            if len(parts) < 3:
                print("  Too few fields:", parts)
                continue

            payload = parts[2].strip()
            print("  Payload:", payload)

            # ----------------------------------------------------------
            # RELAY1 — turn motor ON
            # ----------------------------------------------------------
            if payload == "RELAY1":
                if latest_data["m"] == 1:
                    print("  -> RELAY1 ignored: motor already ON (m=1) — resending ACK")
                    utime.sleep_ms(100)
                    send_lora_ack()
                else:
                    # Motor is OFF — this is a genuine new ON command.
                    print("  -> Motor ON (1s pulse)")
                    pulse_relay(relay1)
                    latest_data["m"] = 1
                    blink_led(3, 100)
                    utime.sleep_ms(100)
                    send_lora_ack()

            # ----------------------------------------------------------
            # RELAY2 — turn motor OFF
            # ----------------------------------------------------------
            elif payload == "RELAY2":
                if latest_data["m"] == 0:
                    print("  -> RELAY2 ignored: motor already OFF (m=0) — resending ACK")
                    utime.sleep_ms(100)
                    send_lora_ack()
                else:
                    # Motor is ON — this is a genuine new OFF command.
                    print("  -> Motor OFF (1s pulse)")
                    pulse_relay(relay2)
                    latest_data["m"] = 0
                    blink_led(3, 100)
                    utime.sleep_ms(100)
                    send_lora_ack()

        except Exception as e:
            print("  RX parse error:", e, "| line:", line)

# -------------------------------------------------------------- MAIN ---
lora_init()
last_send = utime.ticks_ms()

while True:
    now = utime.ticks_ms()

    if utime.ticks_diff(now, last_send) >= SEND_INTERVAL_MS:
        send_lora()
        last_send = utime.ticks_ms()

    check_rx()

    utime.sleep_ms(50)