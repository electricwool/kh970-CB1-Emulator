#!/usr/bin/env python3
"""
Memory bus for the CB-1 emulator: 64 KiB program space (ROM + internal RAM +
SFR) plus the 1 MiB banked data space (onboard SRAM, cartridge).

External data bank is selected by P6.3..0 (A16..A19 of the 20-bit data space).
"""
SFR = {
    # ports
    0xFF00: 'P0', 0xFF02: 'P2', 0xFF03: 'P3', 0xFF04: 'P4',
    0xFF05: 'P5', 0xFF06: 'P6', 0xFF07: 'P7',
    0xFF0A: 'P0l', 0xFF0B: 'P0h', 0xFF0C: 'RTPC',
    # timers
    0xFF10: 'CR00l', 0xFF11: 'CR00h', 0xFF12: 'CR01l', 0xFF13: 'CR01h',
    0xFF14: 'CR10', 0xFF15: 'CR20', 0xFF16: 'CR21', 0xFF17: 'CR30',
    0xFF18: 'CR02l', 0xFF19: 'CR02h', 0xFF1A: 'CR22', 0xFF1C: 'CR11',
    0xFF20: 'PM0', 0xFF23: 'PM3', 0xFF25: 'PM5', 0xFF26: 'PM6',
    0xFF30: 'CRC0', 0xFF31: 'TOC', 0xFF32: 'CRC1', 0xFF34: 'CRC2',
    0xFF40: 'PUO', 0xFF43: 'PMC3',
    0xFF50: 'TM0l', 0xFF51: 'TM0h', 0xFF52: 'TM1', 0xFF54: 'TM2', 0xFF56: 'TM3',
    0xFF5C: 'PRM0', 0xFF5D: 'TMC0', 0xFF5E: 'PRM1', 0xFF5F: 'TMC1',
    0xFF68: 'ADM', 0xFF6A: 'ADCR',
    # serial
    0xFF80: 'CSIM', 0xFF82: 'SBIC', 0xFF86: 'SIO',
    0xFF88: 'ASIM', 0xFF8A: 'ASIS', 0xFF8C: 'RXB', 0xFF8E: 'TXS', 0xFF90: 'BRGC',
    # system
    0xFFC0: 'STBC', 0xFFC4: 'MM', 0xFFC5: 'PW', 0xFFC6: 'RMC',
    # interrupts
    0xFFE0: 'IF0l', 0xFFE1: 'IF0h', 0xFFE4: 'MK0l', 0xFFE5: 'MK0h',
    0xFFE8: 'PR0l', 0xFFE9: 'PR0h', 0xFFEC: 'ISM0l', 0xFFED: 'ISM0h',
    0xFFF4: 'INTM0', 0xFFF5: 'INTM1', 0xFFF8: 'ISR',
}


class Bus:
    def __init__(self, rom, machine=None):
        self.machine = machine
        self.rom = rom                          # full 512 KiB image (or None)
        self.prog = bytearray(0x10000)
        if rom:
            self.prog[0:len(rom)] = rom[:0x10000]
        # internal RAM 0xFD00-0xFEFF (512 B)
        self.ram = bytearray(0x200)
        # external data space: 8 ROM banks (512 KiB) + 32 KiB onboard SRAM
        self.sram = bytearray(0x8000)
        self.sfr = {}
        for a in SFR:
            self.sfr[a] = 0
        # ports: P0/P3/P6 are outputs, P2/P7 are inputs
        self.port_in = {0x02: 0, 0x07: 0}
        self.port_in[0x02] = 0x02           # P2.1 high (CSI CS idle)
        self.port_in[0x07] = 0x10           # P7.4 high (no cartridge)

    # ---- program space --------------------------------------------------
    def read8(self, a):
        a &= 0xFFFF
        if a < 0xFD00:
            return self.prog[a]
        if a < 0xFF00:
            return self.ram[a - 0xFD00]
        return self._read_sfr(a)

    def write8(self, a, v):
        a &= 0xFFFF
        v &= 0xFF
        if a < 0xFD00:
            return
        if a < 0xFF00:
            self.ram[a - 0xFD00] = v
            return
        self._write_sfr(a, v)

    # ---- external (1 MiB) data space ------------------------------------
    def _bank(self):
        return (self.sfr[0xFF06] & 0x0F) << 16

    def read_ext8(self, a):
        bank = self.sfr[0xFF06] & 0x0F
        full = (bank << 16) | (a & 0xFFFF)
        if bank < 8 and self.rom and full < len(self.rom):
            return self.rom[full]           # ROM data banks 0-7
        if bank == 8:
            return self.sram[a & 0x7FFF]    # onboard SRAM 32 KiB
        return 0xFF                          # cartridge / unmapped

    def write_ext8(self, a, v):
        bank = self.sfr[0xFF06] & 0x0F
        if bank == 8:
            self.sram[a & 0x7FFF] = v & 0xFF

    # ---- SFR ------------------------------------------------------------
    def _read_sfr(self, a):
        if a == 0xFF00:                        # P0: output latch
            return self.sfr.get(a, 0)
        if a == 0xFF02 and self.machine:       # P2: arrows + CSI CS + 9V + MISO
            return self.machine.read_p2()
        if a == 0xFF07 and self.machine:       # P7: keypad columns + cartridge
            return self.machine.read_p7()
        if a == 0xFF02 or a == 0xFF07:
            return self.port_in.get(a, 0)
        if a == 0xFF06:
            return self.sfr[a]
        if a in (0xFFE1, 0xFFE0):              # interrupt request flags
            return self.sfr[a]
        if a == 0xFF8A:                        # ASIS: serial status
            return self.sfr.get(a, 0)
        return self.sfr.get(a, 0)

    def _write_sfr(self, a, v):
        if a == 0xFF02 or a == 0xFF07:
            return                            # inputs, ignore writes
        old = self.sfr.get(a, 0)
        self.sfr[a] = v
        if a == 0xFF00 and self.machine:      # P0: LCD data bus
            self.machine.port0_write(v)
        if a == 0xFF8E and self.machine:      # TXS: UART tx started
            self.machine.uart_tx(v)
        if a == 0xFF03 and self.machine:      # P3: LCD ctrl strobes + buzzer
            self.machine.port3_write(old, v)
