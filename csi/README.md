# Pipeline 2: CSI host

The Python host that receives Channel State Information from the ESP32 mesh
([`firmware/esp32-radar3d`](../firmware/esp32-radar3d)) and turns it into
presence, position and (experimental) vital signs.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # from the repository root
pip install -e ".[mqtt,plot]"    # optional: Home Assistant bridge, plotting
```

Python 3.10 or newer.

## Main entry points

| Command | What it does |
| --- | --- |
| `python3 -m csi.quadrants.ws_server --udp-port 5005 --ws-port 8765 --room 6x5 --grid 4x4 --rx "0.5,0.5;5.5,0.5;3.0,4.5" --enable-3d` | Receives frames, runs presence + 2D position (+ height class) and broadcasts WebSocket messages for [`web/ui.html`](../web) |
| `python3 -m csi.blob_cli --train --udp-port 5005` | Collects labelled positions and trains the ML position regressor |
| `python3 -m csi.csi_mac --udp-port 5005 --monitor --vitals` | Monitor with breathing / heart-rate estimates |
| `python3 -m csi.csi_record --record` / `--replay file.txt --websocket :8765` | Record a capture (serial or BLE), replay it later without hardware |
| `./tools/simulate_csi.sh` | Everything above the firmware, with three simulated ESP32 |

## Package layout

| Path | Contents |
| --- | --- |
| `presence/` | `PresenceDetector`: EMPTY / STILL / MOTION state machine, no training |
| `quadrants/` | `ws_server`, `BlobEstimator` (weighted centroid), `PositionRegressor` (RandomForest + Kalman, leave-one-cell-out gate) |
| `blob3d/` | height class (LOW / MID / HIGH) and 3D Kalman tracker |
| `signal/` | Hampel, biquad filters, Welford statistics, Fresnel, spectrogram, CSI ratio, BVP |
| `vitals/` | respiration, heart rate, anomaly detection (experimental, not medical) |
| `services/` | orchestrator, health check, metrics |
| `csi_processor.py`, `esp32_parser.py` | frame parsers (text, ADR-018 `0xC5110001`, radar3d `0xC5110003`) |
| `classifier.py`, `multi_ap.py`, `features.py` | grid classifiers and feature extraction |
| `ha_bridge.py`, `ws_client.py` | Home Assistant MQTT bridge, WebSocket client |

Some internal comments are still in Italian; the public interfaces and docs are in English.

## Tests

```bash
pytest            # 123 tests, from the repository root (plot tests need the [plot] extra)
```
