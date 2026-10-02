#!/usr/bin/env python3
"""Capture all 8 Saleae channels for N seconds, export to CSV, then analyze
the CSV from disk (no terminal streaming). Reports per-channel activity so we
can identify SCK (dense clock) vs CS (sparse wide pulses) vs data lines.

Usage: python capture_full.py [seconds]
"""
import os
import sys
import csv
from saleae import automation

OUT = r"c:\esp32\cb1\_capture"
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
CH = list(range(8))

os.makedirs(OUT, exist_ok=True)

with automation.Manager.connect(port=10430) as mgr:
    cfg = automation.LogicDeviceConfiguration(
        enabled_digital_channels=CH, digital_sample_rate=1_000_000)
    cap = automation.CaptureConfiguration(
        capture_mode=automation.TimedCaptureMode(duration_seconds=DUR))
    with mgr.start_capture(device_configuration=cfg,
                           capture_configuration=cap) as capture:
        print(f"capturing {DUR:.0f}s (all 8 channels) ...", flush=True)
        capture.wait()
        print("exporting CSV ...", flush=True)
        capture.export_raw_data_csv(directory=OUT, digital_channels=CH)

# ---- analyze from disk ---------------------------------------------------
csvpath = os.path.join(OUT, 'digital.csv')
tr = {c: [] for c in CH}
with open(csvpath) as f:
    rd = csv.reader(f)
    next(rd)                       # header
    for row in rd:
        t = float(row[0])
        for c in CH:
            v = int(row[c + 1])
            if not tr[c] or tr[c][-1][1] != v:
                tr[c].append((t, v))

print("\nch  transitions  final  low-pulses  median-low-width  guess")
for c in CH:
    lows = [(tr[c][i + 1][0] - tr[c][i][0])
            for i in range(len(tr[c]) - 1) if tr[c][i][1] == 0]
    med = None
    if lows:
        lows.sort()
        med = lows[len(lows) // 2]
    final = tr[c][-1][1] if tr[c] else '?'
    n = len(tr[c])
    if n <= 1:
        guess = 'static ' + ('HIGH' if final == 1 else 'LOW')
    elif n > 100 and med is not None and med < 0.0005:
        guess = 'SCK? (dense clock)'
    elif n <= 10 and med is not None and med > 0.001:
        guess = 'CS? (sparse wide pulses)'
    else:
        guess = 'data?'
    med_s = f"{med*1000:.3f} ms" if med is not None else "-"
    print(f"  {c}   {n:>9}     {final}     {len(lows):>6}        {med_s}     {guess}")
