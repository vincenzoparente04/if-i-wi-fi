# esp32-radar3d (cross-ping CSI firmware)

Three ESP32 take turns broadcasting at 100 Hz on a fixed channel, and each
one records the CSI of the frames it hears from the other two. The result is
**six cross-bistatic paths** `(tx, rx)` with stable MAC addresses, streamed
over UDP to the host (`csi.quadrants.ws_server`).

```
        ESP32-2
        /    \
       /      \
  ESP32-0 ——— ESP32-1        six paths: 0→1 0→2 1→0 1→2 2→0 2→1
```

## Setup

1. **Toolchain.** Arduino IDE 2.x with the ESP32 core (Boards Manager URL:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`).
   Board: *ESP32 Dev Module*.
2. **Credentials.** Copy `secrets.h.example` to `secrets.h` and set a
   **2.4 GHz** network. `secrets.h` is git-ignored.
3. **MAC addresses.** Flash any `NODE_ID` once on each board, then run
   `python3 tools/discover_macs.py` with one board plugged in at a time. It
   reads the `ESP32 MAC Address:` line printed at boot and writes it into
   `NODE_MACS` in `network_config.h`.
4. **Host IP.** Set `UDP_TARGET_IP` in `network_config.h` to the machine
   running the host pipeline.
5. **Flash all three.** Set `NODE_ID` to 0, 1 and 2 in turn and upload to each board.
6. **Place them** in a triangle, ~1 m above the floor, 2–5 m apart.

## Check it works

On the host:

```bash
python3 -m csi.quadrants.ws_server --udp-port 5005 --ws-port 8765 \
    --room 6x5 --grid 4x4 --rx "0.5,0.5;5.5,0.5;3.0,4.5" --enable-3d
```

The console should show `paths= 6/6`. On each board's serial monitor
(115200 baud) a stats line appears every 5 s:

```
[1] TX:500 CSI:1485 drop[tx0=.. tx1=.. tx2=.. self=0 unk=..] udp_fail:0
```

- `TX` ≈ 500 (5 s × 100 Hz). Much lower means the loop is blocked.
- `self` must stay at 0 (self-loops are filtered).
- `udp_fail` must stay at 0; otherwise check the target IP or the firewall.

## Robustness changes vs. the original team sketch

1. **Hard 100 Hz rate gate in the CSI callback.** On a busy channel the
   callback can fire hundreds of times per second and exhaust lwIP buffers.
2. **UDP back-off (100 ms)** when `endPacket()` fails, so one failure does
   not cascade.
3. **Brownout detector re-enabled.** Disabling it hid power problems; fix the
   power supply instead (see [HARDWARE.md](../../docs/HARDWARE.md)).
4. **Extended serial stats** (`rate_drop`, `udp_fail`).
5. **MAC printed at boot** for `tools/discover_macs.py`.

These changes were made after the team's last hardware session and have not
been re-verified on the boards.
