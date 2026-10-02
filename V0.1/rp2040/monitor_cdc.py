#!/usr/bin/env python3
"""Monitor the Pico's USB CDC and decode T_CSI_BYTE / T_EVENT frames.

Usage: python monitor_cdc.py [PORT] [seconds]
Default: COM19, 15 s.
"""
import sys
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM19"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0


def crc8(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


CMD = {
    0x50: 'cnt0', 0x51: 'cnt1', 0x52: 'cnt2', 0x53: 'cnt3',
    0x80: 'ack', 0x81: 'ack-knit',
    0x90: 'ROWCODE0', 0x91: 'ROWCODE1', 0x92: 'ROWCODE2',
    0xA0: 'NEEDLE0', 0xA1: 'NEEDLE1',
    0xB0: 'START', 0xB1: 'row-adv', 0xB2: 'row-back',
    0xE0: 'done', 0xE1: 'idle', 0xE2: 'sync',
}
EV = {0x01: 'START', 0x02: 'ROW_ADV', 0x03: 'ROW_BACK', 0x04: 'COUNTER', 0x05: 'ABORT'}

try:
    ser = serial.Serial(PORT, 115200, timeout=0.05)
except Exception as e:
    print(f"cannot open {PORT}: {e}")
    sys.exit(1)

print(f"listening on {PORT} for {DURATION:.0f}s ...", flush=True)
buf = bytearray()
frames = 0
start = time.time()
while time.time() - start < DURATION:
    data = ser.read(2048)
    if data:
        buf.extend(data)
        while True:
            i = buf.find(0xAA)
            if i < 0:
                if len(buf) > 2:
                    del buf[:-1]   # keep a possible trailing SYNC byte
                break
            if i > 0:
                del buf[:i]
            if len(buf) < 3:
                break
            ln = buf[2]
            need = 4 + ln
            if len(buf) < need:
                break
            fr = bytes(buf[:need])
            del buf[:need]
            payload = fr[3:3 + ln]
            if crc8(fr[1:1 + 2 + ln]) != fr[-1]:
                continue
            frames += 1
            t = fr[1]
            if t == 0x01 and payload:
                b = payload[0]
                print(f"  BYTE {b:02X}  {CMD.get(b, '')}", flush=True)
            elif t == 0x03 and payload:
                ev = payload[0]
                arg = payload[1] if len(payload) > 1 else -1
                print(f"  EVENT {EV.get(ev, hex(ev))} arg={arg:02X}", flush=True)
            else:
                print(f"  type={t:02X} payload={payload.hex()}", flush=True)

print(f"[done] {frames} frames", flush=True)
ser.close()
