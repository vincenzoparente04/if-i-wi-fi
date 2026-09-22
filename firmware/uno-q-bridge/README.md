# uno-q-bridge (STM32U585 sketch)

The microcontroller half of the Arduino UNO Q. It registers RPC methods that
the Linux side calls through `arduino-router`, so Python can switch a relay
the moment presence is detected.

| RPC | Returns | Used by |
| --- | --- | --- |
| `ping()` | `true` | `rssi/bridge_client.py --ping` |
| `get_sensors()` | `"ts,temp,hum,air,light"` | `rssi/decision_engine.py` (optional sensor fusion) |
| `set_relay(bool)` | `bool` | `rssi/decision_engine.py` on presence changes |
| `set_led(bool)` | `bool` | status feedback |
| `test_uart()` | `"LOOPBACK_OK"` / `"ESP32_OK"` / `"FAIL:*"` | wiring checks |

## Wiring

| Pin | Connected to |
| --- | --- |
| D2 | DHT22 data |
| A0 | MQ135 analog out |
| A1 | LDR (10 kΩ divider) |
| D3 | relay IN |
| D4 | status LED |

The sensors are optional: without them `get_sensors()` returns zeros for temperature and humidity and floating analog readings.

## Flash

1. Arduino IDE → Board: **Arduino UNO Q**.
2. Library Manager → install **Arduino_RouterBridge** (the core ships a stub
   that fails otherwise) and **DHT sensor library**.
3. Upload `uno-q-bridge.ino`. One long blink of `LED_BUILTIN` means the bridge
   is up and every method is registered.
4. From the Linux side: `python3 -m rssi.bridge_client --ping`.
