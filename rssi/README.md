# Pipeline 1: RSSI on the Arduino UNO Q

Presence detection from the signal strength of the board's own Wi-Fi link.
No extra hardware, standard library only (scikit-learn is optional).

| File | What it is |
| --- | --- |
| [`rssi_demo.py`](rssi_demo.py) | **The stage demo.** Calibrates the empty room, detects presence, serves the live dashboard (`demo/index.html`) over HTTP + Server-Sent Events. Runs on the UNO Q, on Linux, on a Mac, or simulated. |
| [`decision_engine.py`](decision_engine.py) | Orchestrator: RSSI detector + optional monitor-mode capture + STM32 sensors → relay / LED through `arduino-router`. |
| [`enhanced_presence.py`](enhanced_presence.py) | The hackathon detector (gradient + multi-metric fusion). `--mode quick` is the "calibrate empty room, calibrate movement, test" flow from the first live demo. |
| [`bridge_client.py`](bridge_client.py) | Dependency-free MessagePack RPC client for `arduino-router` (`--ping`, `--get-sensors`, `--set-relay`). |
| [`rssi_ml.py`](rssi_ml.py) | Optional Random Forest classifier trained on labelled windows. |
| [`rssi_capture.py`](rssi_capture.py) | Guided, labelled RSSI capture to CSV for offline analysis. |
| [`monitor_presence.py`](monitor_presence.py) | Monitor-mode capture (needs root): device count, probe requests, per-MAC RSSI. |

## Run it

```bash
# on the UNO Q (or any Linux box with Wi-Fi)
sudo apt-get install -y iw
python3 -m rssi.rssi_demo --source linux
# open http://<uno-q-ip>:8080/

# on a Mac: real RSSI from your own Wi-Fi (needs Xcode command line tools)
python3 -m rssi.rssi_demo --source macos

# anywhere, no Wi-Fi
python3 -m rssi.rssi_demo --source sim
```

Step out of the area for the warm-up, keep it clear for the 15 s calibration,
then walk between the board and the access point. If the room is noisy the
calibration retries (up to 4 times) and keeps the cleanest run.

Useful flags: `--th 0` (automatic threshold, default), `--cal-s 15`,
`--warmup-s 5`, `--rate 20`, `--no-traffic`.

## Close the loop on the MCU

Flash [`firmware/uno-q-bridge`](../firmware/uno-q-bridge), then:

```bash
python3 -m rssi.bridge_client --ping
python3 -m rssi.decision_engine --dry-run     # log decisions only
python3 -m rssi.decision_engine               # drive the relay on presence
```

The algorithm is described step by step in [docs/HOW_IT_WORKS.md](../docs/HOW_IT_WORKS.md#pipeline-1-the-rssi-detector).
