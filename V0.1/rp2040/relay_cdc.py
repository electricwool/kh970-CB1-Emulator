#!/usr/bin/env python3
"""Headless relay: bridge the RP2040 USB CDC (COM19) to the emulator backend
(serve.py on http://127.0.0.1:8765). Mirrors web/index.html's WebSerial loop
without a browser.

Usage: python relay_cdc.py [PORT] [BASE_URL]
"""
import sys
import time
import threading
import serial
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM19"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8765"


def crc8(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def post(path, payload):
    try:
        req = urllib.request.Request(BASE + path, data=bytes(payload), method='POST')
        urllib.request.urlopen(req, timeout=1)
    except Exception:
        pass


def get_bytes(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=1) as r:
            return r.read()
    except Exception:
        return b''


try:
    ser = serial.Serial(PORT, 115200, timeout=0.05)
except Exception as e:
    print(f"cannot open {PORT}: {e}")
    sys.exit(1)

print(f"relay: {PORT} <-> {BASE}", flush=True)
stop = threading.Event()


def rx_loop():
    buf = bytearray()
    n = 0
    while not stop.is_set():
        try:
            data = ser.read(2048)
        except Exception as e:
            print("rx err", e)
            break
        if data:
            buf.extend(data)
            while True:
                i = buf.find(0xAA)
                if i < 0:
                    if len(buf) > 2:
                        del buf[:-1]
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
                n += 1
                t = fr[1]
                if t == 0x01:      # T_CSI_BYTE -> emulator
                    post('/csi_rx', payload)
                elif t == 0x05:    # T_PIN -> emulator
                    post('/csi_pin', payload)
                elif t == 0x03:    # T_EVENT -> emulator
                    post('/csi_event', payload)


def tx_loop():
    while not stop.is_set():
        b = get_bytes('/csi_tx')
        if b:
            try:
                ser.write(b)
            except Exception as e:
                print("tx err", e)
                break
        s = get_bytes('/csi_state')
        if s:
            try:
                ser.write(s)
            except Exception as e:
                print("tx err", e)
                break
        if not b and not s:
            time.sleep(0.002)


t1 = threading.Thread(target=rx_loop, daemon=True)
t2 = threading.Thread(target=tx_loop, daemon=True)
t1.start()
t2.start()
print("relay running; Ctrl-C to stop", flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    stop.set()
