#!/usr/bin/env python3
"""Decode the manually-captured Saleae CSV (8 channels) into CSI transactions.

Channel map (0-based CSV columns, from rp2040/PINOUT.md):
  Channel 0 = DIN   (machine -> CB1, ACTIVE-LOW, sampled on SCK falling edge)
  Channel 1 = SCK   (clock, idle low)
  Channel 3 = CS    (attention, ACTIVE-LOW)
  Channel 5 = DOUT  (CB1 -> machine, active-high, sampled on SCK rising edge)
"""
import csv
import sys

path = sys.argv[1] if len(sys.argv) > 1 else r"captures\digital.csv"

tr = {c: [] for c in (0, 1, 3, 5)}
with open(path) as f:
    rd = csv.reader(f)
    next(rd)
    for row in rd:
        t = float(row[0])
        for c in (0, 1, 3, 5):
            v = int(row[c + 1])
            if not tr[c] or tr[c][-1][1] != v:
                tr[c].append((t, v))

din, sck, cs, dout = tr[0], tr[1], tr[3], tr[5]
print(f"transitions: DIN={len(din)} SCK={len(sck)} CS={len(cs)} DOUT={len(dout)}")

# sample period
dts = [sck[i+1][0]-sck[i][0] for i in range(len(sck)-1)]
if dts:
    dts.sort()
    print(f"SCK edge spacing median {dts[len(dts)//2]*1e6:.1f} us, "
          f"min {dts[0]*1e6:.1f} us, max {dts[-1]*1e6:.1f} us")


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


cs_falls = [t for t, v in cs if v == 0]
cs_rises = [t for t, v in cs if v == 1]

transactions = []
for ft in cs_falls:
    rt = next((t for t in cs_rises if t > ft), ft + 0.01)
    f_edges = [t for t, v in sck if v == 0 and ft < t < rt]
    r_edges = [t for t, v in sck if v == 1 and ft < t < rt]
    master = reply = None
    if len(f_edges) >= 8:
        m = 0
        for fe in f_edges[:8]:
            m = (m << 1) | (0 if level_at(0, fe) else 1)
        master = m
    if len(r_edges) >= 8:
        r = 0
        for re_ in r_edges[:8]:
            r = (r << 1) | level_at(5, re_)
        reply = r
    transactions.append((ft, rt, master, reply, len(f_edges), len(r_edges)))

ok = sum(1 for x in transactions if x[2] is not None)
print(f"\nCS windows: {len(cs_falls)}   decoded: {ok}")

TAGS = {}
def tag(m):
    if m is None: return ''
    if m == 0x80: return 'KEEP80'
    if m == 0x81: return 'KEEP81'
    if 0x50 <= m <= 0x53: return 'COUNTER'
    if 0x90 <= m <= 0x92: return 'ROWCODE'
    if m in (0xA0, 0xA1): return 'NEEDLE'
    if 0xB0 <= m <= 0xB8: return 'DISPATCH'
    if m <= 0x4F: return 'CONFIG'
    if 0x60 <= m <= 0x6D: return 'SENSOR'
    if 0xC0 <= m <= 0xCD: return 'TRI'
    return '?'

print("\n--- all transactions ---")
for ft, rt, m, r, nf, nr in transactions:
    if m is None:
        print(f"[{ft:7.4f}s] short: {nf}f/{nr}r clk")
    else:
        print(f"[{ft:7.4f}s] M={m:02X} R={r:02X}  {tag(m)}")

print("\n--- knitting-cycle commands ---")
for ft, rt, m, r, nf, nr in transactions:
    if m in (0xB0, 0xB1, 0xB2, 0xA0, 0xA1, 0x91, 0x92):
        print(f"[{ft:7.4f}s] M={m:02X} R={r:02X}  {tag(m)}")
