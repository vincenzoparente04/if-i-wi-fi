#!/usr/bin/env bash
# simulate_csi.sh: run the CSI pipeline end to end with no hardware.
#
# Starts:
#   1. the WebSocket backend (csi.quadrants.ws_server) with 3D tracking
#   2. a simulator that emits radar3d frames as if from 3 ESP32 nodes
#   3. the live 3D UI (web/ui.html) in your browser
#
# Ctrl-C stops everything.
#
# Usage:
#   ./tools/simulate_csi.sh
#   ./tools/simulate_csi.sh --no-3d
#   ./tools/simulate_csi.sh --static       # simulated person does not move
#   ./tools/simulate_csi.sh --no-browser

set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

ENABLE_3D=1
OPEN_BROWSER=1
INJECTOR_ARGS="--moving --burst 0"

for arg in "$@"; do
  case "$arg" in
    --no-3d)       ENABLE_3D=0 ;;
    --no-browser)  OPEN_BROWSER=0 ;;
    --static)      INJECTOR_ARGS="--burst 0" ;;
    --help|-h)
      sed -n '2,15p' "$0"; exit 0 ;;
  esac
done

PIDS=()
cleanup() {
  echo
  echo "==> Stopping: ${PIDS[*]:-none}"
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "==> Starting ws_server (UDP :5005, WS :8765)"
WS_ARGS=(--udp-port 5005 --ws-port 8765 --room 6x5 --grid 4x4 \
         --rx "0.5,0.5;5.5,0.5;3.0,4.5")
if [ "$ENABLE_3D" = "1" ]; then
  WS_ARGS+=(--enable-3d --room-height 3.0)
fi
python3 -m csi.quadrants.ws_server "${WS_ARGS[@]}" &
PIDS+=($!)
sleep 1.5

echo "==> Starting the frame simulator (3 virtual ESP32 nodes)"
python3 tools/inject_radar3d_frames.py --port 5005 $INJECTOR_ARGS &
PIDS+=($!)
sleep 0.5

UI_URL="file://$PWD/web/ui.html?ws=ws://localhost:8765&room=6x5&grid=4x4"
if [ "$ENABLE_3D" = "0" ]; then
  UI_URL="${UI_URL}&3d=0"
fi

if [ "$OPEN_BROWSER" = "1" ]; then
  if command -v open >/dev/null 2>&1; then
    open "$UI_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$UI_URL" &
  fi
fi
echo "==> UI: $UI_URL"
echo
echo "==> Ctrl-C to stop."
wait
