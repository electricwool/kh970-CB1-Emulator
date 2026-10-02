#!/usr/bin/env python3
"""Manual handshake over USB CDC: drive the RP2040 pins step by step.

The firmware (MANUAL_HANDSHAKE=1) accepts single-byte commands:
  'P' -> replies with one raw byte (bit0=SCK bit1=CS bit2=DIN bit3=DOUT)
  'H' -> SCK high, 'L' -> SCK release, 'O' -> DOUT high, 'I' -> DOUT low

Usage: python manual_handshake.py [PORT]
"""
import sys
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM19"


def open_serial():
    last = None
    for _ in range(40):
        try:
            return serial.Serial(PORT, 115200, timeout=0.2)
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"cannot open {PORT}: {last}")


def cmd(ser, c):
    ser.write(c.encode('ascii'))
    ser.flush()


def poll(ser):
    ser.reset_input_buffer()
    cmd(ser, 'P')
    time.sleep(0.05)
    b = ser.read(16)
    return b[-1] if b else None


def fmt(p):
    return f"SCK={(p >> 0) & 1} CS={(p >> 1) & 1} DIN={(p >> 2) & 1} DOUT={(p >> 3) & 1}"


ser = open_serial()
print(f"opened {PORT}", flush=True)

# Step 1: wait for CS low (machine attention)
print("Step 1: waiting for CS low (power-cycle the machine now) ...", flush=True)
deadline = time.time() + 90
seen_cs = False
while time.time() < deadline:
    p = poll(ser)
    if p is not None:
        print(f"  {fmt(p)}", flush=True)
        if (p >> 1) & 1 == 0:
            print("  *** CS LOW detected ***", flush=True)
            seen_cs = True
            break
    time.sleep(0.1)

if not seen_cs:
    print("timeout: CS never went low", flush=True)
    sys.exit(1)

# Step 2: drive SCK high (the ack)
print("Step 2: driving SCK high ('H')", flush=True)
cmd(ser, 'H')
time.sleep(0.05)
p = poll(ser)
print(f"  after ack: {fmt(p)}", flush=True)

# Step 3: wait for DIN high
print("Step 3: waiting for DIN high ...", flush=True)
deadline = time.time() + 5
seen_din = False
while time.time() < deadline:
    p = poll(ser)
    if p is not None:
        print(f"  {fmt(p)}", flush=True)
        if (p >> 2) & 1 == 1:
            print("  *** DIN HIGH detected ***", flush=True)
            seen_din = True
            break
    time.sleep(0.05)

# Step 4: drive DOUT high
print("Step 4: driving DOUT high ('O')", flush=True)
cmd(ser, 'O')
time.sleep(0.05)
p = poll(ser)
print(f"  after arm: {fmt(p)}", flush=True)

print("handshake steps complete — polling for 8 s to observe ...", flush=True)
deadline = time.time() + 8
while time.time() < deadline:
    p = poll(ser)
    if p is not None:
        print(f"  {fmt(p)}", flush=True)
    time.sleep(0.2)

ser.close()
print("done", flush=True)
