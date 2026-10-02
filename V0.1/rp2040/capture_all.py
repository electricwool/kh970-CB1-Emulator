#!/usr/bin/env python3
"""Capture all 8 digital channels for N seconds and report per-channel activity."""
import os
import sys
import csv
from saleae import automation

OUT = r"c:\esp32\cb1\_capture"
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
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
        capture.export_raw_data_csv(directory=OUT, digital_channels=CH)

tr = {c: [] for c in CH}
with open(os.path.join(OUT, 'digital.csv')) as f:
    rd = csv.reader(f)
    next(rd)
    for row in rd:
        t = float(row[0])
        for c in CH:
            v = int(row[c + 1])
            if not tr[c] or tr[c][-1][1] != v:
                tr[c].append((t, v))

print("\nchannel  transitions  final-level")
for c in CH:
    final = tr[c][-1][1] if tr[c] else '?'
    print(f"  Ch{c}:   {len(tr[c]):>10}     {final}")
