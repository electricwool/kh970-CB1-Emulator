#!/usr/bin/env python3
"""
CB-1 emulator backend: runs the 78K/II machine and serves the LCD + keypad +
serial to the web canvas page over HTTP (polling) — no external dependencies.

Endpoints:
  GET  /             -> web/index.html
  GET  /frame        -> {"pixels": base64(122*64 bytes 0/1)}
  POST /key?k=NAME   -> key down;  /key?k=NAME&up=1 -> key up
  POST /uart_rx      -> body bytes injected into UART Rx (RXB + ASIS flag)
  GET  /uart_tx      -> drain pending UART Tx bytes as JSON list
  POST /csi_rx       -> body bytes injected into CSI (SIO)
  GET  /csi_state    -> framed T_STATE records (needle row / row code / counter)
  POST /csi_event    -> event record from the RP2040 (start / row advance / ...)

Flags:
  --diag             boot straight into the hardware diagnostic menu
  --speed N          emulated steps per second (default 144000 ~= 5x slower
                     than the unthrottled ~722k steps/sec)
  --png PATH         periodically save the live framebuffer
  --no-browser       don't auto-open the web UI
"""
import base64
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from emu.machine import Machine
from emu.lcdpng import save_png
from emu.csi import Fifo4K, CsiStateCollector, encode_frame, T_STATE, T_REPLY, T_PIN

ROM = 'kh970CB1v1.0-AM27C040@DIP32.bin'
WEB_INDEX = os.path.join('web', 'index.html')
# Emulated steps per second. Unthrottled the interpreter does ~722k steps/sec;
# the default is 5x slower so on-screen timing is close to real time.
DEFAULT_SPEED = 144_000

# Key-pulse lengths (a single click of any length -> exactly one key press).
#
# The firmware reads keys in two very different ways:
#  * Menu loops poll the keypad/arrows in a tight ~10.8k-step cycle, but after
#    every arrow press they redraw the menu (~135k steps) during which keys are
#    NOT polled.  So arrows must be held ~1 full menu cycle (~144k steps) or a
#    click can land inside the redraw and be lost.
#  * Numeric-entry / pattern-editor loops poll in a fast ~16k-step cycle with no
#    big redraw, so a long pulse there re-registers the key several times
#    (auto-repeat: one click entered "111" instead of "1").
#
# Therefore the pulse length is split by key type: arrows get the long pulse,
# keypad keys (digits and function keys) get a short pulse that survives the
# key-scan debounce (~6k steps) but expires before the next poll (~16k steps).
ARROW_PULSE_STEPS = 144_000
KEYPAD_PULSE_STEPS = 25_000
_ARROW_KEYS = {'UP', 'DOWN', 'LEFT', 'RIGHT'}


def resource_path(rel):
    """Resolve a data file that is bundled into the executable.

    PyInstaller onefile extracts bundled files to sys._MEIPASS at runtime;
    running from source uses the script's own directory.
    """
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class Backend:
    def __init__(self, boot_diag=False, png_path=None, speed=DEFAULT_SPEED):
        # boot_diag: jump straight into the hardware diagnostic menu at power-up
        # png_path: if set, the live framebuffer is saved here periodically
        # speed: emulated steps per second (real-time throttle)
        self.m = Machine(resource_path(ROM), boot_diag=boot_diag)
        self.sps = max(1, int(speed))
        self.lock = threading.Lock()
        self.fb = bytearray(122 * 32)
        self.png_path = png_path
        self._steps = 0                # emulated step counter
        self._key_release = {}         # key -> step at which to auto-release
        self.keys = []                 # pending key events
        self.uart_rx = bytearray()
        self.uart_tx = bytearray()
        self.csi_in_fifo = Fifo4K()          # bridge -> emulator records (tagged)
        self.csi_tx_fifo = Fifo4K()          # emulator -> bridge framed records
        self.csi_state_fifo = Fifo4K()       # emulator -> RP2040 STATE records
        self.csi_collector = CsiStateCollector()
        self.csi_events = []                 # recent event records (log)
        self._txn_byte_low = None            # last HEV_BYTES low-byte seen
        self._txn_count = 0                  # reconstructed valid-transaction count
        self._last_txn_time = 0.0            # when the last valid transaction completed
        self._last_fe7e = None               # last observed expected-ack value
        self._diag_active = False            # a diagnostic test-mode ack is on the wire
        self._machine_boot_at = 0.0          # when the machine last (re)booted (config relay)
        self.csi_pin_n = 0                   # inbound T_PIN records seen
        self.csi_byte_n = 0                  # inbound T_CSI_BYTE records seen
        self.csi_state_n = 0                 # outbound T_STATE records pushed
        self._last_state_push = 0.0          # last periodic state-push time
        self._bridge_pat = {}                # bridge pattern-cache debug (0x22-0x25)
        self.csi_in_log = []                 # recent inbound records (debug)
        self.csi_cmd_log = []                # recent forwarded command bytes (debug)
        self.running = True
        self.step_lock = threading.Lock()    # serialises CPU stepping (display vs CSI)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.csi_thread = threading.Thread(target=self._csi_worker, daemon=True)

    def start(self):
        self.thread.start()
        self.csi_thread.start()

    def _run(self):
        batch = 4000                       # one INTC10 timer tick per batch
        spb = batch / self.sps             # real seconds each batch must span
        frame = 0
        while self.running:
            t0 = time.perf_counter()
            with self.lock:
                self._release_keys()
                while self.keys:
                    k, down = self.keys.pop(0)
                    if down:
                        self._press(k)   # each down starts one fixed-length pulse
                # inject UART rx bytes
                if self.uart_rx:
                    self.m.bus.sfr[0xFF8C] = self.uart_rx.pop(0)   # RXB
                    self.m.bus.sfr[0xFF8A] |= 0x40                 # ASIS rx ready
            # run one timer tick's worth of instructions (serialised with the
            # CSI worker via step_lock so the two never step the CPU at once).
            with self.step_lock:
                for _ in range(batch):
                    self.m.step()
                self._steps += batch
                self.m.tick()
                # Emit the ROM's CSI pin outputs (SCK / DOUT) back to the bridge.
                for pin, lvl in self.m.csi.drain_pins():
                    self.csi_tx_fifo.write(encode_frame(T_PIN, bytes((pin, lvl))))
            with self.lock:
                self.fb = self.m.lcd.pixels()
            # throttle to the configured emulation speed
            elapsed = time.perf_counter() - t0
            if elapsed < spb:
                time.sleep(spb - elapsed)
            # periodically dump the live framebuffer for visual inspection
            if self.png_path:
                frame += 1
                if frame % 100 == 0:
                    save_png(self.png_path, self.fb, 122, 32, 4)

    def _csi_worker(self):
        """Serve the CSI machine link at full speed, decoupled from the
        throttled display loop and the INTC10 tick cadence.  Each exchange runs
        only as long as the ROM's handlers actually need (emu/csi.py exits its
        run budget when the handler RETIs), so bytes are injected/pulled
        directly into the CSI registers without waiting on the display throttle."""
        while self.running:
            data = self.csi_in_fifo.drain()
            if data:
                with self.step_lock:
                    self._serve_csi(data)
            else:
                time.sleep(0.0005)
            # Diagnostic test-mode acks (D2/D3/D4/D5) are set by menu code and
            # cannot be derived from the machine's commands, so check on every
            # pass (cheap; only writes when the ROM's fe7e changes).
            self._push_diag_fe7e()
            # Keep the bridge cache warm even while the machine idles (it only
            # sends 0x80 keep-alive polls then, which are answered autonomously
            # and never forwarded as T_CSI_BYTE).  Pushing on a slow cadence
            # means a menu edit or pattern load reaches the bridge before the
            # machine's next read, instead of one read behind.
            now = time.time()
            if now - self._last_state_push > 0.5:
                self._last_state_push = now
                # Push the FULL needle buffer BEFORE the ready-ack, so the
                # bridge has every row cached before the machine's first
                # A0/A1 read.  Records drain the FIFO in order, so the pattern
                # rows below land ahead of the 0xD2 that starts knitting.
                self._push_pattern()
                # Raise the wire ready-ack to 0xD2 once the machine's post-reboot
                # handshake has settled.  The bridge owns fe7e (0x01..0x4F -> D0,
                # 0x91 -> D1, 0xB0 -> D6) but cannot derive "ready" (D2) from the
                # machine, so push it here shortly after the boot relay.  Timing is
                # critical: the machine polls 0x80 once right after the counters
                # (expects 0xD0), then again ~287ms after boot (expects 0xD2) and
                # GIVES UP if it still sees 0xD0 — so 0xD2 must land between ~40ms
                # and ~280ms after boot, not 2.5s.  Only when a pattern is loaded
                # (fe66.2 set) — otherwise stay at 0xD0 (not ready).
                if (self._machine_boot_at and now - self._machine_boot_at > 0.07
                        and (self.m.bus.ram[0xFE66 - 0xFD00] & 0x04)):   # fe66.2
                    self.csi_state_fifo.write(
                        encode_frame(T_STATE, bytes((0x80, 0xD2))))
                    self._machine_boot_at = 0.0
                self._push_state()

    @staticmethod
    def _valuable(b):
        """True for command bytes that carry meaning.  Keep-alive polls
        (0x80/0x81) and in-band sync/marker bytes (0xE0-0xE3) are answered
        autonomously by the RP2040 and ignored here.

        Ranges (each is an independent mode, kept from the bridge's own
        is_valuable()):
          0x00-0x4F  config nibbles (fdbb..fdbf, width/version)
          0x50-0x53  counter reads
          0x60-0x6D  sensor bitmap (mem_fe87)
          0x90-0x92  row-code window
          0xA0-0xA1  needle rows
          0xB0-0xBE  dispatch commands (0xB0 start ... 0xBE tri-state mode)
          0xC0-0xCD  tri-state groups (mem_fe85/86)
        """
        return (b <= 0x4F or 0x50 <= b <= 0x53 or 0x60 <= b <= 0x6D or
                0x90 <= b <= 0x92 or b in (0xA0, 0xA1) or
                0xB0 <= b <= 0xBE or 0xC0 <= b <= 0xCD)

    def _serve_csi(self, data):
        """Decode bridge -> emulator records in wire order:
           [0x01, byte]          master byte clocked in (T_CSI_BYTE)
           [0x05, pin, level]    pin change (T_PIN)"""
        i = 0
        while i < len(data):
            t = data[i]
            if t == 0x01 and i + 1 < len(data):
                b = data[i + 1]
                # Ignore keep-alive polls and markers: the bridge answers them
                # autonomously.  Only meaningful commands reach the ROM.
                if self._valuable(b):
                    # The bridge runs the full wire transaction autonomously and
                    # forwards only the command byte.  Apply the command's state
                    # side effects to the emulator's ROM directly (the interrupt
                    # protocol replay is fragile — INTP0/INTCSI are only unmasked
                    # at specific points in the ROM's main loop), then re-sync the
                    # bridge cache.
                    self._apply_command(b)
                    self._push_state()
                    # Latch fe84.3 so the ROM's main knitting loop wakes and
                    # processes the dispatched command (data reads also set it).
                    self.m.bus.ram[0xFE84 - 0xFD00] |= 0x08
                i += 2
            elif t == 0x05 and i + 2 < len(data):
                self._handle_pin(data[i + 1], data[i + 2])
                i += 3
            else:
                break                        # malformed tail -> drop

    def _apply_command(self, cmd):
        """Apply a forwarded command's state side effects to the ROM directly.

        This mirrors what the bridge's dispatch_cmd / the ROM's sub_dd71 do on
        the wire, without replaying the interrupt protocol (the ROM's INTP0/
        INTCSI handlers only fire when the main loop has them unmasked, so the
        byte-level replay is timing-dependent).  Only the machine config relay
        (0x00-0x4F) has a boot-critical side effect: it fills fdbb-fdbf, which
        sub_3a8a turns into the carriage width.  Data reads (0x50-0x53 /
        0x90-0x92 / 0xA0-0xA1) and keep-alives (0x80/0x81) have no side
        effects here — the bridge answers them and _push_state() re-syncs the
        emulator's live SRAM to the bridge cache.
        """
        # 0x91 (row-code read): sub_dd71 lab_ded1 sets fe7e = 0xD1.  The bridge
        # mirrors this on the wire (its own dispatch_cmd) but sends NO event for
        # 0x91, so apply it here to keep the ROM's expected-ack in sync with
        # what the machine will read back on its next 0x80 keep-alive poll.
        if cmd == 0x91:
            self.m.bus.ram[0xFE7E - 0xFD00] = 0xD1
            return
        # 0xB3-0xBE: remaining sub_dd71 dispatch flags (the bridge now forwards
        # 0xB0-0xBE).  Mirror the ROM's side effects exactly (lab_de81..lab_de0f)
        # so the knit-cycle state machine sees the machine's mode toggles that
        # the wire carries but the ROM's own INTCSI handler never runs for.
        if 0xB3 <= cmd <= 0xBE:
            ram = self.m.bus.ram
            if cmd == 0xB3:
                ram[0xFDB9 - 0xFD00] = (ram[0xFDB9 - 0xFD00] + 1) & 0xFF
            elif cmd == 0xB4:
                ram[0xFDBA - 0xFD00] = (ram[0xFDBA - 0xFD00] + 1) & 0xFF
            elif cmd == 0xB5:
                ram[0xFE56 - 0xFD00] |= 0x08          # SET1 fe56.3
            elif cmd == 0xB6:
                ram[0xFEA9 - 0xFD00] = (ram[0xFEA9 - 0xFD00] & ~0x0E) | 0x01
            elif cmd == 0xB7:
                ram[0xFEA9 - 0xFD00] = (ram[0xFEA9 - 0xFD00] & ~0x05) | 0x0A
            elif cmd == 0xB8:
                ram[0xFEA9 - 0xFD00] = (ram[0xFEA9 - 0xFD00] & ~0x0B) | 0x04
            elif cmd == 0xB9:
                ram[0xFE56 - 0xFD00] |= 0x40          # SET1 fe56.6
            elif cmd == 0xBA:
                ram[0xFE56 - 0xFD00] &= ~0x40         # CLR1 fe56.6
            elif cmd == 0xBB:
                ram[0xFE56 - 0xFD00] &= ~0x10         # CLR1 fe56.4
            elif cmd == 0xBC:
                ram[0xFE56 - 0xFD00] |= 0x10          # SET1 fe56.4
            elif cmd == 0xBD:
                if not (ram[0xFE69 - 0xFD00] & 0x02):   # BT fe69.1 -> RET
                    ram[0xFE67 - 0xFD00] &= ~0x04     # CLR1 fe67.2
                    ram[0xFE5A - 0xFD00] &= ~0x08     # CLR1 fe5a.3
                    ram[0xFE5A - 0xFD00] |= 0x04      # SET1 fe5a.2
            elif cmd == 0xBE:
                if not (ram[0xFE69 - 0xFD00] & 0x02):   # BT fe69.1 -> RET
                    ram[0xFE67 - 0xFD00] |= 0x04      # SET1 fe67.2
                    ram[0xFE5A - 0xFD00] &= ~0x08     # CLR1 fe5a.3
                    ram[0xFE5A - 0xFD00] |= 0x04      # SET1 fe5a.2
            return
        if 0x60 <= cmd <= 0x6D:
            # sub_dd71 @ 0xDF76-0xDFEC: sensor bitmap (PH TEST live display).
            # Pairs: even = SET, odd = CLR, of mem_fe87 bit (cmd-0x60)/2
            # (0x60/61 -> .0, 0x62/63 -> .1, ... 0x6C/6D -> .6).
            bit = (cmd - 0x60) >> 1
            fe87 = self.m.bus.ram[0xFE87 - 0xFD00]
            if cmd & 1:
                self.m.bus.ram[0xFE87 - 0xFD00] = fe87 & ~(1 << bit)
            else:
                self.m.bus.ram[0xFE87 - 0xFD00] = fe87 | (1 << bit)
            return
        if 0xC0 <= cmd <= 0xCD:
            # sub_dd71 @ 0xDEE6-0xDF75: 3-way tri-state groups -> mem_fe85/86
            # (KLG TEST live display).  group0 C0/C1/C2 -> fe85.0/.1/.2,
            # group1 C3/C4/C5 -> fe85.3/.4/.5, group2 C8/C9/CA -> fe85.6/.7 +
            # fe86.0, group3 CB/CC/CD -> fe86.1/.2/.3.
            fe85 = self.m.bus.ram[0xFE85 - 0xFD00]
            fe86 = self.m.bus.ram[0xFE86 - 0xFD00]
            if 0xC0 <= cmd <= 0xC2:
                fe85 = (fe85 & ~0x07) | (1 << (cmd - 0xC0))
            elif 0xC3 <= cmd <= 0xC5:
                fe85 = (fe85 & ~0x38) | (1 << (3 + cmd - 0xC3))
            elif 0xC8 <= cmd <= 0xCA:
                v = cmd - 0xC8
                fe85 &= ~0xC0
                fe86 &= ~0x01
                if v == 0:
                    fe85 |= 0x40
                elif v == 1:
                    fe85 |= 0x80
                else:
                    fe86 |= 0x01
            else:  # 0xCB <= cmd <= 0xCD
                fe86 = (fe86 & ~0x0E) | (1 << (1 + cmd - 0xCB))
            self.m.bus.ram[0xFE85 - 0xFD00] = fe85
            self.m.bus.ram[0xFE86 - 0xFD00] = fe86
            return
        if cmd > 0x4F:
            return
        # sub_dd71 @ 0xDFED-0xE02D: low nibble -> fdbb (0x0X), fdbc (0x1X),
        # fdbd (0x2X), fdbe (0x3X), fdbf (0x4X).  fdbb/fdbc = version,
        # fdbd/fdbe/fdbf = width LSB..MSB.
        hi = cmd & 0xF0
        lo = cmd & 0x0F
        if hi == 0x00:
            self.m.bus.ram[0xFDBB - 0xFD00] = lo
        elif hi == 0x10:
            self.m.bus.ram[0xFDBC - 0xFD00] = lo
        elif hi == 0x20:
            self.m.bus.ram[0xFDBD - 0xFD00] = lo
        elif hi == 0x30:
            self.m.bus.ram[0xFDBE - 0xFD00] = lo
        elif hi == 0x40:
            self.m.bus.ram[0xFDBF - 0xFD00] = lo
        # The version-major nibble (0x0X, first byte of the machine's boot
        # relay) marks a fresh machine (re)boot: the machine's boot handshake
        # reads fe7e back on its first 0x80 keep-alive polls and expects 0xD0
        # (lab_2e11 sets it during the ROM's own handshake, which the emulator
        # bypasses).  If the CB-1 was already "ready to knit" (fe7e = 0xD2)
        # when the machine re-booted, the machine sees 0xD2 instead of 0xD0,
        # decides the CB-1 is out of sequence, and drops the link (boot loop).
        # Re-arm 0xD0 here; the knit-ready path re-raises it to 0xD2 after the
        # handshake settles (see _csi_worker).
        if hi == 0x00:
            self.m.bus.ram[0xFE7E - 0xFD00] = 0xD0
            self._machine_boot_at = time.time()
        # The machine relayed config nibbles -> it is provably present.  Set
        # fe56.7 here (rather than on HEV_CS, which fires on every keep-alive
        # pulse before the nibbles arrive) so sub_3a8a sees a valid width.
        self.m.set_machine_present(True)

    def _push_state(self):
        """Push the ROM's live link state to the RP2040 cache as T_STATE.

        The bridge serves these bytes to the machine, so they must mirror the
        emulator's SRAM rather than the bridge's boot defaults.  Called after
        every command and on a slow cadence so a menu edit / pattern action
        propagates on the next exchange.

        Addresses are the ROM's data-source pointers (sub_dc3d..sub_dc99):
          0x50-0x53 counters      -> external-RAM 0x0009..0x000C
          0x90-0x92 row-code      -> external-RAM 0x00A3..0x00A5
          0xA0/A1 needle row      -> external-RAM 0x02EB + fe7c * ceil(width/8)
          0x80 expected-ack fe7e  -> internal-RAM 0xFE7E
          0x81 knit flag fe67.0   -> internal-RAM 0xFE67
        """
        sram = self.m.bus.sram
        ram = self.m.bus.ram
        for i in range(4):
            self.csi_state_fifo.write(
                encode_frame(T_STATE, bytes((0x50 + i, sram[0x0009 + i]))))
        for i in range(3):
            self.csi_state_fifo.write(
                encode_frame(T_STATE, bytes((0x90 + i, sram[0x00A3 + i]))))
        # fe7e is NOT pushed here: the bridge OWNS the wire expected-ack and
        # derives it immediately from the machine's own commands (0x01..0x4F ->
        # 0xD0 boot, 0x91 -> 0xD1, 0xB0 -> 0xD6).  Pushing the ROM's fe7e (or a
        # cached wire copy) here races with that dispatch: the push for the
        # boot 0xD0 arrives AFTER the machine's 0x91 read and clobbers the 0xD1
        # the bridge just set, so the machine sees 0xD0 and drops the link.
        # The one value the bridge can't derive from the machine is "ready"
        # (0xD2), which serve.py pushes once in _csi_worker after the handshake.
        self.csi_state_fifo.write(
            encode_frame(T_STATE, bytes((0x81, ram[0xFE67 - 0xFD00] & 1))))
        self.csi_state_n += 8

    def _push_pattern(self):
        """Push the FULL needle-row buffer to the bridge, matching the
        reference protocol_test.py.  The bridge owns the row pointer (row_idx,
        advanced on 0xB0) and serves A0/A1 from pattern[row_idx]/[row_idx+1],
        so every row must be pre-filled: 0xA0=row0, 0xA1=row1, 0xA2=rows
        2..fddb.  Pushing a dynamic fe7c window into pattern[0]/pattern[1]
        (the old behaviour) misaligns with row_idx after the first 0xB0
        advance, which is why knitting stopped after the first two rows.
        """
        sram = self.m.bus.sram
        ram = self.m.bus.ram
        width = sram[0x0007] | (sram[0x0008] << 8)
        row_bytes = max(1, (width + 7) >> 3)
        fddb = ram[0xFDDB - 0xFD00]          # last row index (wrap bound)
        nrows = max(1, min(fddb + 1, 22))    # bridge N_ROWS = 22

        def needle_row(r):
            base = 0x02EB + r * row_bytes
            nd = bytes(sram[base:base + 25])
            return nd + bytes(25 - len(nd)) if len(nd) < 25 else nd

        self.csi_state_fifo.write(
            encode_frame(T_STATE, bytes((0xA0,)) + needle_row(0)))
        if nrows > 1:
            self.csi_state_fifo.write(
                encode_frame(T_STATE, bytes((0xA1,)) + needle_row(1)))
        for r in range(2, nrows):
            self.csi_state_fifo.write(
                encode_frame(T_STATE, bytes((0xA2, r)) + needle_row(r)))
        self.csi_state_n += nrows

    def _push_diag_fe7e(self):
        """Push the ROM's diagnostic test-mode expected-ack to the bridge.

        The bridge derives fe7e itself for the boot/knit path (0x01..0x4F ->
        0xD0, 0x91 -> 0xD1, 0xB0 -> 0xD6) and for the knit re-arm (D0 -> D2),
        so _push_state() deliberately never pushes fe7e -- pushing those values
        races with the bridge's own dispatch.  The diagnostic test acks are
        different: they are set ONLY by the diagnostic menu's own code
        (PH TEST 0xE929 -> 0xD3, SOL TEST 0xE9A4 -> 0xD4, POS TEST 0xEB04 ->
        0xD5) and KLG TEST's "ready" (sub_3e50 -> 0xD2, guarded by fe55.3),
        so they can never be derived from the machine's commands.  Push those
        on a RAM transition, and push 0xD0 back when the menu leaves a test.
        """
        ram = self.m.bus.ram
        fe7e = ram[0xFE7E - 0xFD00]
        if fe7e == self._last_fe7e:
            return
        prev = self._last_fe7e
        self._last_fe7e = fe7e
        # D2 alone is ambiguous (it is also the normal "ready to knit" ack), so
        # only treat D2 as a test-mode ack while fe55.3 (the KLG test flag) is
        # set.  D3/D4/D5 are diagnostic-only and unambiguous.
        diag = fe7e in (0xD3, 0xD4, 0xD5) or (
            fe7e == 0xD2 and (ram[0xFE55 - 0xFD00] & 0x08))
        klg = fe7e == 0xD2 and (ram[0xFE55 - 0xFD00] & 0x08)
        was_diag = self._diag_active
        if diag:
            self._diag_active = True
            self.csi_state_fifo.write(encode_frame(T_STATE, bytes((0x80, fe7e))))
            if klg:
                # KLG TEST: the machine's 0xB0 cycle byte must NOT set fe7e=D6
                # on the wire (the ROM's lab_dd8e hits BT fe55.3 -> RET).  Tell
                # the bridge it is in KLG mode (selector 0x82).
                self.csi_state_fifo.write(encode_frame(T_STATE, bytes((0x82, 1))))
        elif fe7e == 0xD0 and was_diag:
            # Leaving a test: sub_3e69 re-arms fe7e to 0xD0.  The machine does
            # not re-send its config relay here, so the bridge must be told to
            # stop serving the old test ack.
            self._diag_active = False
            self.csi_state_fifo.write(encode_frame(T_STATE, bytes((0x80, 0xD0))))
            self.csi_state_fifo.write(encode_frame(T_STATE, bytes((0x82, 0))))
        else:
            self._diag_active = False

    def _press(self, k):
        """Start one fixed-length key pulse (a single click -> one key press)."""
        if k in self._key_release:
            return                      # already in this key's pulse (debounce)
        self.m.set_key(k, True)
        steps = ARROW_PULSE_STEPS if k in _ARROW_KEYS else KEYPAD_PULSE_STEPS
        self._key_release[k] = self._steps + steps

    def _release_keys(self):
        """Auto-release keys whose pulse has expired."""
        now = self._steps
        for k, at in list(self._key_release.items()):
            if now >= at:
                self.m.set_key(k, False)
                del self._key_release[k]

    # ---- HTTP handlers --------------------------------------------------
    def frame_json(self):
        with self.lock:
            px = bytes(self.fb)
        return json.dumps({'pixels': base64.b64encode(px).decode('ascii')})

    def csi_debug(self):
        """Live CSI link state, for diagnosing the machine relay."""
        c = self.m.csi
        ram = self.m.bus.ram
        return json.dumps({
            'pc': self.m.cpu.pc,
            'ie': (self.m.cpu.psw >> 7) & 1,
            'cs_low': c.cs_low,
            'din_high': c.din_high,
            'csim': c.csim,
            'sio': c.sio,
            'pending_intp0': c._pending.get(c.INTP0_SLOT if hasattr(c, 'INTP0_SLOT') else 3),
            'pending_intcsi': c._pending.get(19),
            'fe56.7_machine': (ram[0xFE56 - 0xFD00] >> 7) & 1,
            'fe56.5_linked': (ram[0xFE56 - 0xFD00] >> 5) & 1,
            'fe83': ram[0xFE83 - 0xFD00],
            'fe7e': ram[0xFE7E - 0xFD00],
            'trace': [('%s%02X' % (d, b)) for d, b in c.trace[-16:]],
            'pin_out': c.pin_out,
            'pin_log': c.pin_log,
            'csi_in_fifo': len(self.csi_in_fifo),
            'csi_tx_fifo': len(self.csi_tx_fifo),
            'csi_pin_n': self.csi_pin_n,
            'csi_byte_n': self.csi_byte_n,
            'csi_state_n': self.csi_state_n,
            'csi_in_log': self.csi_in_log[-20:],
            'csi_cmd_log': self.csi_cmd_log[-64:],
            'events': [bytes(e).hex() for e in self.csi_events[-32:]],
            'link_active': (time.time() - self._last_txn_time) < 15.0,
            'txn_count': self._txn_count,
            'cpu_hist': [('%04X:%02X' % (pc, op)) for pc, op in self.m.cpu.history[-64:]],
            'sp': self.m.cpu.sp,
            'steps': self._steps,
            'pressed': sorted(self.m.pressed),
            'p0': self.m.bus.sfr.get(0xFF00, 0),
            'p2': self.m.read_p2(),
            'p7': self.m.read_p7(),
            'key_release': {k: v - self._steps for k, v in self._key_release.items()},
            'fe9d': ram[0xFE9D - 0xFD00],
            'fea1': ram[0xFEA1 - 0xFD00],
            'fea0': ram[0xFEA0 - 0xFD00],
            'fe55': ram[0xFE55 - 0xFD00],
            'fe57': ram[0xFE57 - 0xFD00],
            'fe59': ram[0xFE59 - 0xFD00],
            'fe5a': ram[0xFE5A - 0xFD00],
            'fe69': ram[0xFE69 - 0xFD00],
            'fe85': ram[0xFE85 - 0xFD00],
            'fe86': ram[0xFE86 - 0xFD00],
            'fe87': ram[0xFE87 - 0xFD00],
            'fea9': ram[0xFEA9 - 0xFD00],
            'fdb9': ram[0xFDB9 - 0xFD00],
            'fdba': ram[0xFDBA - 0xFD00],
            'fddb': ram[0xFDDB - 0xFD00],
            'bridge_pat0': self._bridge_pat.get(0x22),
            'bridge_pat1': self._bridge_pat.get(0x23),
            'bridge_pat2': self._bridge_pat.get(0x24),
            'bridge_row_idx': self._bridge_pat.get(0x25),
            'pat0': list(self.m.bus.sram[0x02EB:0x02EB + 8]),
            'pat1': list(self.m.bus.sram[0x02EB + 25:0x02EB + 33]),
            'pat2': list(self.m.bus.sram[0x02EB + 50:0x02EB + 58]),
            'diag_active': self._diag_active,
            'last_fe7e': self._last_fe7e,
            'machine_boot_at': round(time.time() - self._machine_boot_at, 2) if self._machine_boot_at else None,
            'sram0002': self.m.bus.sram[0x0002] | (self.m.bus.sram[0x0003] << 8),
            'sram0007': self.m.bus.sram[0x0007] | (self.m.bus.sram[0x0008] << 8),
            'sram7ff4': self.m.bus.sram[0x7FF4 & 0x7FFF] | (self.m.bus.sram[(0x7FF4 & 0x7FFF) + 1] << 8),
            'sram7ffe': self.m.bus.sram[0x7FFE & 0x7FFF] | (self.m.bus.sram[(0x7FFE & 0x7FFF) + 1] << 8),
            'fe66': ram[0xFE66 - 0xFD00],
            'fe49': ram[0xFE49 - 0xFD00],
            'fe12': ram[0xFE12 - 0xFD00],                       # row counter
            'fe72': ram[0xFE72 - 0xFD00] | (ram[0xFE73 - 0xFD00] << 8),  # stitch counter
            'knit_flag': ram[0xFE67 - 0xFD00] & 1,
            'row_codes': [self.m.bus.sram[0x00A3 + i] for i in range(3)],
            'row_index': self.m.bus.sram[0x06EB],
            'needle': [self.m.bus.sram[0x02EB + i] for i in range(16)],
            'reset_log': self.m.reset_log[-6:],
        })

    def queue_key(self, k, down):
        with self.lock:
            self.keys.append((k, down))

    def feed_uart(self, data):
        with self.lock:
            self.uart_rx.extend(data)

    def feed_csi(self, data):
        """Master byte from the bridge (T_CSI_BYTE payload)."""
        with self.lock:
            self.csi_byte_n += 1
            self.csi_in_log.append('B%02X' % data[0] if data else 'B?')
            self.csi_in_log = self.csi_in_log[-40:]
            self.csi_cmd_log.append('B%02X' % data[0] if data else 'B?')
            self.csi_cmd_log = self.csi_cmd_log[-64:]
            self.csi_in_fifo.write(b'\x01' + data)

    def feed_csi_pin(self, data):
        """Pin change from the bridge (T_PIN payload: [pin, level])."""
        with self.lock:
            self.csi_pin_n += 1
            if len(data) >= 2:
                self.csi_in_log.append('P%d=%d' % (data[0], data[1]))
                self.csi_in_log = self.csi_in_log[-40:]
            self.csi_in_fifo.write(b'\x05' + data)

    def drain_uart_tx(self):
        with self.lock:
            data = bytes(self.uart_tx)
            self.uart_tx.clear()
        return data

    def drain_csi_tx(self):
        """Framed records (T_REPLY / T_PIN) the emulator produced for the bridge."""
        with self.lock:
            return self.csi_tx_fifo.drain()

    def _handle_pin(self, pin, lvl):
        """Apply one bridge-reported pin change.  CS/DIN only update the ROM's
        view of P2.1/P2.7 (the reset handshake polls them); the reply bytes are
        staged after each captured byte (see the T_CSI_BYTE path in _run)."""
        csi = self.m.csi
        if pin == csi.PIN_CS:
            csi.set_cs(lvl == 0)
        elif pin == csi.PIN_DIN:
            csi.set_din(lvl == 1)

    def drain_csi_state(self):
        """Framed T_STATE records to push to the RP2040 (raw bytes)."""
        with self.lock:
            return self.csi_state_fifo.drain()

    def feed_csi_event(self, data):
        """Handle an event record (payload: [event_code, arg]) from the RP2040."""
        with self.lock:
            self.csi_events.append(bytes(data))
            if len(self.csi_events) > 64:
                self.csi_events.pop(0)
            # HEV_BYTES (0x16): arg = low byte of the bridge's valid-transaction
            # count.  A change means a framed transaction completed since the
            # last report (true uplink; power-down garbage never completes one).
            if len(data) >= 2 and data[0] == 0x16:
                low = data[1]
                if self._txn_byte_low is None:
                    self._txn_byte_low = low
                    self._txn_count = low
                    # baseline only; no transaction has completed yet
                elif low != self._txn_byte_low:
                    self._txn_count += (low - self._txn_byte_low) & 0xFF
                    self._txn_byte_low = low
                    self._last_txn_time = time.time()
                    # a framed transaction completed -> the link is live.
                    self.m.set_link_active(True)
                # Feed the keep-alive watchdog (fea1) on EVERY report, not just
                # on a count change.  The bridge sends HEV_BYTES every 250 ms, but
                # the real machine's keep-alive poll is ~1 s apart — slower than
                # the firmware's 30-tick watchdog (~0.83 s) — so only resetting
                # on change lets fea1 overflow and the INTC10 handler at 0x104D
                # does BR !reset (0x1061), looping the boot screen forever.
                self.m.bus.ram[0xFEA1 - 0xFD00] = 0
            # Bridge pattern-cache debug: rows 0..2 first byte + row_idx.
            if len(data) >= 2 and 0x22 <= data[0] <= 0x25:
                self._bridge_pat[data[0]] = data[1]
            # Dispatch events from the bridge (payload [EV_code, cmd]).  The
            # bridge's csi_step parses each transaction and reports the semantic
            # meaning; the emulator applies the ROM's state side effects here
            # (the bridge's simplified dispatch only tracks fe7e/knit_flag, not
            # the row/stitch counters that live in the emulator's RAM).
            if len(data) >= 2 and 0x01 <= data[0] <= 0x05:
                ev, cmd = data[0], data[1]
                ram = self.m.bus.ram

                # Faithful port of the ROM's sub_dd71 dispatch side effects
                # (0xDB8E..0xDE80).  The RP2040 answers the wire, so the ROM's
                # own INTCSI handler never runs; the emulator applies its exact
                # flag/counter updates here instead.  The ROM's knit-cycle state
                # machine (sub_336a/sub_3420/...) then reads these flags to call
                # sub_a708/sub_a75e, which advance the pattern row pointer
                # &!06eb (mirrored to fe7c) — the thing that was NOT advancing.

                def row_back_tail():
                    # lab_de58..lab_de80: shared by 0xB2 and the 0xB1 fea9.2 path.
                    if ram[0xFE69 - 0xFD00] & 0x04:      # BT fe69.2 -> RET
                        return
                    row = ram[0xFE12 - 0xFD00]
                    if row != 0xFF:                      # CMP A,#ff -> skip DEC
                        ram[0xFE12 - 0xFD00] = (row - 1) & 0xFF   # DEC fe12
                    if not (ram[0xFE66 - 0xFD00] & 0x08):   # BT fe66.3 -> skip INC
                        st = (ram[0xFE72 - 0xFD00] | (ram[0xFE73 - 0xFD00] << 8)) + 1
                        if st >= 0x2710:
                            st = 0
                        ram[0xFE72 - 0xFD00] = st & 0xFF
                        ram[0xFE73 - 0xFD00] = st >> 8
                    ram[0xFE56 - 0xFD00] |= 0x04          # SET1 fe56.2
                    ram[0xFE67 - 0xFD00] |= 0x02          # SET1 fe67.1

                if ev == 0x01:        # EV_START (0xB0) - lab_dd8e..lab_ddd0
                    ram[0xFEA9 - 0xFD00] &= ~0x08         # CLR1 fea9.3
                    fe57 = ram[0xFE57 - 0xFD00]
                    fe69 = ram[0xFE69 - 0xFD00]
                    do_da6 = False
                    if fe57 & 0x02:                       # BT fe57.1 -> lab_ddd1
                        ram[0xFE57 - 0xFD00] = fe57 & ~0x02   # CLR1 fe57.1
                        do_da6 = True
                    elif fe69 & 0x02:                     # BT fe69.1 -> lab_ddc7
                        do_da6 = False
                    else:
                        ram[0xFE67 - 0xFD00] &= ~0x03     # CLR1 fe67.0 + fe67.1
                        ram[0xFE56 - 0xFD00] |= 0x02      # SET1 fe56.1
                        ram[0xFE57 - 0xFD00] |= 0x01      # SET1 fe57.0
                        do_da6 = not (ram[0xFE55 - 0xFD00] & 0x08)  # BT fe55.3 -> RET
                    if do_da6:                            # lab_dda6
                        ram[0xFE7E - 0xFD00] = 0xD6       # MOV fe7e, #d6
                        fe66 = ram[0xFE66 - 0xFD00]
                        fea9 = ram[0xFEA9 - 0xFD00]
                        fe12 = ram[0xFE12 - 0xFD00]
                        if (not (fe66 & 0x40) and not (fe66 & 0x80) and not (fea9 & 0x04)
                                and (fe66 & 0x08) and not (fe69 & 0x02)
                                and fe12 != 0xFF and fe12 != 0):
                            ram[0xFE59 - 0xFD00] |= 0x02  # SET1 fe59.1
                            ram[0xFE12 - 0xFD00] = 0      # lab_ddc7: fe12 = 0
                elif ev == 0x02:      # EV_ROW_ADV (0xB1) - lab_de10..lab_de48
                    fe66 = ram[0xFE66 - 0xFD00]
                    fe69 = ram[0xFE69 - 0xFD00]
                    fea9 = ram[0xFEA9 - 0xFD00]
                    fe55 = ram[0xFE55 - 0xFD00]
                    fe67 = ram[0xFE67 - 0xFD00]
                    # reach lab_de1e unless fe66.5/6 clear AND fe69.1 set (-> RET)
                    if (fe66 & 0x20) or (fe66 & 0x40) or not (fe69 & 0x02):
                        if not (fea9 & 0x04):             # BF fea9.2 -> lab_de2e
                            if not (fe67 & 0x01):         # BT fe67.0 -> RET
                                ram[0xFE67 - 0xFD00] = fe67 | 0x01   # SET1 fe67.0
                                if not (fe55 & 0x08) and not (fe69 & 0x02):
                                    row = ram[0xFE12 - 0xFD00]
                                    if row != 1:          # CMP A,#01 -> skip INC
                                        ram[0xFE12 - 0xFD00] = (row + 1) & 0xFF
                                ram[0xFE57 - 0xFD00] |= 0x01   # lab_de46: SET1 fe57.0
                        else:                             # fea9.2 set path
                            if not (fe67 & 0x01):         # BT fe67.0 -> RET
                                ram[0xFE67 - 0xFD00] = fe67 | 0x01   # SET1 fe67.0
                                ram[0xFE57 - 0xFD00] |= 0x01   # lab_de2a: SET1 fe57.0
                                row_back_tail()           # BR lab_de58
                elif ev == 0x03:      # EV_ROW_BACK (0xB2) - lab_de49..lab_de80
                    fe55 = ram[0xFE55 - 0xFD00]
                    fe66 = ram[0xFE66 - 0xFD00]
                    fea9 = ram[0xFEA9 - 0xFD00]
                    if fe55 & 0x08:                       # BT fe55.3 -> lab_de6b
                        # The ROM increments fe72 (stitch counter) here, but the
                        # KLG loop (lab_e9d9) checks fe72 BEFORE fdb9/fdba, and
                        # the lace carriage's B2 stream keeps fe72 non-zero, so
                        # the tri-state display (fdb9/fdba) is starved and the
                        # screen sticks on the stitch "0" indicator.  The
                        # emulator's ROM loop is ~85x slower than a real CB-1 and
                        # cannot drain fe72 fast enough, so suppress the
                        # increment (the stitch "0" indicator) but keep the
                        # lab_de7c flags (fe56.2 / fe67.1).
                        ram[0xFE56 - 0xFD00] |= 0x04      # SET1 fe56.2
                        ram[0xFE67 - 0xFD00] |= 0x02      # SET1 fe67.1
                    elif (not (fe66 & 0x80)) or (not (fea9 & 0x04)):
                        row_back_tail()                   # lab_de58 (fe66.7 clear or fea9.2 clear)
                    # else: BT fea9.2 -> RET (no action)
                # EV_COUNTER (0x04) and EV_ABORT (0x05) carry no row/stitch state.
                # Push the updated state immediately so the bridge's fe7e / knit
                # cache tracks the wire without waiting for the 0.5 s cadence
                # (the machine's next 0x80 keep-alive may arrive first).
                self._push_state()
                # Latch "transaction processed" (fe84.3) exactly like the ROM's
                # INTCSI handler does after sub_dd71 (lab_dd0b: MOV fe84, fe83
                # with fe83.3 set).  The ROM's main knitting loop (lab_2fc6) only
                # enters its knit dispatch (lab_2fcc -> sub_3e20/sub_336a /
                # lab_304f -> sub_a708) when fe84.3 is set, so without this latch
                # the row pointer (&!06eb) never advances and the display stays
                # frozen even though the wire is knitting.
                self.m.bus.ram[0xFE84 - 0xFD00] |= 0x08
            # Bridge link status -> emulator flags.  HEV_CS (0x10) is a machine
            # attention pulse; the ROM itself sets fe56.7 during its boot CS poll
            # (lab_2dcf) when cs_low is driven by the T_PIN(CS) records, so do NOT
            # set_machine_present here — doing so marks the machine present on
            # every keep-alive pulse (before the config nibbles have been relayed)
            # and sub_3a8a then overwrites the width with 0, looping the boot.
            # HEV_UP (0x13) = link-up complete -> fe56.5 (linked) and CS released;
            # HEV_WAIT (0x14) = the machine gave up -> CS released.
            if len(data) >= 1:
                ev = data[0]
                if ev == 0x13:
                    self.m.set_link_up(True)
                    self.m.csi.set_cs(False)
                elif ev == 0x14:
                    self.m.csi.set_cs(False)

    def reboot_diag(self):
        with self.lock:
            self.m.boot_diagnostic()

    def reset(self):
        """Reboot to the boot screen without clearing the onboard SRAM.

        Saved patterns live in the bank-8 SRAM; a reset (like a power-cycle or
        the firmware's own reset path) re-runs the boot init while keeping that
        SRAM intact.
        """
        with self.lock:
            self.keys.clear()
            self._key_release.clear()
            self.uart_rx.clear()
            self.uart_tx.clear()
            self.csi_in_fifo.drain()
            self.csi_tx_fifo.drain()
            self.m.reset()

    def cancel_handshake(self):
        """Force the emulator to treat the link as live and boot past the
        handshake (manual override for the UI button)."""
        with self.lock:
            self.m.set_link_active(True)


backend = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client (browser / WebSerial poller) went away mid-response.
            pass

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ('/', '/index.html'):
            try:
                body = open(resource_path(WEB_INDEX), 'rb').read()
                return self._send(200, body, 'text/html')
            except FileNotFoundError:
                return self._send(404, 'not found', 'text/plain')
        if p.path == '/frame':
            return self._send(200, backend.frame_json())
        if p.path == '/csi_debug':
            return self._send(200, backend.csi_debug())
        if p.path == '/uart_tx':
            return self._send(200, json.dumps(list(backend.drain_uart_tx())))
        if p.path == '/csi_tx':
            return self._send(200, bytes(backend.drain_csi_tx()), 'application/octet-stream')
        if p.path == '/csi_state':
            return self._send(200, bytes(backend.drain_csi_state()), 'application/octet-stream')
        return self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        if p.path == '/key':
            k = q.get('k', [''])[0]
            backend.queue_key(k, 'up' not in q)
            return self._send(200, 'ok')
        if p.path == '/diag':
            backend.reboot_diag()
            return self._send(200, 'ok')
        if p.path == '/reset':
            backend.reset()
            return self._send(200, 'ok')
        if p.path == '/cancel_handshake':
            backend.cancel_handshake()
            return self._send(200, 'ok')
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = self.rfile.read(length)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        if p.path == '/uart_rx':
            backend.feed_uart(data)
            return self._send(200, 'ok')
        if p.path == '/csi_rx':
            backend.feed_csi(data)
            return self._send(200, 'ok')
        if p.path == '/csi_pin':
            backend.feed_csi_pin(data)
            return self._send(200, 'ok')
        if p.path == '/csi_event':
            backend.feed_csi_event(data)
            return self._send(200, 'ok')
        return self._send(404, 'not found', 'text/plain')


def main():
    global backend
    # `--diag` boots straight into the hardware diagnostic menu
    # `--speed N` sets emulated steps per second (default 72000)
    # `--png PATH` periodically saves the live framebuffer (e.g. --png lcd.png)
    # `--no-browser` skips auto-opening the web UI
    png_path = None
    if '--png' in sys.argv:
        i = sys.argv.index('--png')
        png_path = sys.argv[i + 1] if i + 1 < len(sys.argv) else 'lcd.png'
    speed = DEFAULT_SPEED
    if '--speed' in sys.argv:
        i = sys.argv.index('--speed')
        if i + 1 < len(sys.argv):
            try:
                speed = int(sys.argv[i + 1])
            except ValueError:
                pass
    backend = Backend(boot_diag='--diag' in sys.argv, png_path=png_path, speed=speed)
    backend.start()
    srv = HTTPServer(('127.0.0.1', 8765), Handler)
    url = 'http://127.0.0.1:8765'
    print('CB-1 emulator backend on ' + url
          + ('  (diagnostic-menu boot)' if '--diag' in sys.argv else '')
          + ('  (speed %d steps/s)' % speed)
          + (('  (png -> %s)' % png_path) if png_path else ''))
    if '--no-browser' not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        backend.running = False


if __name__ == '__main__':
    main()
