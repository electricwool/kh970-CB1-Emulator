#!/usr/bin/env python3
"""Poll the emulator's /csi_debug until the machine link comes up.

Usage: python poll_link.py [seconds]
"""
import sys
import time
import json
import urllib.request

URL = "http://127.0.0.1:8765/csi_debug"
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

last = None
t0 = time.time()
while time.time() - t0 < SECS:
    try:
        d = json.load(urllib.request.urlopen(URL, timeout=2))
    except Exception as e:
        print(f"  [{time.time()-t0:4.0f}s] backend not ready: {e}", flush=True)
        time.sleep(1)
        continue
    line = (f"[{time.time()-t0:5.1f}s] pc={d['pc']:04X} cs_low={int(d['cs_low'])} "
            f"din_high={int(d['din_high'])} mach={d['fe56.7_machine']} "
            f"linked={d['fe56.5_linked']} bytes={d['csi_byte_n']} "
            f"pins={d['csi_pin_n']}")
    if line != last:
        print(line, flush=True)
        last = line
    if d['csi_byte_n'] > 0:
        print("LINK UP - machine is clocking bytes", flush=True)
        break
    time.sleep(1)
else:
    print("no link activity seen in window", flush=True)
