#!/usr/bin/env python3
"""
NEC 78K/II (uPD78213) CPU core for the Brother KH-970 CB-1 emulator.

Registers (memory-mapped at 0xFEE0-0xFEFF, bank 0 = 0xFEF8..0xFEFF):
  index: 0=X 1=A 2=C 3=B 4=E 5=D 6=L 7=H
  pairs: AX=(A hi,X lo) BC=(B hi,C lo) DE=(D hi,E lo) HL=(H hi,L lo)
PSW: bit0 CY, bit4 AC, bit6 Z, bit7 IE.

All memory access goes through `bus` (handles SFR / peripheral side-effects).
"""
REG = ('X', 'A', 'C', 'B', 'E', 'D', 'L', 'H')
PAIR_HI = (1, 3, 5, 7)   # AX,BC,DE,HL high-byte register index
PAIR_LO = (0, 2, 4, 6)


class CPU78K2:
    def __init__(self, bus):
        self.bus = bus
        self.reset()

    def reset(self):
        self.rbn = 0
        self.pc = 0
        self.sp = 0x0000
        self.psw = 0
        self.halted = False
        self.cycles = 0
        # 78K/II fetches the RESET vector from 0x0000 (little-endian)
        self.pc = self.rw(0x0000) if self.bus is not None else 0

    # ---- registers -----------------------------------------------------
    @property
    def rbase(self):
        return 0xFEF8 - (self.rbn << 3)

    def reg(self, i):
        return self.bus.read8(self.rbase + i)

    def sreg(self, i, v):
        self.bus.write8(self.rbase + i, v & 0xFF)

    def rp_get(self, rp):
        return (self.reg(PAIR_HI[rp]) << 8) | self.reg(PAIR_LO[rp])

    def rp_set(self, rp, v):
        self.sreg(PAIR_HI[rp], v >> 8)
        self.sreg(PAIR_LO[rp], v)

    @property
    def ax(self):
        return self.rp_get(0)

    @ax.setter
    def ax(self, v):
        self.rp_set(0, v)

    # ---- flags ---------------------------------------------------------
    def _set(self, bit, v):
        if v:
            self.psw |= (1 << bit)
        else:
            self.psw &= ~(1 << bit)

    @property
    def cy(self):
        return (self.psw >> 0) & 1

    def _set_cy(self, v):
        self._set(0, v)

    def _set_z(self, v):
        # Must test the full value: 16-bit ops (ADDW/SUBW/CMPW/SHRW/SHLW)
        # pass 16-bit results here, where only the low byte being zero is not
        # a zero result (e.g. CMPW AX,#0xFFFF with AX=0x00FF -> 0x0100 != 0).
        self._set(6, v == 0)

    def _set_ac_add(self, a, b, ci=0):
        self._set(4, (a & 0x0F) + (b & 0x0F) + ci > 0x0F)

    def _set_ac_sub(self, a, b, ci=0):
        self._set(4, (a & 0x0F) >= (b & 0x0F) + ci)

    # ---- bus helpers ----------------------------------------------------
    def rb(self, a):
        return self.bus.read8(a & 0xFFFF)

    def wb(self, a, v):
        self.bus.write8(a & 0xFFFF, v & 0xFF)

    def rw(self, a):
        return self.rb(a) | (self.rb(a + 1) << 8)

    def ww(self, a, v):
        self.wb(a, v)
        self.wb(a + 1, v >> 8)

    def ext_rb(self, a):
        return self.bus.read_ext8(a & 0xFFFF)

    def ext_wb(self, a, v):
        self.bus.write_ext8(a & 0xFFFF, v & 0xFF)

    def _mem_r(self, a, ext):
        return self.ext_rb(a) if ext else self.rb(a)

    def _mem_w(self, a, v, ext):
        (self.ext_wb if ext else self.wb)(a, v)

    # ---- fetch helpers --------------------------------------------------
    def _i8(self):
        v = self.rb(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def _i16(self):
        v = self.rw(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        return v

    def _rel(self, disp):
        if disp & 0x80:
            disp -= 0x100
        return (self.pc + disp) & 0xFFFF

    def _saddr(self, low):
        return 0xFF00 + low if low < 0x20 else 0xFE00 + low

    def _push8(self, v):
        self.sp = (self.sp - 1) & 0xFFFF
        self.wb(self.sp, v)

    def _pop8(self):
        v = self.rb(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        return v

    def _push16(self, v):
        self._push8(v >> 8)
        self._push8(v)

    def _pop16(self):
        lo = self._pop8()
        hi = self._pop8()
        return (hi << 8) | lo

    # ---- interrupt ------------------------------------------------------
    def request_int(self, slot):
        if not (self.psw & 0x80):
            return
        self._push16(self.pc)
        self._push8(self.psw)
        self.psw &= ~0x80
        self.pc = self.rw(slot * 2)

    # ---- step -----------------------------------------------------------
    def step(self):
        if self.halted:
            return 0
        self.cycles += 2
        op = self._i8()
        ext = False
        if op == 0x01:                      # '&' external-data prefix
            ext = True
            op = self._i8()
        return self._exec(op, ext)

    # =====================================================================
    def _exec(self, op, ext):
        b = self.bus

        # -------- control ------------------------------------------------
        if op == 0x00:
            return 2                                        # NOP

        if op == 0x05:
            o = b.read8(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            if (o & 0xFC) == 0xA8:                          # SEL RBn
                self.rbn = o & 3
                self.psw = (self.psw & ~0x0C) | ((o & 3) << 2)
                return 2
            if (o & 0xFE) == 0xE2:                          # MOVW AX,[DE]/[HL]
                a = self.rp_get(2 if not (o & 1) else 3)
                self.ax = self.ext_rb(a) | (self.ext_rb(a + 1) << 8) if ext else self.rw(a)
                return 2
            if (o & 0xFE) == 0xE6:                          # MOVW [DE]/[HL],AX
                a = self.rp_get(2 if not (o & 1) else 3)
                if ext:
                    self.ext_wb(a, self.ax)
                    self.ext_wb(a + 1, self.ax >> 8)
                else:
                    self.ww(a, self.ax)
                return 2
            if (o & 0xE8) == 0x08:                          # MULU / DIVUW r
                ri = o & 7
                r = self.reg(ri)
                if (o >> 4) & 1:
                    self._divuw(r, ri)
                else:
                    self._mulu(r)
                return 2
            if (o & 0xED) == 0x8C:                          # ROR4 / ROL4 [mem]
                a = self.rp_get(2 if not ((o >> 1) & 1) else 3)
                self._rot4(a, (o >> 4) & 1, ext)
                return 2
            if (o & 0xFE) == 0xC8:                          # INCW/DECW SP
                self.sp = (self.sp + (1 if not (o & 1) else -1)) & 0xFFFF
                return 2
            if (o & 0xF8) == 0x48:                          # BR rp
                self.pc = self.rp_get((o >> 1) & 3)
                return 2
            if (o & 0xF8) == 0x58:                          # CALL rp
                self._push16(self.pc)
                self.pc = self.rp_get((o >> 1) & 3)
                return 2
            return self._bad(op, ext)

        if op == 0x08:
            o = b.read8(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            bit = o & 7
            if (o & 0x90) == 0x00:                          # MOV1/AND1/OR1/XOR1 CY, saddr/sfr.bit
                a = self._saddr(self._i8()) if not (o & 8) else (0xFF00 + self._i8())
                self._bit_cy((o >> 5) & 3, (self.rb(a) >> bit) & 1)
                return 3
            if (o & 0xF0) == 0x10:                          # MOV1 saddr/sfr.bit, CY
                a = self._saddr(self._i8()) if not (o & 8) else (0xFF00 + self._i8())
                v = self.rb(a)
                self.wb(a, (v | (1 << bit)) if self.cy else (v & ~(1 << bit)))
                return 3
            if (o & 0xF0) == 0x30:                          # AND1 CY, /saddr/sfr.bit
                a = self._saddr(self._i8()) if not (o & 8) else (0xFF00 + self._i8())
                self._set_cy(self.cy & (0 if (self.rb(a) >> bit) & 1 else 1))
                return 3
            if (o & 0xF0) == 0x50:                          # OR1 CY, /saddr/sfr.bit
                a = self._saddr(self._i8()) if not (o & 8) else (0xFF00 + self._i8())
                self._set_cy(self.cy | (0 if (self.rb(a) >> bit) & 1 else 1))
                return 3
            if (o & 0xF0) == 0x70:                          # NOT1 saddr/sfr.bit
                a = self._saddr(self._i8()) if not (o & 8) else (0xFF00 + self._i8())
                self.wb(a, self.rb(a) ^ (1 << bit))
                return 3
            if (o & 0xE8) == 0x88:                          # SET1/CLR1 sfr.bit
                a = 0xFF00 + self._i8()
                v = self.rb(a)
                self.wb(a, (v | (1 << bit)) if (o >> 4) & 1 else (v & ~(1 << bit)))
                return 3
            if (o & 0xE8) == 0xA0:                          # BF / BTCLR saddr.bit, rel
                a = self._saddr(self._i8())
                disp = self._i8()
                tgt = self._rel(disp)
                if (self.rb(a) >> bit) & 1:
                    if (o >> 4) & 1:                        # BTCLR
                        self.wb(a, self.rb(a) & ~(1 << bit))
                        self.pc = tgt
                else:
                    if not ((o >> 4) & 1):                  # BF
                        self.pc = tgt
                return 4
            if (o & 0xE8) == 0xA8:                          # BF/BT sfr.bit, rel
                a = 0xFF00 + self._i8()
                disp = self._i8()
                tgt = self._rel(disp)
                if (self.rb(a) >> bit) & 1:
                    if (o >> 4) & 1:
                        self.pc = tgt
                else:
                    if not ((o >> 4) & 1):
                        self.pc = tgt
                return 4
            if (o & 0xF8) == 0xD0:                          # BTCLR sfr.bit, rel
                a = 0xFF00 + self._i8()
                disp = self._i8()
                tgt = self._rel(disp)
                if (self.rb(a) >> bit) & 1:
                    self.wb(a, self.rb(a) & ~(1 << bit))
                    self.pc = tgt
                return 4
            return self._bad(op, ext)

        if op == 0x02:
            return self._psw_bitop()
        if op == 0x03:
            return self._ax_bitop()

        if op == 0x09:
            o = b.read8(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            if o == 0xC0:                                   # MOV STBC,#imm
                self.pc = (self.pc + 1) & 0xFFFF
                self.wb(0xFFC0, self._i8())
                return 3
            if o == 0xF0:                                   # MOV A,!addr16 / &!addr16
                a = self._i16()
                self.sreg(1, self._mem_r(a, ext))
                return 4
            if o == 0xF1:                                   # MOV !addr16,A / &!addr16,A
                a = self._i16()
                self._mem_w(a, self.reg(1), ext)
                return 4
            return self._bad(op, ext)

        if (op & 0xFE) == 0x4A:                             # DI/EI
            self._set(7, op & 1)
            return 2

        if (op & 0xF8) == 0xB8:                             # MOV r,#imm
            self.sreg(op & 7, self._i8())
            return 2

        if op == 0x3A:                                      # MOV saddr,#imm
            a = self._saddr(self._i8())
            self.wb(a, self._i8())
            return 3
        if op == 0x2B:                                      # MOV sfr,#imm
            a = 0xFF00 + self._i8()
            self.wb(a, self._i8())
            return 3

        if op == 0x24:
            o = b.read8(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            if (o & 0x88) == 0x00:                          # MOV r,r'
                self.sreg(o >> 4, self.reg(o & 7))
                return 2
            if (o & 0x99) == 0x08:                          # MOVW rp,rp'
                self.rp_set((o >> 5) & 3, self.rp_get((o >> 1) & 3))
                return 2
            return self._bad(op, ext)

        if op == 0x25 and (b.read8(self.pc) & 0x88) == 0:   # XCH r,r'
            o = b.read8(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            a, c = self.reg(o >> 4), self.reg(o & 7)
            self.sreg(o >> 4, c)
            self.sreg(o & 7, a)
            return 2

        if (op & 0xF8) == 0xD0:                             # MOV A,r
            self.sreg(1, self.reg(op & 7))
            return 2
        if (op & 0xF8) == 0xD8:                             # XCH A,r
            r = op & 7
            t = self.reg(r)
            self.sreg(r, self.reg(1))
            self.sreg(1, t)
            return 2

        if op == 0x20:                                      # MOV A,saddr
            self.sreg(1, self.rb(self._saddr(self._i8())))
            return 2
        if op == 0x22:                                      # MOV saddr,A
            self.wb(self._saddr(self._i8()), self.reg(1))
            return 2
        if op == 0x10:                                      # MOV A,sfr
            self.sreg(1, self.rb(0xFF00 + self._i8()))
            return 2
        if op == 0x12:                                      # MOV sfr,A
            self.wb(0xFF00 + self._i8(), self.reg(1))
            return 2
        if op == 0x38:                                      # MOV saddr,saddr'
            d = self._saddr(self._i8())
            s = self._saddr(self._i8())
            self.wb(d, self.rb(s))
            return 3
        if op == 0x39:                                      # XCH saddr,saddr'
            d = self._saddr(self._i8())
            s = self._saddr(self._i8())
            a, c = self.rb(d), self.rb(s)
            self.wb(d, c)
            self.wb(s, a)
            return 3
        if op == 0x21:                                      # XCH A,saddr/sfr
            a = (0xFF00 + self._i8()) if ext else self._saddr(self._i8())
            t = self.rb(a)
            self.wb(a, self.reg(1))
            self.sreg(1, t)
            return 2

        if (op & 0xF8) == 0x58 and (op & 7) < 6:            # MOV A,[mem]
            a = self._mem_ind(op & 7)
            self.sreg(1, self._mem_r(a, ext))
            return 2
        if (op & 0xF8) == 0x50 and (op & 7) < 6:            # MOV [mem],A
            a = self._mem_ind(op & 7)
            self._mem_w(a, self.reg(1), ext)
            return 2

        if op in (0x16, 0x06, 0x0A):                        # MOV/XCH/ALU A,[mem]
            return self._mem_long(op, ext)

        # -------- MOVW ---------------------------------------------------
        if (op & 0xF8) == 0x60:                             # MOVW rp,#imm
            self.rp_set((op >> 1) & 3, self._i16())
            return 3
        if op == 0x0C:                                      # MOVW saddrp,#imm
            a = self._saddr(self._i8())
            self.ww(a, self._i16())
            return 3
        if op == 0x0B:                                      # MOVW sfrp/SP,#imm
            a = 0xFF00 + self._i8()
            self.ww(a, self._i16())
            if a == 0xFF1C:
                self.sp = self.rw(a)
            return 3
        if op == 0x1C:                                      # MOVW AX,saddrp
            self.ax = self.rw(self._saddr(self._i8()))
            return 2
        if op == 0x1A:                                      # MOVW saddrp,AX
            self.ww(self._saddr(self._i8()), self.ax)
            return 2
        if (op & 0xFD) == 0x11:                             # MOVW AX,sfrp / sfrp,AX / SP
            a = 0xFF00 + self._i8()
            if a == 0xFF1C:
                if not (op & 2):
                    self.ax = self.sp
                else:
                    self.sp = self.ax
            else:
                if not (op & 2):
                    self.ax = self.rw(a)
                else:
                    self.ww(a, self.ax)
            return 2

        # -------- ALU ----------------------------------------------------
        if (op & 0xF8) == 0xA8:                             # op A,#imm
            self._alu(op & 7, self._i8())
            return 2
        if (op & 0xF8) == 0x68:                             # op saddr/sfr,#imm
            a = (0xFF00 + self._i8()) if ext else self._saddr(self._i8())
            self._alu_mem(op & 7, a, self._i8())
            return 3
        if op in (0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F):
            o = b.read8(self.pc)
            if (o & 0x88) == 0:                             # op r,r'
                self.pc = (self.pc + 1) & 0xFFFF
                self._alu(op & 7, self.reg(o & 7), dst=o >> 4)
                return 2
            if op in (0x88, 0x8A, 0x8F) and (o & 0xF9) == 0x08:  # ADDW/SUBW/CMPW AX,rp
                self.pc = (self.pc + 1) & 0xFFFF
                self._alu16({0x88: 0, 0x8A: 1, 0x8F: 2}[op], self.rp_get((o >> 1) & 3))
                return 2
            return self._bad(op, ext)
        if (op & 0xF8) == 0x98:                             # op A,saddr/sfr
            a = (0xFF00 + self._i8()) if ext else self._saddr(self._i8())
            self._alu(op & 7, self.rb(a))
            return 2
        if (op & 0xF8) == 0x78:                             # op A,saddr,saddr'
            d = self._saddr(self._i8())
            s = self._saddr(self._i8())
            self._alu(op & 7, self.rb(s), dst_addr=d)
            return 3

        if op in (0x2D, 0x2E, 0x2F):                        # ADDW/SUBW/CMPW AX,#imm
            k = {0x2D: 0, 0x2E: 1, 0x2F: 2}[op]
            self._alu16(k, self._i16())
            return 3
        if op in (0x1D, 0x1E, 0x1F):                        # ADDW/SUBW/CMPW AX,saddrp/sfrp
            a = (0xFF00 + self._i8()) if ext else self._saddr(self._i8())
            self._alu16(op - 0x1D, self.rw(a))
            return 2

        # -------- INC/DEC ------------------------------------------------
        if (op & 0xF0) == 0xC0:                             # INC/DEC r
            self._incdec(op & 7, (op >> 3) & 1)
            return 2
        if (op & 0xFE) == 0x26:                             # INC/DEC saddr
            self._incdec_mem(self._saddr(self._i8()), op & 1)
            return 2
        if (op & 0xF4) == 0x44:                             # INCW/DECW rp
            rp = op & 3
            self.rp_set(rp, (self.rp_get(rp) + (1 if not ((op >> 3) & 1) else -1)) & 0xFFFF)
            return 2

        # -------- shifts --------------------------------------------------
        if (op & 0xFE) == 0x30:
            o = b.read8(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            self._shift(op & 1, o)
            return 2
        if (op & 0xFE) == 0x06:                             # ADJBA/ADJBS
            self._adj(op & 1)
            return 2

        # -------- bit (CY) ------------------------------------------------
        if op == 0x40: self._set_cy(0); return 2             # CLR1 CY
        if op == 0x41: self._set_cy(1); return 2             # SET1 CY
        if op == 0x42: self._set_cy(self.cy ^ 1); return 2   # NOT1 CY
        if (op & 0xE8) == 0xA0:                             # SET1/CLR1 saddr.bit
            a = self._saddr(self._i8())
            bit = op & 7
            v = self.rb(a)
            self.wb(a, (v | (1 << bit)) if (op >> 4) & 1 else (v & ~(1 << bit)))
            return 2

        # -------- branches -------------------------------------------------
        if (op & 0xFC) == 0x80:                             # BNZ/BZ/BNC/BC rel
            disp = self._i8()
            tgt = self._rel(disp)
            k = op & 3
            take = ((self.psw & 0x40) == 0) if k == 0 else \
                   ((self.psw & 0x40) != 0) if k == 1 else \
                   (self.cy == 0) if k == 2 else (self.cy == 1)
            if take:
                self.pc = tgt
            return 2
        if op == 0x14:                                      # BR rel
            self.pc = self._rel(self._i8())
            return 2
        if op == 0x2C:                                      # BR !addr16
            self.pc = self._i16()
            return 3
        if (op & 0xF8) == 0x70:                             # BT saddr.bit,rel
            a = self._saddr(self._i8())
            disp = self._i8()
            tgt = self._rel(disp)
            if (self.rb(a) >> (op & 7)) & 1:
                self.pc = tgt
            return 3
        if (op & 0xFE) == 0x32:                             # DBNZ C/B,rel
            r = 2 if not (op & 1) else 3                     # C=2, B=3
            self.sreg(r, (self.reg(r) - 1) & 0xFF)
            disp = self._i8()
            tgt = self._rel(disp)
            if self.reg(r):
                self.pc = tgt
            return 2
        if op == 0x3B:                                      # DBNZ saddr,rel
            a = self._saddr(self._i8())
            v = (self.rb(a) - 1) & 0xFF
            self.wb(a, v)
            disp = self._i8()
            tgt = self._rel(disp)
            if v:
                self.pc = tgt
            return 3

        # -------- call / return -------------------------------------------
        if op == 0x28:                                      # CALL !addr16
            a = self._i16()
            self._push16(self.pc)
            self.pc = a
            return 3
        if (op & 0xF8) == 0x90:                             # CALLF !addr11
            nn = self._i8()
            self._push16(self.pc)
            self.pc = 0x0800 + nn + ((op & 7) << 8)
            return 2
        if (op & 0xE0) == 0xE0:                             # CALLT [addr5]
            tgt = self.rw(0x0040 + ((op & 0x1F) << 1))
            self._push16(self.pc)
            self.pc = tgt
            return 2
        if (op & 0xF6) == 0x56:                             # RET/RETI/BRK/RETB
            idx = ((op & 8) >> 2) + (op & 1)
            if idx == 0:
                self.pc = self._pop16()
            elif idx == 1:
                self.psw = self._pop8()
                self.rbn = (self.psw >> 2) & 3
                self.pc = self._pop16()
            elif idx == 2:
                self.request_int(0x1F)
            else:
                self.pc = self._pop16()
            return 2

        # -------- stack ----------------------------------------------------
        if (op & 0xF4) == 0x34:                             # PUSH/POP rp
            rp = op & 3
            if (op >> 3) & 1:
                self._push16(self.rp_get(rp))
            else:
                self.rp_set(rp, self._pop16())
            return 2
        if (op & 0xFE) == 0x48:                             # PUSH/POP PSW
            if op & 1:
                self._push8(self.psw)
            else:
                self.psw = self._pop8()
                self.rbn = (self.psw >> 2) & 3
            return 2
        if op == 0x29:                                      # PUSH sfr
            self._push8(self.rb(0xFF00 + self._i8()))
            return 2
        if op == 0x43:                                      # POP sfr
            self.wb(0xFF00 + self._i8(), self._pop8())
            return 2

        return self._bad(op, ext)

    # ================= helpers =================
    def _bad(self, op, ext):
        raise NotImplementedError('opcode 0x%02X at 0x%04X (ext=%s)' % (op, (self.pc - 1) & 0xFFFF, ext))

    def _mem_ind(self, k):
        # k: 0 [DE+] 1 [HL+] 2 [DE-] 3 [HL-] 4 [DE] 5 [HL]
        if k == 0:
            a = self.rp_get(2)
            self.rp_set(2, (a + 1) & 0xFFFF)
            return a
        if k == 1:
            a = self.rp_get(3)
            self.rp_set(3, (a + 1) & 0xFFFF)
            return a
        if k == 2:
            a = self.rp_get(2)
            self.rp_set(2, (a - 1) & 0xFFFF)
            return a
        if k == 3:
            a = self.rp_get(3)
            self.rp_set(3, (a - 1) & 0xFFFF)
            return a
        if k == 4:
            return self.rp_get(2)
        return self.rp_get(3)

    def _mem_long(self, op, ext):
        o = self._i8()
        sub = o & 0x8F
        kind = o >> 4
        if op == 0x16:
            a = self._mem_ind(kind)
        elif op == 0x06:
            off = self._i8()
            base = kind & 3
            if base == 0:
                a = (self.rp_get(2) + off) & 0xFFFF
            elif base == 1:
                a = (self.sp + off) & 0xFFFF
            else:
                a = (self.rp_get(3) + off) & 0xFFFF
        else:  # 0x0A
            base = self._i16()
            kind2 = kind & 3
            if kind2 == 0:
                rp = self.rp_get(2)
            elif kind2 == 1:
                rp = self.reg(1)
            elif kind2 == 2:
                rp = self.rp_get(3)
            else:
                rp = self.reg(3)
            a = (base + rp) & 0xFFFF
        if sub == 0x00:
            self.sreg(1, self._mem_r(a, ext))
        elif sub == 0x80:
            self._mem_w(a, self.reg(1), ext)
        elif sub == 0x04:
            t = self._mem_r(a, ext)
            self._mem_w(a, self.reg(1), ext)
            self.sreg(1, t)
        elif (sub & 0xF8) == 0x08:           # ALU A,[mem]: ADD/ADDC/SUB/SUBC/AND/XOR/OR/CMP
            self._alu(o & 7, self._mem_r(a, ext))
        return {0x16: 2, 0x06: 3, 0x0A: 4}[op]

    def _alu(self, k, src, dst=1, dst_addr=None):
        a = self.reg(dst) if dst_addr is None else self.rb(dst_addr)
        if k == 0:        # ADD
            r = (a + src) & 0xFF
            self._set_cy(a + src > 0xFF)
            self._set_ac_add(a, src)
        elif k == 1:      # ADDC
            ci = self.cy
            r = (a + src + ci) & 0xFF
            self._set_cy(a + src + ci > 0xFF)
            self._set_ac_add(a, src, ci)
        elif k == 2:      # SUB
            r = (a - src) & 0xFF
            self._set_cy(a < src)
            self._set_ac_sub(a, src)
        elif k == 3:      # SUBC
            ci = self.cy
            r = (a - src - ci) & 0xFF
            self._set_cy(a < src + ci)
            self._set_ac_sub(a, src, ci)
        elif k == 4:      # AND
            r = a & src
            self._set_cy(0)
        elif k == 5:      # XOR
            r = a ^ src
            self._set_cy(0)
        elif k == 6:      # OR
            r = a | src
            self._set_cy(0)
        else:             # CMP
            r = (a - src) & 0xFF
            self._set_cy(a < src)
            self._set_ac_sub(a, src)
        if k == 7:                  # CMP: flags only, never write back
            self._set_z(r)
            return
        if dst_addr is None:
            self.sreg(dst, r)
        else:
            self.wb(dst_addr, r)
        self._set_z(r)

    def _alu_mem(self, k, addr, imm):
        self._alu(k, imm, dst_addr=addr)

    def _alu16(self, k, v):
        ax = self.ax
        if k == 0:
            r = (ax + v) & 0xFFFF
            self._set_cy(ax + v > 0xFFFF)
            self.ax = r
        elif k == 1:
            r = (ax - v) & 0xFFFF
            self._set_cy(ax < v)
            self.ax = r
        else:  # CMPW
            r = (ax - v) & 0xFFFF
            self._set_cy(ax < v)
        self._set_z(r)

    def _incdec(self, r, dec):
        v = self.reg(r)
        n = (v - 1 if dec else v + 1) & 0xFF
        self.sreg(r, n)
        self._set_z(n)
        self._set(4, (v & 0x0F) == (0x00 if dec else 0x0F))

    def _incdec_mem(self, a, dec):
        v = self.rb(a)
        n = (v - 1 if dec else v + 1) & 0xFF
        self.wb(a, n)
        self._set_z(n)
        self._set(4, (v & 0x0F) == (0x00 if dec else 0x0F))

    def _shift(self, odd, o):
        ra = ((o >> 6) & 3) + (odd << 2)
        n = (o >> 3) & 7
        if n == 0:
            n = 8
        is_pair = (ra & 3) == 3                # SHRW (3) / SHLW (7) are 16-bit
        if is_pair:
            rp = (o >> 1) & 3
            v = self.rp_get(rp)
            bits = 16
        else:
            r = o & 7
            v = self.reg(r)
            bits = 8
        top = bits - 1
        mask = (1 << bits) - 1
        for _ in range(n):
            cy = self.cy
            if ra == 0:      # RORC: rotate right through carry
                nc = v & 1
                v = ((v >> 1) | (cy << top)) & mask
            elif ra == 1:    # ROR: rotate right (bit0 wraps to top)
                nc = v & 1
                v = ((v >> 1) | (nc << top)) & mask
            elif ra == 2:    # SHR: logical shift right
                nc = v & 1
                v = (v >> 1) & mask
            elif ra == 3:    # SHRW: logical shift right (16-bit)
                nc = v & 1
                v = (v >> 1) & mask
            elif ra == 4:    # ROLC: rotate left through carry
                nc = (v >> top) & 1
                v = ((v << 1) | cy) & mask
            elif ra == 5:    # ROL: rotate left (bit top wraps to bit0)
                nc = (v >> top) & 1
                v = ((v << 1) | nc) & mask
            elif ra == 6:    # SHL: logical shift left
                nc = (v >> top) & 1
                v = (v << 1) & mask
            else:            # SHLW: logical shift left (16-bit)
                nc = (v >> top) & 1
                v = (v << 1) & mask
            self._set_cy(nc)
        if is_pair:
            self.rp_set(rp, v)
        else:
            self.sreg(r, v)
        self._set_z(v)

    def _mulu(self, r):
        self.ax = (self.reg(1) * r) & 0xFFFF
        self._set_cy(0)

    def _divuw(self, r, ri):
        if r == 0:
            self._set_cy(1)
            self.ax = 0xFFFF
            return
        ax = self.ax
        self.ax = ax // r
        self.sreg(ri, ax % r)      # remainder -> divisor register
        self._set_cy(0)

    def _rot4(self, a, left, ext):
        v = self._mem_r(a, ext)
        x = ((self.reg(1) & 0x0F) << 8) | v
        if left:
            x = ((x << 4) | (x >> 8)) & 0xFFF
        else:
            x = ((x >> 4) | ((x & 0x0F) << 8)) & 0xFFF
        self.sreg(1, (self.reg(1) & 0xF0) | ((x >> 8) & 0x0F))
        self._mem_w(a, x & 0xFF, ext)

    def _adj(self, sub):
        a = self.reg(1)
        if sub:
            if self.psw & 0x10 or a > 0x99:
                a = (a - 0x60) & 0xFF
                self._set_cy(1)
            if self.psw & 0x10 or (a & 0x0F) > 0x09:
                a = (a - 0x06) & 0xFF
        else:
            if self.psw & 0x10 or a > 0x99:
                a = (a + 0x60) & 0xFF
                self._set_cy(1)
            if self.psw & 0x10 or (a & 0x0F) > 0x09:
                a = (a + 0x06) & 0xFF
        self.sreg(1, a)
        self._set_z(a)

    def _bit_cy(self, k, bit):
        c = self.cy
        if k == 0:
            self._set_cy(bit)
        elif k == 1:
            self._set_cy(c & bit)
        elif k == 2:
            self._set_cy(c | bit)
        else:
            self._set_cy(c ^ bit)

    def _psw_bitop(self):
        o = self._i8()
        bit = o & 7
        if (o & 0x90) == 0x00:      # MOV1/AND1/OR1/XOR1 CY, PSW.bit
            self._bit_cy((o >> 5) & 3, (self.psw >> bit) & 1)
        elif (o & 0xF0) == 0x10:    # MOV1 PSW.bit, CY
            if self.cy:
                self.psw |= (1 << bit)
            else:
                self.psw &= ~(1 << bit)
        elif (o & 0xF0) == 0x30:    # AND1 CY, /PSW.bit
            self._set_cy(self.cy & (0 if (self.psw >> bit) & 1 else 1))
        elif (o & 0xF0) == 0x50:    # OR1 CY, /PSW.bit
            self._set_cy(self.cy | (0 if (self.psw >> bit) & 1 else 1))
        elif (o & 0xF0) == 0x70:    # NOT1 PSW.bit
            self.psw ^= (1 << bit)
        elif (o & 0xE8) == 0x80:    # SET1/CLR1 PSW.bit
            if (o >> 4) & 1:
                self.psw |= (1 << bit)
            else:
                self.psw &= ~(1 << bit)
        elif (o & 0xF8) == 0xA0:    # BT/BF PSW.bit,rel
            disp = self._i8()
            tgt = self._rel(disp)
            if ((self.psw >> bit) & 1) == ((o >> 4) & 1):
                self.pc = tgt
            return 4
        elif (o & 0xF8) == 0xD0:    # BTCLR PSW.bit,rel
            disp = self._i8()
            tgt = self._rel(disp)
            if (self.psw >> bit) & 1:
                self.psw &= ~(1 << bit)
                self.pc = tgt
            return 4
        return 2

    def _ax_bitop(self):
        o = self._i8()
        bit = o & 7
        r = (o >> 3) & 1                    # X=0, A=1
        v = self.reg(r)
        if (o & 0x90) == 0x00:
            self._bit_cy((o >> 5) & 3, (v >> bit) & 1)
        elif (o & 0xF0) == 0x10:
            self.sreg(r, (v | (1 << bit)) if self.cy else (v & ~(1 << bit)))
        elif (o & 0xF0) == 0x30:
            self._set_cy(self.cy & (0 if (v >> bit) & 1 else 1))
        elif (o & 0xF0) == 0x50:
            self._set_cy(self.cy | (0 if (v >> bit) & 1 else 1))
        elif (o & 0xF0) == 0x70:
            self.sreg(r, v ^ (1 << bit))
        elif (o & 0xE0) == 0x80:
            if (o >> 4) & 1:
                self.sreg(r, v | (1 << bit))
            else:
                self.sreg(r, v & ~(1 << bit))
        elif (o & 0xE0) == 0xA0:            # BT/BF X/A.bit,rel
            disp = self._i8()
            tgt = self._rel(disp)
            if ((v >> bit) & 1) == ((o >> 4) & 1):
                self.pc = tgt
            return 3
        elif (o & 0xF0) == 0xD0:            # BTCLR X/A.bit,rel
            disp = self._i8()
            tgt = self._rel(disp)
            if (v >> bit) & 1:
                self.sreg(r, v & ~(1 << bit))
                self.pc = tgt
            return 3
        return 2
