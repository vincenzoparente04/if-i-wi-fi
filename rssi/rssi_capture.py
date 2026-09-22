"""rssi_capture.py: labelled RSSI capture (still / moving) for analysis.

Guides you through a sequence of phases (STAND STILL / MOVE) and records RSSI
at a fixed rate into a CSV `t,rssi,label`. Use it to measure how well motion
separates from noise in your room and to tune the detector on real data.

Usage (on the UNO Q):
    PATH=/usr/sbin:/sbin:$PATH python3 -m rssi.rssi_capture

Output: /tmp/rssi_capture.csv
"""
from __future__ import annotations

import sys
import threading
import time

from rssi.rssi_demo import (read_rssi_linux, detect_wifi_iface,
                            detect_gateway, _traffic_generator)


# Sequenza: (testo a schermo, secondi, etichetta nel CSV)
# 'prep' non viene salvato; serve solo a far stabilizzare e prepararsi.
PHASES = [
    ("PREPARATI (non registro ancora)", 5, "prep"),
    ("STAI FERMO / allontanati dalla board", 15, "still"),
    ("MUOVITI vicino alla board (cammina, agita la mano)", 15, "move"),
    ("STAI FERMO", 15, "still"),
    ("MUOVITI vicino alla board", 15, "move"),
    ("STAI FERMO", 10, "still"),
]

RATE_HZ = 20.0
OUT_PATH = "/tmp/rssi_capture.csv"


def main():
    iface = detect_wifi_iface() or "wlan0"
    gw = detect_gateway()
    period = 1.0 / RATE_HZ

    stop = threading.Event()
    if gw:
        threading.Thread(target=_traffic_generator, args=(gw, 0.02, stop),
                         daemon=True).start()

    # sanity: una lettura
    v0 = read_rssi_linux(iface)
    if v0 is None:
        print(f"ERRORE: nessun RSSI da {iface}. Controlla il WiFi.")
        return
    print(f"\nInterfaccia: {iface}   gateway: {gw}   RSSI iniziale: {v0} dBm")
    print("Segui le istruzioni. Inizio tra 2s...\n")
    time.sleep(2)

    rows: list[tuple[float, float, str]] = []
    t0 = time.time()
    for text, dur, lab in PHASES:
        marker = ">>> FERMO" if lab == "still" else (">>> MUOVITI" if lab == "move" else "...")
        ph_start = time.time()
        last_cd = -1
        while time.time() - ph_start < dur:
            rem = dur - (time.time() - ph_start)
            v = read_rssi_linux(iface)
            if v is not None and lab != "prep":
                rows.append((round(time.time() - t0, 3), v, lab))
            cd = int(rem) + 1
            if cd != last_cd:
                sys.stdout.write(f"\r  {marker:<14} {text:<48} {cd:>2}s  rssi={v}   ")
                sys.stdout.flush()
                last_cd = cd
            time.sleep(period)
        sys.stdout.write("\r" + " " * 90 + "\r")
        print(f"  [fatto] {text}")

    stop.set()

    with open(OUT_PATH, "w") as f:
        f.write("t,rssi,label\n")
        for t, v, lab in rows:
            f.write(f"{t},{v},{lab}\n")

    n_still = sum(1 for r in rows if r[2] == "still")
    n_move = sum(1 for r in rows if r[2] == "move")
    print(f"\nSALVATO: {len(rows)} campioni in {OUT_PATH}")
    print(f"  fermo:    {n_still} campioni")
    print(f"  movimento:{n_move} campioni")
    print("Ora dillo all'assistente: recupero io il file e lo analizzo.")


if __name__ == "__main__":
    main()
