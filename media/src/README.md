# Media sources

Everything in `media/` is generated from files in this folder or from the demo,
so it can be regenerated after a change.

| Output | Source | How |
| --- | --- | --- |
| `media/banner.png` (also the GitHub social preview) | `banner.html` | screenshot at 1280×640, device scale 1.5 |
| `media/how-it-works.png` | `diagrams.html?d=pipeline` | element screenshot of `#pipeline`, transparent background, scale 2 |
| `media/architecture.png` | `diagrams.html?d=arch` | element screenshot of `#arch`, transparent background, scale 2 |
| `media/demo.gif` | `demo/index.html?capture&seed=7` | 1200×675 frames every 300 ms of simulated time from t = 12 s, scaled to 800 px, shared 255-colour palette |
| `media/demo-still.png` | `demo/index.html?capture&seed=7` | 1440×810 at t = 33 s |
| `media/csi-monitor.jpg` | `web/ui.html` | with `./tools/simulate_csi.sh` running |
| `media/use-cases/*.jpg` | concept video from the pitch | frames, 640×360 |

Serve the repository root (`python3 -m http.server`) so the fonts and relative
paths resolve, and use any headless Chrome driver (Puppeteer, Playwright, or
the DevTools protocol) to take the screenshots.

`uno-q-render.png` and `uno-q-render-bottom.png` are 3D renders of the Arduino UNO Q
exported from our presentation.
