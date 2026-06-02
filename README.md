# 🌾 ParvaLink — Smart Farm IoT System

> Long-range wireless farm motor control and 3-phase power monitoring using Raspberry Pi Pico W + LoRa + MIT App Inventor

![MicroPython](https://img.shields.io/badge/MicroPython-v1.23-blue?logo=python)
![LoRa](https://img.shields.io/badge/LoRa-RYLR998-green)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%20Pico-red)
![MIT App Inventor](https://img.shields.io/badge/App-MIT%20App%20Inventor-orange)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 📖 Overview

**ParvaLink** is a low-cost, long-range IoT system designed for Indian farmers to remotely control irrigation pump motors and monitor 3-phase electrical parameters — without requiring internet at the farm location.

The system uses **LoRa radio** (865 MHz) to communicate up to **3–5 km** between a home base station (Pico W) and a farm field unit (Pico H). An Android app built with MIT App Inventor provides the user interface.

---

## ✨ Features

- 📡 **Long-range LoRa communication** — works up to 5 km, no internet needed at farm
- ⚡ **3-phase power monitoring** — R, Y, B phase Amps and Volts in real time
- 🌡️ **Temperature and humidity monitoring** — DHT11 sensor at farm
- 🔁 **Remote motor START/STOP** — 1-second relay pulse from phone
- ✅ **Motor status confirmation** — app shows ON/OFF within 1–2 seconds
- 🔄 **Auto-retry on packet loss** — up to 3 automatic retries, no manual pressing
- 🛡️ **Deduplication protection** — prevents accidental double relay fire
- 📱 **Android app** — built with MIT App Inventor, no coding required to use
- 💾 **IP address memory** — saved via TinyDB, auto-loads on app start

---

## 🏗️ System Architecture

```
┌─────────────────────┐         LoRa 865 MHz          ┌─────────────────────┐
│     HOME NODE       │ ◄────────────────────────────► │     FARM NODE       │
│   Raspberry Pi      │        3-5 km range            │   Raspberry Pi      │
│     Pico W          │                                 │     Pico H          │
│                     │                                 │                     │
│  • Wi-Fi HTTP server│                                 │  • DHT11 sensor     │
│  • LoRa receiver    │                                 │  • 3-phase monitor  │
│  • Relay commander  │                                 │  • Relay1 (START)   │
│  • Auto-retry logic │                                 │  • Relay2 (STOP)    │
└────────┬────────────┘                                 └─────────────────────┘
         │ Wi-Fi (HTTP)
         │ 192.168.1.x
┌────────▼────────────┐
│   ANDROID APP       │
│  MIT App Inventor   │
│                     │
│  • Live sensor data │
│  • START / STOP     │
│  • Motor status     │
│  • IP address setup │
└─────────────────────┘
```

---

## 🔧 Hardware Required

| Component | Quantity | Purpose |
|-----------|----------|---------|
| Raspberry Pi Pico W | 1 | Home node — Wi-Fi + LoRa bridge |
| Raspberry Pi Pico H | 1 | Farm node — sensors + relay control |
| RYLR998 LoRa Module | 2 | Long-range radio communication |
| DHT11 Sensor | 1 | Temperature and humidity at farm |
| 2-Channel Relay Module | 1 | Motor START and STOP contactors |
| Android Phone | 1 | MIT App Inventor app |

---

## 📌 Pin Connections

### Farm Node (Pico H)

| Pin | Connection |
|-----|-----------|
| GP0 (TX) | RYLR998 RX |
| GP1 (RX) | RYLR998 TX |
| GP15 | DHT11 Data |
| GP16 | Relay1 — Motor START |
| GP17 | Relay2 — Motor STOP |
| 3.3V | RYLR998 VCC, DHT11 VCC |
| GND | Common Ground |

### Home Node (Pico W)

| Pin | Connection |
|-----|-----------|
| GP0 (TX) | RYLR998 RX |
| GP1 (RX) | RYLR998 TX |
| 3.3V | RYLR998 VCC |
| GND | Common Ground |

---

## 📡 LoRa Configuration

| Parameter | Value |
|-----------|-------|
| Band | 865 MHz (India) |
| Network ID | 5 |
| Home Node Address | 1 |
| Farm Node Address | 2 |
| Spreading Factor | SF9 |
| Bandwidth | 125 kHz |
| Coding Rate | 4/5 |
| TX Power | 22 dBm |

---

## 📦 JSON Data Packet

The farm node sends this JSON every 5 seconds:

```json
{
  "t": 1609459521,
  "T": 34,
  "H": 53,
  "m": 1,
  "p": [
    {"a": 10.5, "v": 435.6},
    {"a": 10.2, "v": 437.5},
    {"a": 10.8, "v": 438.7}
  ]
}
```

| Key | Meaning |
|-----|---------|
| `t` | Unix timestamp |
| `T` | Temperature (°C) |
| `H` | Humidity (%) |
| `m` | Motor status — 1=ON, 0=OFF |
| `p` | Phase array — R, Y, B phases |
| `p[n].a` | Phase current (Amps) |
| `p[n].v` | Phase voltage (Volts) |

---

## 🌐 HTTP API Endpoints

The Home Node runs a simple HTTP server on port 80:

| Endpoint | Method | Response | Purpose |
|----------|--------|----------|---------|
| `/data` | GET | Full JSON | All sensor data |
| `/status` | GET | `{"m":0}` or `{"m":1}` | Motor state only |
| `/relay?id=1&state=0` | GET | `OK` or `RETRY` | Start motor |
| `/relay?id=2&state=0` | GET | `OK` or `RETRY` | Stop motor |
| `/ping` | GET | `pong` | Connectivity check |

---

## 🔄 Relay Reliability Logic

The system uses a **confirm-and-retry** approach to handle LoRa packet loss:

```
App presses START
        ↓
Home sends RELAY1 (attempt 1)
        ↓
Wait up to 4s for farm ACK (m=1 in JSON)
        ↓
    ┌───┴───┐
  ACK     No ACK
received  in 4s
    │         │
    ↓         ↓
CONFIRMED  Wait 1s → retry (attempt 2)
  m=1              │
                   ↓
             Wait 4s for ACK
                   │
              ┌────┴────┐
            ACK       No ACK
              │           │
              ↓           ↓
         CONFIRMED    Retry (attempt 3)
                           │
                      Final attempt
                           │
                    ┌──────┴──────┐
                  ACK           No ACK
                    │               │
                    ↓               ↓
               CONFIRMED      Return RETRY
                               to app
```

Farm node has **8-second deduplication** — even if home sends multiple RELAY1 commands, the relay fires only once.

---

## 📱 App Features

- Live display of R, Y, B phase Amps and Volts
- Temperature and humidity display
- Motor status label — ON ✓ (green) / OFF ✗ (red) / ⚠ No Signal (yellow)
- START and STOP relay buttons
- IP address setup with TinyDB memory
- Automatic data polling every 2 seconds via Clock timer
- Separate Web components for data polling (Web1) and relay commands (Web2)

---

## 📁 Project Structure

```
ParvaLink/
├── farm_node_reliable.py    # Farm node — sensor read, relay control, LoRa TX/RX
├── home_node_final.py       # Home node — Wi-Fi HTTP server, relay confirm+retry
└── README.md                # This file
```

---

## 🚀 Setup Instructions

### 1. Flash MicroPython

Flash MicroPython firmware to both Pico W and Pico H from [micropython.org](https://micropython.org/download/)

### 2. Configure Wi-Fi

In `home_node_final.py` update your Wi-Fi credentials:

```python
WIFI_SSID     = "your_wifi_name"
WIFI_PASSWORD = "your_wifi_password"
```

### 3. Flash the Code

- Flash `farm_node_reliable.py` as `main.py` on **Pico H**
- Flash `home_node_final.py` as `main.py` on **Pico W**

### 4. Install the App

- Download the `.apk` from the releases section
- Or open the `.aia` file in [MIT App Inventor](https://ai2.appinventor.mit.edu) and build it yourself

### 5. Set IP Address

- Power on both Picos
- Check serial monitor for the Pico W IP address (e.g. `192.168.1.8`)
- Open the app → tap **Set IP Address** → enter the IP → tap **Confirm IP**

---

## ⚙️ Tuning Parameters

In `home_node_final.py`:

```python
RELAY_CONFIRM_TIMEOUT_MS = 4000   # Wait time per attempt (ms)
RELAY_MAX_RETRIES        = 2      # Number of retries after first attempt
RELAY_RETRY_DELAY_MS     = 1000   # Delay between retries (ms)
```

In `farm_node_reliable.py`:

```python
SEND_INTERVAL_MS  = 5000    # JSON send interval (ms)
RELAY_DEDUP_MS    = 8000    # Duplicate command rejection window (ms)
```

---

## 🐛 Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Relay not firing | LoRa packet loss | Auto-retry handles it — wait 10s |
| Motor status wrong | Delayed ACK | Wait for next 5s JSON packet |
| Error 1101 in app | Server busy during relay confirm | Normal — data updates after relay completes |
| No Signal in app | Pico W not reachable | Check Wi-Fi, check IP address in app |
| LoRa not initialising | Wiring issue | Check TX/RX pins, check 3.3V supply |
| DHT11 error | Sensor fault | Check GP15 connection, replace sensor |

---

## 📊 Performance

| Metric | Value |
|--------|-------|
| LoRa range (open field) | 3–5 km |
| Data update interval | 5 seconds |
| Relay response time (normal) | 1–2 seconds |
| Relay response time (retry) | 5–14 seconds |
| Motor status confirmation | Within 1–2 seconds of relay fire |
| Packet loss handling | Up to 3 automatic retries |

---

## 🔒 Relay Safety

- Active-LOW relay board — relay is OFF when pin is HIGH (safe default on boot)
- 1-second pulse only — relay does not stay latched by software
- Deduplication — 8-second window prevents accidental double fire
- Separate START and STOP relays — matches motor contactor wiring

---

## 📜 License

MIT License — free to use, modify, and distribute.

---

## 👤 Author

**Parvasense** — [parvasenseofficial@gmail.com](mailto:parvasenseofficial@gmail.com)

---

## 🙏 Acknowledgements

- [MicroPython](https://micropython.org/) — Python for microcontrollers
- [RYLR998 LoRa Module](https://reyax.com/) — Reyax Technology
- [MIT App Inventor](https://appinventor.mit.edu/) — MIT CSAIL
- [Raspberry Pi Foundation](https://www.raspberrypi.org/) — Pico W and Pico H

---

*Built for Indian farmers — affordable, reliable, no internet required at the farm.*
