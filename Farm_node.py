import machine, utime, ujson, dht

# ------------------------------------------------------------------ CONFIG ---
MY_ADDRESS       = 2
DEST_ADDRESS     = 1
NETWORK_ID       = 5
LORA_BAND        = 865000000
SEND_INTERVAL_MS = 5000

RELAY_COOLDOWN_MS = 4000

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
relay1 = machine.Pin(16, machine.Pin.OUT, value=1)  # Motor ON
relay2 = machine.Pin(17, machine.Pin.OUT, value=1)  # Motor OFF

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

# -------------------------------------------- RELAY DEBOUNCE TIMESTAMPS ---

_last_relay_time = {
    "RELAY1": 0,
    "RELAY2": 0,
}

# --------------------------------------------------------- LORA HELPERS ---
def cmd(c, wait=800):
    """Send AT command — used for init and TX only. Flushes UART first."""
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
    """Read sensors, update latest_data, transmit JSON over LoRa."""
    global latest_data
    temp, hum = read_sensor()

    # Replace with real ADC reads if available
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
        # "m" intentionally NOT overwritten — preserves relay state
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
    """
    Send immediate JSON after relay fires (or on duplicate command).
    Does NOT re-read DHT to avoid 1-2s sensor delay.
    Uses current T/H already in latest_data.
    """
    payload  = ujson.dumps(latest_data, separators=(',', ':'))
    length   = len(payload)
    if length > 240:
        return
    command  = "AT+SEND={},{},{}".format(DEST_ADDRESS, length, payload)
    print("  LoRa ACK TX ({} B): {}".format(length, payload))
    response = cmd(command, 2500)
    print("  ACK Response:", response)

# --------------------------------------------------------- RELAY CONTROL ---
def pulse_relay(relay_pin):
    """Active-LOW pulse: pull LOW for 1s then release HIGH."""
    relay_pin.value(0)    # ON
    utime.sleep_ms(1000)
    relay_pin.value(1)    # OFF

# -------------------------------------------- RELAY DEBOUNCE HELPER ---
def relay_in_cooldown(command):

    last = _last_relay_time.get(command, 0)
    if last == 0:
        return False  # never executed before — allow
    elapsed = utime.ticks_diff(utime.ticks_ms(), last)
    return elapsed < RELAY_COOLDOWN_MS

def mark_relay_executed(command):
    """Record the current timestamp as the last execution time for command."""
    _last_relay_time[command] = utime.ticks_ms()

# ------------------------------------------------------------ RX BUFFER ---
_rx_buf = b""

def check_rx():

    global _rx_buf, latest_data

    # Drain whatever arrived since last tick (non-blocking)
    while uart.any():
        chunk = uart.read(64)
        if chunk:
            _rx_buf += chunk

    # Process complete lines only
    while b"\n" in _rx_buf:
        line, _rx_buf = _rx_buf.split(b"\n", 1)
        line = line.strip().decode("utf-8", "ignore")

        if "+RCV=" not in line:
            continue

        print("[LoRa RX]", line)

        # Format: +RCV=<addr>,<len>,<payload>,<rssi>,<snr>
        # Example: +RCV=1,6,RELAY1,-60,8
        try:
            body  = line[len("+RCV="):]       # strip +RCV= prefix
            parts = body.split(",", 4)
            # parts[0]=addr  parts[1]=len  parts[2]=payload  [3]=rssi  [4]=snr
            if len(parts) < 3:
                print("  Too few fields:", parts)
                continue

            payload = parts[2].strip()
            print("  Payload:", payload)

            if payload == "RELAY1":
                if relay_in_cooldown("RELAY1"):

                    elapsed = utime.ticks_diff(
                        utime.ticks_ms(), _last_relay_time["RELAY1"]
                    )
                    print("  -> RELAY1 duplicate ({}ms ago) — skipping pulse, resending ACK".format(elapsed))
                    utime.sleep_ms(300)
                    send_lora_ack()
                else:
                    print("  -> Motor ON (1s pulse)")
                    pulse_relay(relay1)
                    latest_data["m"] = 1
                    mark_relay_executed("RELAY1")
                    blink_led(3, 100)
                    utime.sleep_ms(300)
                    send_lora_ack()

            elif payload == "RELAY2":
                if relay_in_cooldown("RELAY2"):
                    elapsed = utime.ticks_diff(
                        utime.ticks_ms(), _last_relay_time["RELAY2"]
                    )
                    print("  -> RELAY2 duplicate ({}ms ago) — skipping pulse, resending ACK".format(elapsed))
                    utime.sleep_ms(300)
                    send_lora_ack()
                else:
                    print("  -> Motor OFF (1s pulse)")
                    pulse_relay(relay2)
                    latest_data["m"] = 0
                    mark_relay_executed("RELAY2")
                    blink_led(3, 100)
                    utime.sleep_ms(300)
                    send_lora_ack()

        except Exception as e:
            print("  RX parse error:", e, "| line:", line)

# -------------------------------------------------------------- MAIN ---
lora_init()
last_send = utime.ticks_ms()

while True:
    now = utime.ticks_ms()

    # Periodic sensor send first (cmd() flushes UART — do before check_rx)
    if utime.ticks_diff(now, last_send) >= SEND_INTERVAL_MS:
        send_lora()
        last_send = utime.ticks_ms()

    # Passive relay listener (never blocks, never flushes UART)
    check_rx()

    utime.sleep_ms(50)