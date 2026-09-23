#!/usr/bin/env python3
"""
CB-1 machine: CPU + bus + peripherals (LCD, keypad, serial, timers).
"""
from .cpu78k2 import CPU78K2
from .bus import Bus

# Keypad matrix: name -> (P0 low-nibble row select, P7 column bit)
# Rows (active-low): P0.0=0x0E, P0.3=0x07, P0.2=0x0B, P0.1=0x0D
# Columns (active-low): P7.0..P7.3
KEY_MATRIX = {
    'OK': (0x0E, 0), 'M/B': (0x0E, 1), 'ADVICE': (0x0E, 2), 'DC': (0x0E, 3),
    '0': (0x07, 0), '7': (0x07, 1), '4': (0x07, 2), '1': (0x07, 3),
    '.': (0x0B, 0), '8': (0x0B, 1), '5': (0x0B, 2), '2': (0x0B, 3),
    'C': (0x0D, 0), '9': (0x0D, 1), '6': (0x0D, 2), '3': (0x0D, 3),
}
# Arrow pad: name -> P2 bit (active-low, bits 2..5)
# ROM keyb_scan_arrows (0xD199) maps: P2.4 -> key 0x04 -> "next"/scroll-right,
# P2.5 -> key 0x08 -> "prev"/scroll-left.  So P2.4 = RIGHT, P2.5 = LEFT.
KEY_ARROWS = {'UP': 2, 'DOWN': 3, 'RIGHT': 4, 'LEFT': 5}

# boot-time key chords: holding these at reset enters the diagnostic menu
BOOT_DIAG = {'DOWN', 'OK'}

# direct diagnostic-menu boot: when enabled, the reset handler's key check at
# DIAG_HOOK is short-circuited and the CPU jumps straight to DIAG_ENTRY, so we
# don't depend on the key-chord scan working.
DIAG_HOOK = 0x2E29   # lab_2e29: CALL keyb_read (right before the test-mode check)
DIAG_ENTRY = 0xE7D9  # lab_e7d9: hardware diagnostic menu entry


class Machine:
    def __init__(self, rom_path=None, boot_keys=None, boot_diag=False):
        rom = None
        if rom_path:
            with open(rom_path, 'rb') as f:
                rom = f.read()
        self.bus = Bus(rom, self)
        self.cpu = CPU78K2(self.bus)
        self.lcd = LCD()
        self.uart_tx_log = []
        self.tick_count = 0
        self.boot_keys = set(boot_keys or [])  # keys held from power-up
        self.pressed = set(self.boot_keys)
        self.boot_diag = boot_diag              # jump straight to diag menu on boot

    def reset(self):
        self.cpu.reset()
        self.pressed = set(self.boot_keys)

    def boot_diagnostic(self):
        """Reboot directly into the hardware diagnostic menu (no key chord)."""
        self.boot_diag = True
        self.reset()

    def set_key(self, name, down):
        if down:
            self.pressed.add(name)
        else:
            self.pressed.discard(name)

    # P7.3-0 = keypad columns (0 = pressed). P7.4 = cartridge detect.
    def read_p7(self):
        v = 0x0F                        # all columns released (high)
        row = self.bus.sfr[0xFF00] & 0x0F
        for name, (r, col) in KEY_MATRIX.items():
            if name in self.pressed and r == row:
                v &= ~(1 << col)
        v |= 0x10                       # P7.4: no cartridge (high)
        return v

    # P2.5-2 = arrows (0 = pressed). P2.1 = CSI CS, P2.6 = 9V detect (1=present), P2.7 = MISO.
    def read_p2(self):
        v = 0x3C                        # all arrows released
        for name, bit in KEY_ARROWS.items():
            if name in self.pressed:
                v &= ~(1 << bit)
        v |= 0x02                       # P2.1 CSI CS idle high
        v |= 0x40                       # P2.6 9V present (normal power)
        v |= 0x80                       # P2.7 MISO idle high
        return v

    def step(self):
        if self.boot_diag and self.cpu.pc == DIAG_HOOK:
            self.cpu.pc = DIAG_ENTRY          # short-circuit the key check
            self.boot_diag = False             # one-shot
        return self.cpu.step()

    def run(self, n):
        for _ in range(n):
            try:
                self.step()
            except NotImplementedError as e:
                print('UNIMPLEMENTED:', e)
                raise
        return self.cpu.pc

    # ---- peripheral hooks ----------------------------------------------
    def uart_tx(self, v):
        self.uart_tx_log.append(v)
        # set TX-done flag so firmware sees completion (IF0h.6)
        self.bus.sfr[0xFFE1] |= 0x40

    def port0_write(self, v):
        self.lcd.pending = v

    def port3_write(self, old, v):
        # P3.5=A0 (0=cmd/1=data), P3.6=WR#0, P3.7=WR#1, P3.4=buzzer, P3.2=SCK, P3.3=MOSI
        a0 = (v >> 5) & 1
        wr0 = (v >> 6) & 1
        wr1 = (v >> 7) & 1
        # latch on falling edge of the write strobes
        if wr0 == 0 and ((old >> 6) & 1) == 1:
            self.lcd.write(0, a0, self.lcd.pending)
        if wr1 == 0 and ((old >> 7) & 1) == 1:
            self.lcd.write(1, a0, self.lcd.pending)
        self.lcd.buzzer = (v >> 4) & 1

    def tick(self):
        """Fire the INTC10 'tick' timer interrupt."""
        # vector slot for INTC10 = 12 (address 0x0018)
        self.cpu.request_int(12)

    def buffer_pixels(self):
        """Render the logical display buffer (bank-8 SRAM) as a 122x32 array.

        The firmware draws into a 122x32 framebuffer at external SRAM
        0x0103 + page + col*4 (bit 0 = top row of the page) and copies it to
        the LCD with disp_lcd01_refresh.  The LCD RAM can additionally be
        poked directly to implement blinking UI elements (e.g. the menu
        selection box flashes on/off), so the LCD RAM alone is not a stable
        screenshot.  The buffer holds the steady, non-blinking image.
        """
        invert = (self.bus.ram[0xFEBF - 0xFD00] >> 7) & 1
        out = bytearray(122 * 32)
        for col in range(122):
            for page in range(4):
                b = self.bus.sram[(0x0103 + page + col * 4) & 0x7FFF]
                if invert:
                    b ^= 0xFF
                for bit in range(8):
                    out[(page * 8 + bit) * 122 + col] = (b >> bit) & 1
        return out


class LCD:
    """SED1520 x2 model: 2 chips x 61 columns = 122 x 32 pixels.

    The CB-1 firmware only uses 4 of the 8 SED1520 pages (lcd01_setRAMpage
    masks the page to 0..3, and lcd_write rejects rows >= 32), so the visible
    area is 122 wide x 32 tall (4 pages of 8 rows).
    Column-address command = 0x00..0x3F, page command = 0xB8..0xBF.
    """
    PAGES = 4

    def __init__(self):
        self.pending = 0
        self.buzzer = 0
        self.chip0 = bytearray(self.PAGES * 61)
        self.chip1 = bytearray(self.PAGES * 61)
        self.page = [0, 0]
        self.col = [0, 0]
        self.on = True
        self.ram = [self.chip0, self.chip1]

    def write(self, chip, a0, byte):
        if a0 == 0:
            self.ctrl(chip, byte)
        else:
            self.data(chip, byte)

    def ctrl(self, chip, byte):
        if byte <= 0x3F:            # column address (0x00..0x3F)
            self.col[chip] = byte & 0x3F
        elif 0xB8 <= byte <= 0xBF:  # page address (0xB8..0xBB used)
            self.page[chip] = byte & 0x07
        elif byte == 0xAE:
            self.on = False
        elif byte == 0xAF:
            self.on = True

    def data(self, chip, byte):
        p = self.page[chip]
        c = self.col[chip]
        if 0 <= p < self.PAGES and 0 <= c < 61:
            self.ram[chip][p * 61 + c] = byte
            self.col[chip] = (c + 1) % 61

    def pixels(self):
        """Return a 122 x 32 bytearray of 0/1 (row-major)."""
        out = bytearray(122 * self.PAGES * 8)
        for chip in range(2):
            base_col = chip * 61
            for page in range(self.PAGES):
                for col in range(61):
                    d = self.ram[chip][page * 61 + col]
                    for bit in range(8):
                        x = base_col + col
                        y = page * 8 + bit
                        out[y * 122 + x] = (d >> bit) & 1
        return out
