"""rssi_demo.py: live presence dashboard driven by Wi-Fi RSSI.

This is the script we ran on stage on the Arduino UNO Q. It samples the RSSI
of the host's own Wi-Fi link, learns what the empty room looks like, and then
flags presence in real time. The dashboard is the same page as the web demo
(demo/index.html), switched to live mode and fed through Server-Sent Events.
No dependencies outside the Python standard library.

Sources:
    linux : reads /proc/net/wireless or `iw` (Arduino UNO Q, any Linux box)
    macos : reads the Mac's Wi-Fi RSSI through CoreWLAN (small Swift helper)
    sim   : synthetic RSSI, to try the plumbing without Wi-Fi

Detector (robust to false positives):
    - swing  = trimmed peak-to-peak (5th-95th percentile) of the RSSI window
    - jitter = standard deviation of the RSSI window
    - level  = |RSSI - empty-room median|, catches a body sitting still in the path
  Swing and jitter are normalised by the empty-room baseline. A sample is
  "hot" if max(swing, jitter) exceeds the auto threshold or the level shift
  exceeds its band; presence needs >= 70 % hot samples over ~0.7 s and is
  released below 25 % (hysteresis, no flicker).

Usage (on the UNO Q):
    python3 -m rssi.rssi_demo --source linux
    # then open http://<uno-q-ip>:8080/

Usage (on a laptop):
    python3 -m rssi.rssi_demo --source macos      # your Mac's real Wi-Fi RSSI
    python3 -m rssi.rssi_demo --source sim        # no Wi-Fi needed
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import stdev


DEFAULT_HTTP_PORT = 8080
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


# ============================================================
# Shared state (the RSSI worker writes, the HTTP handler reads)
# ============================================================
_state_lock = threading.Lock()
_state: dict = {
    "phase": "init",       # init | warmup | calibrating | ready
    "msg": "Starting…",
    "ratio": 1.0,
    "r_swing": 1.0,
    "r_jitter": 1.0,
    "level_dev": 0.0,
    "level_th": 3.5,
    "rssi": 0.0,
    "presence": False,
    "threshold": 2.0,
    "fps": 0.0,
    "source": "",
    "base_level": None,
}


def _set_state(**kw):
    with _state_lock:
        _state.update(kw)


# ============================================================
# RSSI sources
# ============================================================
def detect_wifi_iface() -> str | None:
    """First Wi-Fi interface (wlan0, wlp*, wlx*) on Linux."""
    try:
        for entry in sorted(os.listdir("/sys/class/net")):
            if re.match(r"^(wlan|wlx|wlp)", entry):
                return entry
    except FileNotFoundError:
        pass
    return None


def detect_gateway() -> str | None:
    """Default gateway (target of the traffic generator), or None."""
    try:  # Linux
        out = subprocess.check_output(["ip", "route"], timeout=3, stderr=subprocess.DEVNULL).decode()
        m = re.search(r"default via (\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:  # macOS / BSD
        out = subprocess.check_output(["route", "-n", "get", "default"], timeout=3, stderr=subprocess.DEVNULL).decode()
        m = re.search(r"gateway:\s*(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _find_iw() -> str:
    """Absolute path of `iw` (non-interactive shells often miss /sbin)."""
    for p in ("/usr/sbin/iw", "/sbin/iw", "/usr/bin/iw"):
        if os.path.exists(p):
            return p
    return shutil.which("iw") or "iw"


IW_BIN = _find_iw()
_linux_method: str | None = None  # "proc" | "iw", decided once


def _read_proc_rssi(iface: str) -> float | None:
    """RSSI (dBm) from /proc/net/wireless (legacy WEXT interface).

    Columns: iface | status | link | level(dBm) | noise | ...
    Pure file read, ~0 ms. Many modern drivers no longer expose it."""
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                s = line.strip()
                if s.startswith(iface + ":"):
                    parts = s.replace(iface + ":", " ").split()
                    if len(parts) >= 3:
                        return float(parts[2].rstrip("."))
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    return None


def _read_iw_rssi(iface: str) -> float | None:
    """RSSI (dBm) via `iw dev <iface> link` (nl80211). ~5 ms on the UNO Q."""
    try:
        out = subprocess.check_output([IW_BIN, "dev", iface, "link"],
                                      timeout=2, stderr=subprocess.DEVNULL).decode()
        m = re.search(r"signal:\s*(-?\d+)", out)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def read_rssi_linux(iface: str) -> float | None:
    """RSSI (dBm) on Linux: /proc/net/wireless if available, otherwise `iw`.
    The working method is remembered after the first success."""
    global _linux_method
    if _linux_method == "proc":
        return _read_proc_rssi(iface)
    if _linux_method == "iw":
        return _read_iw_rssi(iface)
    v = _read_proc_rssi(iface)
    if v is not None:
        _linux_method = "proc"
        return v
    v = _read_iw_rssi(iface)
    if v is not None:
        _linux_method = "iw"
        return v
    return None


# --- macOS: CoreWLAN Swift helper, compiled once and then executed per sample ---
_MACOS_SWIFT_SRC = """
import CoreWLAN
if let i = CWWiFiClient.shared().interface() {
    print(i.rssiValue())
} else {
    print("none")
}
"""
_macos_helper_path: str | None = None


def _ensure_macos_helper() -> str | None:
    """Compile the CoreWLAN helper into a temporary binary (once)."""
    global _macos_helper_path
    if _macos_helper_path and os.path.exists(_macos_helper_path):
        return _macos_helper_path
    if not shutil.which("swiftc") and not shutil.which("swift"):
        return None
    import tempfile
    d = tempfile.mkdtemp(prefix="rssi_demo_")
    src = os.path.join(d, "rssi.swift")
    with open(src, "w") as f:
        f.write(_MACOS_SWIFT_SRC)
    binp = os.path.join(d, "rssi_helper")
    try:
        subprocess.check_call(["swiftc", "-O", src, "-o", binp],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _macos_helper_path = binp
        return binp
    except Exception:
        _macos_helper_path = src  # fallback: run through the swift interpreter
        return _macos_helper_path


def read_rssi_macos() -> float | None:
    """RSSI (dBm) of the Mac's Wi-Fi via CoreWLAN."""
    helper = _ensure_macos_helper()
    if not helper:
        return None
    try:
        cmd = ["swift", helper] if helper.endswith(".swift") else [helper]
        out = subprocess.check_output(cmd, timeout=4, stderr=subprocess.DEVNULL).decode().strip()
        if out and out != "none":
            v = float(out)
            if v != 0.0:
                return v
    except Exception:
        pass
    return None


class _SimSource:
    """Synthetic RSSI: a stable noisy baseline, and every ~12 s four seconds
    of 'presence' (large fluctuations) so the dashboard flips to PRESENT."""
    def __init__(self, base: float = -52.0):
        self.base = base
        self.t0 = time.time()

    def read(self) -> float:
        t = time.time() - self.t0
        present = (t % 12.0) >= 8.0  # 12 s cycle: 8 s empty, 4 s present
        noise = random.gauss(0, 0.6)
        if present:
            noise += random.gauss(0, 4.0) + 3.0 * math.sin(t * 6.0)
        return self.base + noise


# ============================================================
# Traffic generator: keeps the link "warm" and responsive to motion
# ============================================================
def _traffic_generator(target_ip: str, period_s: float, stop_evt: threading.Event):
    """Sends tiny UDP datagrams to the gateway so frames keep flowing and the
    driver refreshes RSSI more often. No root required."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    next_t = time.time()
    while not stop_evt.is_set():
        try:
            s.sendto(b"x", (target_ip, 9))  # port 9 (discard), usually closed
        except OSError:
            pass
        next_t += period_s
        sleep = next_t - time.time()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_t = time.time()


# ============================================================
# Outlier-robust numeric helpers
# ============================================================
def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _trimmed_p2p(values, lo_q: float = 5, hi_q: float = 95) -> float:
    """Trimmed peak-to-peak: immune to single impulsive outliers (central 90 %)."""
    vals = list(values)
    if len(vals) < 4:
        return 0.0
    return _percentile(vals, hi_q) - _percentile(vals, lo_q)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[len(s) // 2]


# ============================================================
# RSSI worker: sample, calibrate, detect, publish to _state
# ============================================================
def _rssi_worker(source: str, iface: str | None, rate_hz: float, window: int,
                 th: float, warmup_s: float, cal_s: float, use_traffic: bool):
    W = max(window, 20)
    period = 1.0 / max(rate_hz, 1.0)
    _set_state(threshold=th, source=source)

    # ---- Source ---------------------------------------------------------
    if source == "sim":
        sim = _SimSource()
        reader = sim.read
    elif source == "macos":
        reader = read_rssi_macos
    else:
        if not iface:
            iface = detect_wifi_iface()
        if not iface:
            _set_state(phase="init", msg="No Wi-Fi interface found (wlan0?).")
            return
        reader = lambda: read_rssi_linux(iface)  # noqa: E731

    # ---- Optional traffic generator (real sources only) ------------------
    stop_evt = threading.Event()
    if use_traffic and source in ("linux", "macos"):
        gw = detect_gateway()
        if gw:
            threading.Thread(target=_traffic_generator, args=(gw, 0.02, stop_evt),
                             daemon=True).start()
            _set_state(msg=f"Traffic generator -> {gw}")

    # ---- Make sure the source produces data ------------------------------
    _set_state(phase="init", msg=f"Reading RSSI ({source})…")
    first = None
    t_wait = time.time()
    while time.time() - t_wait < 6.0:
        first = reader()
        if first is not None:
            break
        time.sleep(period)
    if first is None:
        _set_state(phase="init", msg=f"No RSSI from source '{source}'. Check the Wi-Fi connection.")
        return

    # ---- Fixed-rate sampling with a median prefilter ---------------------
    # The median over MEDK samples removes impulsive driver spikes (single
    # jumps of 6-11 dB) that would otherwise trigger false positives.
    MEDK = 5
    _medq: deque = deque(maxlen=MEDK)

    def sample():
        raw = reader()
        if raw is None:
            return None
        _medq.append(raw)
        return _median(list(_medq))

    # ---- Warm-up: the operator leaves the scene --------------------------
    t_start = time.time()
    while True:
        remaining = warmup_s - (time.time() - t_start)
        if remaining <= 0:
            break
        _set_state(phase="warmup", msg=f"Please step out of the area… {int(remaining) + 1}s")
        sample()
        time.sleep(period)

    # ---- Empty-room calibration with a quality check ---------------------
    # Measures the channel noise over cal_s. If someone is still in the path
    # (noise too high) it retries and keeps the cleanest run.
    CAL_CLEAN_TH = 1.2          # max std (dB) for a "clean" calibration
    MAX_CAL_TRIES = 4
    best_cal: list[float] | None = None
    best_noise = 1e9
    for attempt in range(MAX_CAL_TRIES):
        suffix = f" (retry {attempt + 1})" if attempt else ""
        cal: list[float] = []
        t_cal = time.time()
        while True:
            remaining = cal_s - (time.time() - t_cal)
            if remaining <= 0:
                break
            _set_state(phase="calibrating",
                       msg=f"Learning the empty room…{suffix} {int(remaining) + 1}s")
            v = sample()
            if v is not None:
                cal.append(v)
            time.sleep(period)
        if len(cal) < W * 2:
            continue
        noise = stdev(cal)
        if noise < best_noise:
            best_noise, best_cal = noise, cal
        if noise <= CAL_CLEAN_TH:
            break
        _set_state(phase="calibrating",
                   msg="The path looks busy (someone nearby?). Clear the area, retrying…")
        time.sleep(1.5)

    if best_cal is None:
        _set_state(phase="init", msg="Calibration failed: not enough samples. Restart.")
        return
    cal = best_cal

    # Baseline: robust median of the metrics over sliding windows of the
    # empty room (adapts to the geometry of *your* AP <-> board setup).
    swing_w: list[float] = []
    jitter_w: list[float] = []
    for i in range(W, len(cal) + 1):
        chunk = cal[i - W:i]
        swing_w.append(_trimmed_p2p(chunk))
        jitter_w.append(stdev(chunk) if len(chunk) >= 2 else 0.0)
    base_swing = max(_median(swing_w), 0.4)
    base_jitter = max(_median(jitter_w), 0.25)

    # Empty-room level + level band for STATIC presence (a body blocking the
    # direct path). Scales with the noise, capped at 7 dB.
    base_level = _median(cal)
    level_db = max(3.5, min(6.0 * best_noise, 7.0))

    # AUTO threshold (th <= 0): 40 % above the 95th percentile of the empty-room
    # ratio, clamped to [2.6, 3.6]. Noisy room -> higher threshold, quiet room
    # -> more sensitive, so behaviour is consistent across rooms.
    if th <= 0:
        emp = [max(swing_w[i] / base_swing, jitter_w[i] / base_jitter)
               for i in range(len(swing_w))]
        emp_p95 = _percentile(emp, 95) if emp else 1.5
        th = max(2.6, min(3.6, emp_p95 * 1.4))
    _set_state(threshold=round(th, 2), level_th=level_db, base_level=base_level)

    # ---- Live monitoring --------------------------------------------------
    rec: deque = deque(maxlen=W)

    # Presence = SUSTAINED elevation with hysteresis. Human motion keeps the
    # ratio high continuously; noise spikes last 1-3 samples and never fill
    # the short window.
    SUSTAIN_K = 14       # ~0.7 s at 20 Hz
    ON_FRAC = 0.70       # fire when >= 70 % of the last K samples are hot
    OFF_FRAC = 0.25      # release below 25 %
    above: deque = deque(maxlen=SUSTAIN_K)

    presence = False
    _set_state(phase="ready", presence=False, msg="ready")

    frame_times: deque = deque(maxlen=50)
    last_t = time.time()

    while True:
        v = sample()
        time.sleep(period)
        if v is None:
            continue
        rec.append(v)

        now = time.time()
        dt = now - last_t
        last_t = now
        if dt > 0:
            frame_times.append(1.0 / dt)
        fps = sum(frame_times) / len(frame_times) if frame_times else 0.0

        if len(rec) < W:
            continue

        swing = _trimmed_p2p(list(rec))
        jitter = stdev(rec)
        r_swing = swing / base_swing
        r_jitter = jitter / base_jitter
        ratio = max(r_swing, r_jitter)       # variability  -> movement
        level_dev = abs(v - base_level)      # level offset -> static presence

        hot = (ratio > th) or (level_dev > level_db)
        above.append(1 if hot else 0)
        frac = sum(above) / len(above)
        if not presence and frac >= ON_FRAC:
            presence = True
        elif presence and frac <= OFF_FRAC:
            presence = False

        _set_state(phase="ready", ratio=ratio, r_swing=r_swing, r_jitter=r_jitter,
                   level_dev=level_dev, level_th=level_db, rssi=v,
                   presence=presence, fps=fps)


# ============================================================
# HTTP: serves demo/index.html in live mode + the SSE state stream
# ============================================================
_STATIC = {
    "/assets/uno-q.png": ("assets/uno-q.png", "image/png"),
}


def _live_page() -> bytes:
    html = (DEMO_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("<script>", "<script>window.IFI_LIVE = true;</script>\n<script>", 1).encode("utf-8")


class _DemoHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(_live_page(), "text/html; charset=utf-8")
        if path in _STATIC:
            rel, ctype = _STATIC[path]
            return self._send((DEMO_DIR / rel).read_bytes(), ctype)
        if path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    with _state_lock:
                        snap = dict(_state)
                    self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.send_response(404)
        self.end_headers()


# ============================================================
# Entry point
# ============================================================
def _auto_source() -> str:
    if os.path.exists("/proc/net/wireless"):
        return "linux"
    if platform.system() == "Darwin":
        return "macos"
    return "sim"


def main():
    ap = argparse.ArgumentParser(description="If-I-Wi-Fi live RSSI presence dashboard")
    ap.add_argument("--source", choices=["auto", "linux", "macos", "sim"], default="auto",
                    help="RSSI source (default: auto)")
    ap.add_argument("--iface", default=None, help="Wi-Fi interface on Linux (default: autodetect)")
    ap.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                    help=f"Dashboard HTTP port (default: {DEFAULT_HTTP_PORT})")
    ap.add_argument("--rate", type=float, default=20.0, help="RSSI sampling rate in Hz (default 20)")
    ap.add_argument("--window", type=int, default=50, help="Detector window in samples (default 50)")
    ap.add_argument("--th", type=float, default=0.0,
                    help="Presence ratio threshold. 0 = AUTO (adapts to the room during calibration).")
    ap.add_argument("--warmup-s", type=float, default=5.0,
                    help="Seconds to leave the scene before calibration (default 5)")
    ap.add_argument("--cal-s", type=float, default=15.0,
                    help="Seconds of empty-room calibration (default 15)")
    ap.add_argument("--no-traffic", action="store_true",
                    help="Disable the traffic generator (gateway pings)")
    args = ap.parse_args()

    source = args.source if args.source != "auto" else _auto_source()

    threading.Thread(
        target=_rssi_worker,
        args=(source, args.iface, args.rate, args.window, args.th,
              args.warmup_s, args.cal_s, not args.no_traffic),
        daemon=True,
    ).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.http_port), _DemoHTTPHandler)
    print(f"\n  RSSI source: {source}")
    print(f"  Dashboard:   http://localhost:{args.http_port}/  (or http://<this-host-ip>:{args.http_port}/)")
    print("  Ctrl+C to quit.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
