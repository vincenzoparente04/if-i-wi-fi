# Firmware

| Folder | Board | Toolchain | Role | Status |
| --- | --- | --- | --- | --- |
| [`uno-q-bridge/`](uno-q-bridge) | Arduino UNO Q, STM32U585 side | Arduino IDE, UNO Q core + Arduino_RouterBridge | exposes `set_relay`, `set_led`, `get_sensors` to Linux over RPC (Pipeline 1) | ran on the UNO Q |
| [`esp32-radar3d/`](esp32-radar3d) | 3× ESP32 | Arduino IDE, ESP32 core | cross-ping CSI mesh, UDP `0xC5110003` (Pipeline 2) | used by the team |
| [`esp32-csi-node/`](esp32-csi-node) | 1× ESP32-S3 | ESP-IDF 5.2+ | single-node CSI capture, UDP `0xC5110001` | ported from RuView, not re-verified |

Frame formats: [docs/csi-frame-format.md](../docs/csi-frame-format.md).
Board notes, placement and power: [docs/HARDWARE.md](../docs/HARDWARE.md).
