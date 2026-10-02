#!/usr/bin/env python3
"""Capture the machine-link wire with the Saleae and analyze the handshake.

Channel map (0-based CSV): DIN=0, SCK=1, CS=3, DOUT=5.

Usage: python capture_handshake.py [seconds]
"""
import os
import sys
import csv
import traceback
from saleae import automation

OUT = r"c:\esp32\cb1\_capture"
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
CH = [0, 1, 3, 5]   # DIN, SCK, CS, DOUT

os.makedirs(OUT, exist_ok=True)

with automation.Manager.connect(port=10430) as mgr:
    cfg = automation.LogicDeviceConfiguration(
        enabled_digital_channels=CH, digital_sample_rate=1_000_000)
    cap = automation.CaptureConfiguration(
        capture_mode=automation.TimedCaptureMode(duration_seconds=DUR))
    with mgr.start_capture(device_configuration=cfg,
                           capture_configuration=cap) as capture:
        print(f"capturing {DUR:.0f}s ...", flush=True)
        capture.wait()
        print("capture complete, exporting ...", flush=True)
        capture.export_raw_data_csv(directory=OUT, digital_channels=CH)

csvpath = os.path.join(OUT, 'digital.csv')
tr = {c: [] for c in CH}
with open(csvpath) as f:
    rd = csv.reader(f)
    next(rd)                       # header
    for row in rd:
        t = float(row[0])
        for j, c in enumerate(CH):
            v = int(row[j + 1])
            if not tr[c] or tr[c][-1][1] != v:
                tr[c].append((t, v))


def level(c, t):
    a = tr[c]
    lo, hi = 0, len(a) - 1
    while lo < hi:
        m = (lo + hi + 1) // 2
        if a[m][0] <= t:
            lo = m
        else:
            hi = m - 1
    return a[lo][1]


print(f"\nchannel transitions: DIN={len(tr[0])} SCK={len(tr[1])} "
      f"CS={len(tr[3])} DOUT={len(tr[5])}")

falls = [t for t, v in tr[3] if v == 0]
rises = [t for t, v in tr[3] if v == 1]
print(f"CS-low attention windows: {len(falls)}")

acked = 0
for ft in falls:
    rt = next((t for t in rises if t > ft), None)
    if rt is None:
        rt = ft + 5.0
    sck_high = any(v == 1 and ft < t < rt for t, v in tr[1])
    din_high = any(v == 1 and ft < t < rt for t, v in tr[0])
    dur_ms = (rt - ft) * 1000
    print(f"  CS {ft:.4f}s -> {rt:.4f}s ({dur_ms:.0f}ms): "
          f"SCK-high={sck_high}  DIN-high={din_high}")
    if sck_high:
        acked += 1

print(f"\nwindows acknowledged (SCK went high): {acked}/{len(falls)}")

sck_rising = sum(1 for i in range(1, len(tr[1])) if tr[1][i][1] == 1)
print(f"total SCK rising edges: {sck_rising}  "
      f"(>0 with pulses = machine is clocking = link up)")
