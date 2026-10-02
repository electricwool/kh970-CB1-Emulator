#!/usr/bin/env python3
"""CSI (3-wire serial) model + 4K FIFO + transport framing for the CB-1.

The CB-1 is the CSI *master* on the KH-970 machine link (see machine_link.md):

    P2.1 = CS  (attention, machine-driven)          -> INTP0  (slot 3)
    P3.2 = SCK (clock, CB-1-driven — the master)
    P3.3 = MOSI (CB-1 -> machine data out)
    P2.7 = MISO (machine -> CB-1 data in)
    CSIM = 0xFF80, SIO = 0xFF86                      -> INTCSI (slot 19)

Every wire byte is a full-duplex 8-bit shift: the master clocks its byte in
while the slave shifts out the byte it staged in SIO.  The firmware runs that
entirely through CSIM/SIO and the INTP0/INTCSI interrupts, so the emulator
models the peripheral at *byte* level (not at SCK level):

    master byte M -> CS edge fires INTP0 -> p0_irq_handler stages reply R,
                     arms CSIM -> shift: SIO <- M, reply <- R -> INTCSI fires
                     -> csi_irq_handler reads SIO (=M) and advances state.

This module provides:

    Fifo4K        thread-safe 4096-byte FIFO (the decoupling buffer)
    crc8, Frame   framed record codec for the byte-stream transport
                  (WebSerial / USB CDC are stream oriented, no framing)
    CSIPeripheral the register/byte-exchange model described above

The 1:1 ``exchange()`` is the *prototype* byte pipe.  The production path
(see CSI_BRIDGE.md) offloads the transaction state machine to the RP2040 and
uses STATE/EVENT records; this model then serves as the reference to verify
the ported state machine against the real ROM.
"""
import collections
import threading

# ---- interrupt wiring -----------------------------------------------------
INTP0_SLOT = 3        # vector 0x0006   (P2.1 chip-select edge)
INTCSI_SLOT = 19      # vector 0x0026   (CSI byte shift complete)
IF0L = 0xFFE0         # IF0l : bit0 = INTP0 request flag
IF0H = 0xFFE1         # IF0h : bit7 = INTCSI request flag
MK0L = 0xFFE4         # MK0l : bit0 = INTP0 mask
MK0H = 0xFFE5         # MK0h : bit7 = INTCSI mask
INTP0_BIT = 0
INTCSI_BIT = 7


class Fifo4K:
    """Thread-safe byte FIFO with a hard 4096-byte capacity.

    On overflow the *oldest* bytes are dropped so a slow consumer loses
    history but never wedges the producer.  All operations are O(1).
    """

    def __init__(self, capacity=4096):
        self.cap = capacity
        self._q = collections.deque()
        self._n = 0
        self.lock = threading.Lock()

    def write(self, data):
        """Append bytes; returns the number of bytes dropped (if overflow)."""
        dropped = 0
        with self.lock:
            for b in data:
                self._q.append(b)
                self._n += 1
            while self._n > self.cap:
                self._q.popleft()
                self._n -= 1
                dropped += 1
        return dropped

    def drain(self):
        """Atomically remove and return all buffered bytes."""
        with self.lock:
            out = bytes(self._q)
            self._q.clear()
            self._n = 0
        return out

    def __len__(self):
        with self.lock:
            return self._n

    @property
    def space(self):
        with self.lock:
            return self.cap - self._n

    @property
    def high_water(self):
        """True when the FIFO is >= 75% full (skip optional refreshes)."""
        with self.lock:
            return self._n >= self.cap * 3 // 4


# ---- transport framing ----------------------------------------------------
SYNC = 0xAA

# record types
T_CSI_BYTE = 0x01   # bridge -> emulator: captured master byte
T_REPLY = 0x02      # emulator -> bridge: reply byte to stage
T_EVENT = 0x03
T_STATE = 0x04
T_PIN = 0x05        # both directions: [pin, level]

# event codes
EV_START = 0x01
EV_ROW_ADV = 0x02
EV_ROW_BACK = 0x03
EV_COUNTER = 0x04
EV_ABORT = 0x05
EV_BOOT = 0x06


def crc8(data):
    """CRC-8 (poly 0x07, init 0x00, MSB first, no reflection)."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def encode_frame(ftype, payload):
    """Encode one record: AA type len payload... crc8."""
    payload = bytes(payload)
    head = bytes((SYNC, ftype, len(payload)))
    return head + payload + bytes((crc8(head[1:] + payload),))


class FrameDecoder:
    """Incremental deframer: feed() returns a list of (type, payload)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while True:
            f = self._extract()
            if f is None:
                break
            out.append(f)
        return out

    def _extract(self):
        # resync: drop until SYNC
        i = self.buf.find(SYNC)
        if i < 0:
            if len(self.buf) > 0 and self.buf[-1] != SYNC:
                # keep only a trailing possible-SYNC byte
                self.buf = self.buf[-1:] if self.buf[-1] == SYNC else bytearray()
            else:
                self.buf.clear()
            return None
        if i > 0:
            del self.buf[:i]
        if len(self.buf) < 3:                     # SYNC type len
            return None
        ftype = self.buf[1]
        length = self.buf[2]
        need = 4 + length                          # SYNC type len payload crc
        if len(self.buf) < need:
            return None
        frame = bytes(self.buf[:need])
        del self.buf[:need]
        payload = frame[3:3 + length]
        if crc8(frame[1:1 + 2 + length]) != frame[-1]:
            return None                            # bad CRC -> drop, keep scanning
        return ftype, payload


# ---- transaction -> STATE-record collector -------------------------------
# The production path offloads the wire protocol to the RP2040 (autonomous
# csi_step in rp2040/csi_bridge.c).  The emulator stays the source of truth:
# as it runs the real ROM handlers for each monitored master byte, it re-emits
# the bytes the ROM *served* as T_STATE records so the RP2040's cache stays
# warm.  Data commands only: 0x50-0x53 (counter), 0x90-0x92 (row code),
# 0xA0/0xA1 (needle row).
class CsiStateCollector:
    DATA_CMDS = set(range(0x50, 0x54)) | set(range(0x90, 0x93)) | {0xA0, 0xA1}

    def __init__(self):
        self.reset()

    def reset(self):
        self.cmd = None
        self.echoed = False
        self.data = bytearray()

    def feed(self, master, reply):
        """Track one (master byte, reply byte) exchange.  Returns a
        ``(selector, payload)`` tuple when a data transaction completes,
        else ``None``."""
        if self.cmd is None:
            self.cmd = master                      # byte 1: the command
            return None
        if not self.echoed:
            if reply == self.cmd:                  # byte 2: sync -> echo
                self.echoed = True
                return None
            self.reset()                            # unexpected byte -> resync
            return None
        if reply == 0xE1:                          # final: done marker
            sel, payload = self.cmd, bytes(self.data)
            self.reset()
            return (sel, payload) if sel in self.DATA_CMDS else None
        self.data.append(reply)                    # data byte served
        return None


# ---- CSI peripheral model -------------------------------------------------
class CSIPeripheral:
    """Byte-level CSI slave model; drives the real ROM's CSI handlers.

    Attach to a :class:`emu.machine.Machine`; route CSIM/SIO SFR accesses
    through ``write_csim``/``write_sio``/``read_sio`` (see bus.py) and route
    P2.1 (CS) through ``cs_low`` in ``Machine.read_p2``.
    """

    # pin ids carried in T_PIN records (must match csi32u4.ino)
    PIN_CS = 0      # bridge -> emulator (input)
    PIN_DIN = 1     # bridge -> emulator (input)
    PIN_DOUT = 2    # emulator -> bridge (output)
    PIN_SCK = 3     # emulator -> bridge (output)

    def __init__(self, machine):
        self.m = machine
        self.csim = 0          # last CSIM value written
        self.sio = 0           # SIO shift register (staged out byte / shifted-in byte)
        self.cs_low = False    # P2.1 level driven by the machine (bridge-reported)
        self.din_high = True   # P2.7 MISO level (machine -> CB-1), idle high
        self._pending = {INTP0_SLOT: False, INTCSI_SLOT: False}
        self.trace = []        # list of ('>', byte) master, ('<', byte) reply
        self.pin_out = []      # [(pin_id, level)] commands for the bridge
        self.pin_log = []      # history of emitted pin commands (diagnostic)
        self._last_pin = {}    # dedup for pin_out
        self._last_reply = 0   # reply staged by the last cs_fall()

    # ---- bridge-reported pin levels (the relay drives these) -------------
    def set_cs(self, low):
        """Machine's CS level (P2.1).  ``low`` True = asserted."""
        self.cs_low = bool(low)

    def set_din(self, high):
        """Machine's DIN level (P2.7, active-low data).  ``high`` True = idle."""
        self.din_high = bool(high)

    # ---- ROM P3 write -> bridge pin commands -----------------------------
    def _emit_pin(self, pin, level):
        if self._last_pin.get(pin) != level:
            self._last_pin[pin] = level
            self.pin_out.append((pin, level))
            self.pin_log.append((pin, level))
            self.pin_log = self.pin_log[-40:]

    def set_p3(self, old, new):
        """ROM wrote P3.  Translate SCK (P3.2) / MOSI (P3.3) bit changes into
        bridge pin commands.  SET1 P3.2 is the handshake ack (drive SCK high);
        SET1 P3.3 arms the link (raise DOUT) and, once armed, the CB-1 releases
        SCK so the machine can clock."""
        old_b2 = (old >> 2) & 1
        new_b2 = (new >> 2) & 1
        old_b3 = (old >> 3) & 1
        new_b3 = (new >> 3) & 1
        if new_b2 != old_b2 and new_b2:
            self._emit_pin(self.PIN_SCK, 1)      # drive SCK high (ack)
        if new_b3 != old_b3:
            self._emit_pin(self.PIN_DOUT, new_b3)
            if new_b3:                            # armed -> give SCK back to the machine
                self._emit_pin(self.PIN_SCK, 0)

    def drain_pins(self):
        """Atomically remove and return pending bridge pin commands."""
        out = self.pin_out
        self.pin_out = []
        return out

    def reset(self):
        """Reset the peripheral on a CPU reset.  Preserves ``cs_low`` /
        ``din_high`` (the live wire levels) so a re-run of the boot handshake
        sees the machine, but clears the transaction/pin state."""
        self.csim = 0
        self.sio = 0
        self._pending = {INTP0_SLOT: False, INTCSI_SLOT: False}
        self.trace = []
        self.pin_out = []
        self.pin_log = []
        self._last_pin = {}
        self._last_reply = 0

    # ---- register view ---------------------------------------------------
    @property
    def armed(self):
        return bool(self.csim & 0x80)          # CSIM bit7 = operation enabled

    def write_csim(self, v):
        self.csim = v & 0xFF
        if v & 0x80:                            # armed -> CSI hardware owns SCK
            self._emit_pin(self.PIN_SCK, 0)

    def write_sio(self, v):
        self.sio = v & 0xFF                    # stage/overwrite the shift register

    def read_sio(self):
        return self.sio                        # current shift register contents

    # ---- interrupt gating ------------------------------------------------
    def _masked(self, slot):
        sfr = self.m.bus.sfr
        if slot == INTP0_SLOT:
            return bool(sfr[MK0L] & (1 << INTP0_BIT))
        return bool(sfr[MK0H] & (1 << INTCSI_BIT))

    def _signal(self, slot):
        self._pending[slot] = True
        return self._maybe_fire(slot)

    def _maybe_fire(self, slot):
        # Only deliver when unmasked AND the CPU's IE flag is set; otherwise
        # keep the request pending (request_int silently no-ops on IE==0).
        # Returns True when the interrupt was actually delivered.
        if self._pending[slot] and not self._masked(slot):
            if self.m.cpu.psw & 0x80:
                self._pending[slot] = False
                self.m.cpu.request_int(slot)
                return True
        return False

    def on_mk_write(self):
        """Firmware unmasked an interrupt; fire anything that was pending."""
        self._maybe_fire(INTP0_SLOT)
        self._maybe_fire(INTCSI_SLOT)

    # ---- byte exchange ---------------------------------------------------
    def _settle(self, budget=20000):
        """Advance the CPU until interrupts are enabled (complete any in-flight
        ISR, e.g. the INTC10 tick) so an interrupt signal below is actually
        delivered instead of just pending.  This is what makes the CSI exchange
        independent of the throttled main loop / tick timing."""
        for _ in range(budget):
            if self.m.cpu.psw & 0x80:
                return
            self.m.step()

    def cs_fall(self, steps=8000):
        """CS falling edge.  Runs INTP0 so p0_irq_handler stages the reply and
        arms CSIM.  Returns the reply byte that must be presented on DOUT.

        The INTP0 handler debounces P2.1 and requires it *released* (high) when
        it runs, so once the interrupt is actually delivered (INTP0 unmasked,
        i.e. the byte protocol phase) we release CS like the machine does right
        after the edge.  During the boot handshake INTP0 is still masked, so
        the signal pends and ``cs_low`` is left untouched for the reset code's
        P2.1 polling."""
        self._settle()
        if self._signal(INTP0_SLOT):
            self.cs_low = False
        self._run_until_armed(steps)
        self._last_reply = self.sio
        return self.sio

    def byte_received(self, master_byte, steps=8000):
        """Master byte clocked in.  Runs INTCSI so csi_irq_handler reads SIO
        and advances the transaction state."""
        m = master_byte & 0xFF
        self._settle()
        self.trace.append(('>', m))
        self.trace.append(('<', self._last_reply))
        self.sio = m
        self._signal(INTCSI_SLOT)
        self._run(steps)

    def exchange(self, master_byte, steps=8000):
        """One full-duplex byte (prototype/loopback helper).  ``master_byte``
        is clocked in; returns the byte the CB-1 shifted out."""
        self.cs_low = True
        reply = self.cs_fall(steps)
        self.cs_low = False
        self.byte_received(master_byte, steps)
        return reply

    def _run_until_armed(self, budget):
        """Run until p0_irq_handler arms CSIM (bit7 set), then finish the
        handler (POP P6 / POP PM6 / CLR1 MK0h.7 / RETI) so the main loop does
        not run between the INTP0 and INTCSI phases."""
        for _ in range(budget):
            self.m.step()
            if self.csim & 0x80:
                for _ in range(16):            # let the handler RETI cleanly
                    self.m.step()
                return

    def _run(self, n):
        """Step until the pending handler RETIs (IE restored) or the budget is
        exhausted.  This runs only as long as the handler actually needs, so the
        exchange completes at full speed instead of a fixed 8000-step burn."""
        for _ in range(n):
            self.m.step()
            if self.m.cpu.psw & 0x80:
                return
