#---------- Farm Node (Sender) --------------
import machine
import utime
import ujson
import dht

# ---------------- CONFIG ----------------
MY_ADDRESS   = 2
DEST_ADDRESS = 1
NETWORK_ID   = 5
LORA_BAND    = 865000000

# ---------------- UART ----------------
uart = machine.UART(
    0,
    baudrate=115200,
    tx=machine.Pin(0),
    rx=machine.Pin(1)
)

# ---------------- DHT11 ----------------
sensor = dht.DHT11(machine.Pin(15))

# ---------------- FUNCTIONS ----------------
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
    print("Starting LoRa...")
    utime.sleep_ms(2000)
    print(cmd("AT"))
    print(cmd("AT+RESET", 2000))
    print(cmd("AT+ADDRESS={}".format(MY_ADDRESS)))
    print(cmd("AT+NETWORKID={}".format(NETWORK_ID)))
    print(cmd("AT+BAND={}".format(LORA_BAND)))
    print(cmd("AT+PARAMETER=9,7,1,12"))  # SF9, BW125kHz, CR4/5, PL=12
    print(cmd("AT+CRFOP=22"))            # Max RF power
    print("\nLoRa Ready\n")

# ---------------- LED ----------------
led = machine.Pin(25, machine.Pin.OUT)

def blink_led(times=3, delay=200):
    for _ in range(times):
        led.value(1)
        utime.sleep_ms(delay)
        led.value(0)
        utime.sleep_ms(delay)

def send_json():
    try:
        sensor.measure()
        temp = sensor.temperature()
        hum  = sensor.humidity()
    except Exception as e:
        print("DHT Error:", e)
        temp = 0
        hum  = 0

    # Dummy phase values
    phases = [
        {"a": 10.5, "v": 435.6},
        {"a": 10.2, "v": 437.5},
        {"a": 10.8, "v": 438.7}
    ]

    # Compact JSON schema (no "i", keep "m")
    payload = ujson.dumps({
        "t": utime.time(),
        "p": phases,
        "T": temp,
        "H": hum,
        "m": 1
    }, separators=(',', ':'))

    length = len(payload)
    if length > 240:
        print("Payload too large:", length)
        return

    command = "AT+SEND={},{},{}".format(DEST_ADDRESS, length, payload)

    print("\nSending ({} bytes):".format(length))
    print(payload)

    response = cmd(command, 2500)  # longer wait for send
    print("Response:", response)

    # Blink LED 3 times after sending
    blink_led(3, 200)

# ---------------- MAIN ----------------
lora_init()
while True:
    send_json()
    utime.sleep(5)  # send every 5 seconds

