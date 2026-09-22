# The journey

If-I-Wi-Fi was built by three students in about two months. We tried a lot of
things; this page tells the story and explains why the repository looks the
way it does.

## Timeline

| When (2026) | What happened |
| --- | --- |
| **May 14–15** | **Arduino Hackathon at Politecnico di Milano.** We pitched *"The sixth sense of buildings"* and got an Arduino UNO Q to prove it. Day one: a feasibility test of the board's two brains talking through `arduino-router` (MessagePack RPC over a Unix socket), and RSSI read straight from `/proc/net/wireless`. |
| **May 15** | **First live demo.** UNO Q + a phone hotspot in an empty lecture hall: calibrate the empty room, calibrate movement, then walk in. The terminal flips to `PRESENT`. This is Pipeline 1 in its first form. |
| **May 14–21** | **Chasing CSI.** RSSI is one number; we wanted the whole channel. From day one an ESP32 streams CSI to the UNO Q: first over UART through the STM32, then over Bluetooth LE, then over UDP. Along the way: seat mapping, room fingerprinting, multi-AP channel hopping. Most of it was later cut. |
| **May 21–25** | **The mesh.** One link cannot localise anyone, and phones randomise their MAC addresses. The fix: three ESP32 that ping each other at 100 Hz on a fixed channel, which gives six stable cross paths. The Python host grows presence, 2D tracking, vitals, a WebSocket server and a 3D UI, plus a test suite. |
| **May 24–28** | **Clean-up.** We studied [RuView](https://github.com/ruvnet/RuView), a much larger open-source Wi-Fi sensing project, ported a few solid pieces (signal filters, vitals estimators, the binary frame format, an ESP-IDF capture node, a Rust decoder) and wrote down everything we refused to port and why: [what-was-cut.md](what-was-cut.md). |
| **June 6–10** | **Stage demos.** Full-screen dashboards for the presentation: a CSI version (one ESP32 streaming to the UNO Q over UDP) and the RSSI version with radar and 3D views, automatic threshold and a calibration quality check. That RSSI detector is the one in this repository. |
| **July 24** | **Final presentation** at Politecnico di Milano. |
| **September** | **This repository.** Three working repositories and eight branches merged into one, translated to English, with a web demo that runs without the board. The UNO Q was on loan and has gone back. |

## Why the repository looks like this

- **Two pipelines.** RSSI is what made the UNO Q demo work with zero extra
  hardware. CSI is where the finer sensing is. Both are kept, and both are honest about their status.
- **A simulated web demo.** We no longer have the board, but the detector is
  just maths. The [web demo](../demo) runs the same algorithm on a simulated
  signal, and `rssi/rssi_demo.py --source macos` runs it on a real Wi-Fi link
  from your laptop.
- **An `experimental/` folder.** The Rust host and a few tools were never
  verified end to end. They are kept, clearly labelled, instead of being
  mixed with code we trust.
- **A fresh history.** The original repositories contained local network
  configuration, so this one starts clean. The full commit history stays in
  the original (archived) repositories.

## What we learned

1. **RSSI is underrated.** With a median filter, a trimmed peak-to-peak, an
   automatic threshold and hysteresis, one number per frame is enough to
   detect presence reliably next to the link.
2. **Placement beats algorithms.** Most "bugs" were a node in the wrong place.
3. **Calibrate with a quality check.** If someone is in the path during
   calibration, every decision afterwards is wrong. Measure the noise and retry.
4. **Say what you did not validate.** Vital signs from Wi-Fi are exciting and
   easy to oversell. We label them experimental and never call them medical.
5. **Coding agents speed you up, audits keep you honest.** We used AI agents
   heavily; the cut list and the status table exist because we reviewed what
   they produced.
