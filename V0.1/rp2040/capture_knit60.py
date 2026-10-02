#!/usr/bin/env python3
"""60 s Saleae capture of the machine link while knitting, then decode every
byte transaction from the CSV.

Channel map (0-based CSV): DIN=0 (active-low, machine->CB1), SCK=1,
CS=3 (active-low), DOUT=5 (active-high, CB1->machine).

Decode: for each CS-low window, sample DIN on each SCK FALLING edge (active
low -> bit=1 when low) and DOUT on each SCK RISING edge.  MSB first on both.

Usage: python capture_knit60.py
"""
import os
import csv
from saleae import automation

OUT = r"c:\esp32\cb1\_capture"
DUR = 60.0
CH = [0, 1, 3, 5]          # DIN, SCK, CS, DOUT
RATE = 500_000             # 0.5 MHz: ~28 samples per SCK half-period

os.makedirs(OUT, exist_ok=True)

with automation.Manager.connect(port=10430) as mgr:
    cfg = automation.LogicDeviceConfiguration(
        enabled_digital_channels=CH, digital_sample_rate=RATE)
    cap = automation.CaptureConfiguration(
        capture_mode=automation.TimedCaptureMode(duration_seconds=DUR))
    with mgr.start_capture(device_configuration=cfg,
                           capture_configuration=cap) as capture:
        print(f"capturing {DUR:.0f}s (DIN/SCK/CS/DOUT @ {RATE/1e3:.0f} kHz) ...",
              flush=True)
        capture.wait()
        print("exporting CSV ...", flush=True)
        capture.export_raw_data_csv(directory=OUT, digital_channels=CH)

csvpath = os.path.join(OUT, 'digital.csv')

# Build per-channel transition lists.
tr = {c: [] for c in CH}
with open(csvpath) as f:
    rd = csv.reader(f)
    next(rd)
    for row in rd:
        t = float(row[0])
        for j, c in enumerate(CH):
            v = int(row[j + 1])
            if not tr[c] or tr[c][-1][1] != v:
                tr[c].append((t, v))

din, sck, cs, dout = tr[0], tr[1], tr[3], tr[5]
print(f"\ntransitions: DIN={len(din)} SCK={len(sck)} CS={len(cs)} DOUT={len(dout)}")

# Merge all transitions into a single timeline of events for sampling.
events = []
for t, v in din:
    events.append((t, 'din', v))
for t, v in sck:
    events.append((t, 'sck', v))
for t, v in cs:
    events.append((t, 'cs', v))
for t, v in dout:
    events.append((t, 'dout', v))
events.sort(key=lambda e: e[0])


def level_at(sig, t):
    a = tr[sig]
    lo, hi = 0, len(a) - 1
    while lo < hi:
        m = (lo + hi + 1) // 2
        if a[m][0] <= t:
            lo = m
        else:
            hi = m - 1
    return a[lo][1]


def val_at(t):
    """(cs, sck, din, dout) levels just after time t."""
    return (level_at(3, t), level_at(1, t), level_at(0, t), level_at(5, t))


# Find CS falling edges (start of a byte window) and rising edges (end).
cs_falls = [t for t, v in cs if v == 0]
cs_rises = [t for t, v in cs if v == 1]

transactions = []
i = 0
for ft in cs_falls:
    rt = None
    # find next rise after ft
    for r in cs_rises:
        if r > ft:
            rt = r
            break
    if rt is None:
        rt = ft + 0.01
    # collect SCK falling/rising edges inside [ft, rt]
    f_edges = [t for t, v in sck if v == 0 and ft < t < rt]
    r_edges = [t for t, v in sck if v == 1 and ft < t < rt]
    if len(f_edges) < 8:
        transactions.append((ft, rt, None, None, len(f_edges)))
        continue
    # DIN sampled on each falling edge (first 8), active-low
    master = 0
    for fe in f_edges[:8]:
        bit = 0 if level_at(0, fe) else 1      # DIN low -> 1
        master = (master << 1) | bit
    # DOUT sampled on each rising edge (first 8), active-high
    reply = 0
    for re_ in r_edges[:8]:
        bit = level_at(5, re_)
        reply = (reply << 1) | bit
    transactions.append((ft, rt, master, reply, len(f_edges)))
    i += 1

print(f"\nCS windows: {len(cs_falls)}   decoded transactions: "
      f"{sum(1 for x in transactions if x[2] is not None)}")

# Compact, human-readable dump: one line per transaction.
print("\n--- transaction decode (M=master->CB1, R=CB1->master) ---")
n = 0
for ft, rt, m, r, nclk in transactions:
    tag = ""
    if m is not None:
        if m == 0x80:
            tag = "KEEP-ALIVE"
        elif m == 0x81:
            tag = "KEEP-ALIVE(knit?)"
        elif 0x50 <= m <= 0x53:
            tag = "COUNTER"
        elif 0x90 <= m <= 0x92:
            tag = "ROWCODE"
        elif m in (0xA0, 0xA1):
            tag = "NEEDLE"
        elif 0xB0 <= m <= 0xB8:
            tag = "DISPATCH"
        elif m <= 0x4F:
            tag = "CONFIG"
    if m is not None:
        print(f"[{ft:7.4f}s] M={m:02X} R={r:02X}  {tag}")
    else:
        print(f"[{ft:7.4f}s] <short window {nclk} clk>")
    n += 1

# Highlight the knitting cycle commands.
print("\n--- knitting-cycle commands ---")
for ft, rt, m, r, nclk in transactions:
    if m in (0xB0, 0xB1, 0xB2, 0xA0, 0xA1, 0x91, 0x92):
        print(f"[{ft:7.4f}s] M={m:02X} R={r:02X}")

print(f"\nfull decode written to {csvpath}")
