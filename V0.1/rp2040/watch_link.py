#!/usr/bin/env python3
"""Continuously watch the emulator's /csi_debug and log link-state changes.

Usage: python watch_link.py [poll_seconds]
Runs until Ctrl-C. Logs every state change with a timestamp and flags link-up
the moment the machine starts clocking bytes.
"""
import sys
import time
import json
import urllib.request

URL = "http://127.0.0.1:8765/csi_debug"
POLL = float(sys.argv[1]) if len(sys.argv) > 1 else 0.2


def fetch():
    try:
        return json.load(urllib.request.urlopen(URL, timeout=2))
    except Exception:
        return None


prev = None
t0 = time.time()
print(f"watching link state every {POLL*1000:.0f} ms ... (Ctrl-C to stop)", flush=True)
while True:
    d = fetch()
    if d is None:
        time.sleep(POLL)
        continue
    evs = tuple(d.get('events', []))
    key = (d['cs_low'], d['fe56.7_machine'], d['fe56.5_linked'], d['csi_byte_n'], evs)
    if key != prev:
        dt = time.time() - t0
        print(f"[{dt:8.2f}s] cs_low={int(d['cs_low'])} mach={d['fe56.7_machine']} "
              f"linked={d['fe56.5_linked']} bytes={d['csi_byte_n']} "
              f"fe7e={d['fe7e']:02X} pins={d['csi_pin_n']} "
              f"events={list(evs)}", flush=True)
        if d['csi_byte_n'] > 0 and (prev is None or prev[3] == 0):
            print("*** LINK UP - machine is clocking bytes ***", flush=True)
            print("    recent csi_in_log:", d['csi_in_log'], flush=True)
        prev = key
    time.sleep(POLL)
