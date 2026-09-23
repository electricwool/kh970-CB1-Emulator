# Fonts & glyphs — CB-1 display subsystem

This is the canonical reference for the CB-1's sprite/font data. Getting these
exactly right is required before the LCD can render text correctly. All offsets
are **ROM file offsets** (`kh970CB1v1.0-AM27C040@DIP32.bin`, 512 KiB).

## CRITICAL: fonts are COLUMN-MAJOR (LCD-native)

The sprite data is stored **column-major**: each byte is a VERTICAL column of
8 pixels, **bit 0 = top row**. A glyph `w` wide × `h` tall occupies
`w × ceil(h/8)` bytes; column `c` lives at byte offset `c × ceil(h/8)`, low
byte = top 8 rows. (This was previously mis-documented as row-major bit7=left;
the 8×8 `'0'` = `00 3e 41 41 41 41 3e 00` renders correctly ONLY as columns.)

## Display geometry (hardware)

- 2× SED1520 (user reports **SED1520F** variant), 61 columns each → **122 wide**.
- **4 pages** used (32 rows tall). Page command `0xB8–0xBB`; `lcd01_setRAMpage`
  masks the page `& 0x03`, and `lcd_write` rejects rows ≥ 32.
- **Column command = `0x00–0x3F`** (NOT `0x40–0x7F`). `lcd_setcolumn` sends the
  raw column value.
- **Data byte = 8 vertical pixels, bit 0 = top row** of the page.
- Display ON/OFF = `0xAF`/`0xAE`.
- Chip split: firmware column < 61 → `lcd0_ctrl`/`lcd0_data` (P3.6 strobe),
  column ≥ 61 → `lcd1_ctrl`/`lcd1_data` (P3.7 strobe). `lcd01_ctrl`/`lcd01_setRAMpage`
  strobe **both** chips (sets page on both).

## Sprite config table — ROM 0x0242, 7-byte entries

`read_ext_cfg(A)` at 0x170D: entry = `0x0242 + A*7`, fields
`(width, height, cfg_id, base_lo, base_hi, off_lo, off_hi)`.
`cfg_id` = data bank for the sprite data (bank 2 = ROM 0x020000).

| cfg | w  | h  | id | base | offset | notes |
|-----|----|----|----|------|--------|-------|
| 0   | 29 | 15 | 02 | 0x0000 | 0x1f00 | large **kanji** glyphs (language option) |
| 1   | 10 | 16 | 02 | 0x2000 | 0x0000 | **icon sprites** (arrows/boxes) |
| 2   | 16 | 16 | 02 | 0x3000 | 0x5000 | **JAPANESE font** (Katakana/Kanji) |
| 3   | 8  | 8  | 02 | 0x5800 | 0x0000 | **ENGLISH text font** |
| 4   | 16 | 16 | 02 | 0x6000 | 0x7800 | large sprite (arrow) |
| 5   | 5  | 10 | 02 | 0x8000 | 0x0000 | **small font** (5×10) |
| 6   | 6  | 8  | 02 | 0x8800 | 0x0000 | **small font** (6×8) |
| 7   | 32 | 32 | 02 | 0x9000 | 0x0000 | **pattern-design curve icons** (32×32) |
| 8   | 16 | 7  | 02 | 0xa000 | 0x0000 | 16×7 sprite |

Also: `disp_read_def` (0x17BA) sets a **6×6 symbol font at bank 0, base 0x0200**
(width 6, height 6) — this is NOT in the config table.

**Fonts (5 total):** cfg 2 = Japanese (16×16), cfg 3 = English (8×8),
cfg 5 = small (5×10), cfg 6 = small (6×8), `disp_read_def` = symbols (6×6).
Everything else in the config table is sprites/icons (not fonts).

Real glyph counts (auto-detected: dumping stops at the first **fully-0xFF**
glyph = erased EPROM, NOT a real glyph):

| config | nominal | real glyphs |
|--------|---------|-------------|
| cfg 0  | 29×15   | 106 logo sprites |
| cfg 1  | 10×16   | 22 icon sprites |
| cfg 2  | 16×16   | **223 base + 59 offset** |
| cfg 3  | 8×8     | **55** (0–54) |
| cfg 4  | 16×16   | 149 sprites |
| cfg 5  | 5×10    | **10** (digits 0–9) |
| cfg 6  | 6×8     | **13** (0–12) |
| cfg 7  | 32×32   | 12 sprites |
| cfg 8  | 16×7    | 3 sprites |
| 6×6 def | 6×6    | **31** (0–30) |

## Sprite address calculation — `disp_sprite_base` (0x1762)

```
stride        = columns × ceil(rows / 8)   (= w × ceil(h/8), column-major)
sprite_addr   = base + glyph_index * stride   (in data bank = cfg_id)
```
NOTE: the firmware names are swapped — `sprite_height` (0xFEAC) holds the
column count (visual WIDTH) and `sprite_width` (0xFEAD) holds the row count
(visual HEIGHT). Config-table byte0 = columns, byte1 = rows.

- cfg 0 (29×15): stride = 29×2 = 58 → glyph i at ROM `0x020000 + i*58`.
- cfg 1 (10×16): stride = 10×2 = 20 → sprite i at ROM `0x022000 + i*20`.
- cfg 2 (16×16): stride = 16×2 = 32 → glyph i at ROM `0x023000 + i*32`.
- cfg 3 (8×8):   stride = 8×1 = 8  → glyph i at ROM `0x025800 + i*8`.
- cfg 4 (16×16): stride = 16×2 = 32 → sprite i at ROM `0x026000 + i*32`.
- cfg 5 (5×10):  stride = 5×2 = 10 → glyph i at ROM `0x028000 + i*10`.
- cfg 6 (6×8):   stride = 6×1 = 6  → glyph i at ROM `0x028800 + i*6`.
- cfg 7 (32×32): stride = 32×4 = 128 → sprite i at ROM `0x029000 + i*128`.
- cfg 8 (16×7):  stride = 16×1 = 16 → sprite i at ROM `0x02a000 + i*16`.
- 6×6 (disp_read_def): stride = 6×1 = 6 → glyph i at ROM `0x000200 + i*6`.

All sprite data is **column-major, bit 0 = top row** of the column byte.
(Verified: the 8×8 `'0'` = `00 3e 41 41 41 41 3e 00` renders as a proper `0`
when byte = vertical column; the row-major interpretation gives a wrong glyph.)

## Character → glyph index map — `string_machine_table` (0xE4CB)

| chars | index |
|-------|-------|
| `'0'`–`'9'` (0x30–0x39) | `c − 0x30` (0–9) |
| `'A'`–`'Z'` (0x41+) | `c − 0x31` (16–41) |
| `','` | 10 |
| `'('` | 11 |
| `')'` | 12 |
| `'-'` | 13 |
| `'/'` | 14 |
| `'.'` | 15 |
| `'&'` | 42 |
| `' '` (0x20) | blank (`disp_flags.5` set) |

So in the 8×8 font: `'A'`=16, `'B'`=17, `'C'`=18, `'D'`=19, `'E'`=20,
`'S'`=34, `'T'`=35, `'V'`=37, `'R'`=18+? (verify), `'K'`=27, `'H'`=24, `'L'`=28, …

## Font 1 — JAPANESE 16×16 (cfg 2) — ROM 0x023000, 32 bytes/glyph

Column-major: 2 bytes/column × 16 columns. **223 glyphs** at base `0x023000`,
plus **59 glyphs** at the offset range `0x025000`. Katakana/Hiragana/Kanji.

Indexing (from `disp_sprite_base`): base glyph `i` (index `0x9000+i`) → ROM
`0x023000 + i*32`; offset glyph `i` (index `0x8000+i`, drawn HALF-HEIGHT) →
ROM `0x025000 + i*32`. Full atlas in `fonts_atlas.txt`.

### Language-selection option glyphs (NOT cfg 2!)

The language-selection screen is drawn by the sprite-sequence system
(`sub_11d9`) using **cfg 0 (29×15)** — NOT cfg 2. Each option is ONE
29×15 glyph holding the full language name in its native script, with a
built-in dotted border. The menu shows 3 options at once and scrolls
horizontally with the arrow keys to reveal **7 total languages**
(日本語, English, Deutsch, 中文, and Dutch/Spanish/French).

The sequence data (bank 1, ROM 0x010000) maps:

| sprite | cfg0 glyph | language |
|--------|-----------|----------|
| `0x9000` | 0 | rounded-rect selection frame |
| `0x8000` | offset 0 | left dotted border (half height) |
| `0x9062` | 98 | **日本語** (Japanese) |
| `0x9063` | 99 | **English** |
| `0x9064` | 100 | **Deutsch** (German) |
| `0x9065` | 101 | **Nederlands** (Dutch) |
| `0x9066` | 102 | **Español** (Spanish) |
| `0x9067` | 103 | **Français** (French) |
| `0x9068` | 104 | **中文** (Chinese) |
| `0x9069` | 105 | extra glyph (TBD) |
| `0x906A` | 106 | extra glyph (TBD) |
| `0x906B` | 107 | extra glyph (TBD) |
| `0x906C` | 108 | solid fill (erase) |
| `0x8001` | offset 1 | right dotted border (half height) |

So the 29×15 "logo" font (cfg 0) actually holds large language-option
glyphs (29 wide × 15 tall, stride 58) with built-in dotted borders. The
16×16 cfg 2 font is a separate regular Japanese font used elsewhere in the UI.

Note: the selection frame (glyph 0) is drawn over the selected option and
**blinks** — the ROM periodically clears/redraws it directly on the LCD RAM
(not the display buffer), so screenshots must read the display buffer
(`Machine.buffer_pixels()`) to capture the steady "on" state.

## Font 2 — ENGLISH 8×8 (cfg 3) — ROM 0x025800, 8 bytes/glyph

Column-major (byte = vertical column, bit0=top). **55 glyphs** (0–54); glyphs
55+ are `0xFF` (erased, unused). Indices 0–42 = digits/uppercase/punctuation
(see char map); 43–54 = diacritics, box-drawing (dotted/dashed), and symbols
(e.g. `¨`, `_`, `:` — NOT lowercase letters).

Example glyphs (index → column bytes):

```
16 'A' = 7c 12 11 11 11 12 7c 00
17 'B' = 7f 49 49 49 49 49 36 00
18 'C' = 3e 41 41 41 41 41 22 00
19 'D' = 7f 41 41 41 41 22 1c 00
20 'E' = 7f 49 49 49 49 49 41 00
34 'S' = 26 49 49 49 49 49 32 00
35 'T' = 01 01 01 7f 01 01 01 00
```

## Font 3 — 5×10 small font (cfg 5) — ROM 0x028000, 10 bytes/glyph

2 bytes/column × 5 columns. **10 glyphs** = digits `0`–`9` (glyphs 10+ are
0xFF erased).

## Font 4 — 6×8 small font (cfg 6) — ROM 0x028800, 8 bytes/glyph

1 byte/column, 6 bits used. **13 glyphs** (0–12); glyphs 13+ are 0xFF.

## Font 5 — 6×6 symbol font (disp_read_def) — ROM 0x0200, 6 bytes/glyph

Column-major, bit0=top, width 6. **31 glyphs** (0–30). Symbol/icon font (not
ASCII).

## 10×16 icon sprites (cfg 1) — ROM 0x022000, 32 bytes/sprite

2 bytes/column × 10 columns. **22 sprites** (0–21). Used by the
language-selection icons and menu borders.

## 32×32 pattern-design curve icons (cfg 7) — ROM 0x029000, 128 bytes/sprite

4 bytes/column × 32 columns. **12 sprites** (0–11). These are the
**pattern-design option icons** — each glyph shows a specialized curve /
fill style used when designing knit patterns (the CB-1 is a knitting
computer). Not a font: they are large preview glyphs for curve-type selection.

| glyph | meaning |
|-------|---------|
| 0 | straight X-axis (horizontal line) |
| 1 | straight Y-axis (vertical line) |
| 2 | diagonal down |
| 3 | diagonal up |
| 4 | curve down — convex |
| 5 | curve up — convex |
| 6 | curve up — concave |
| 7 | curve down — concave |
| 8 | back neck hole |
| 9 | front neck hole |
| 10 | sleeve insert — straight |
| 11 | sleeve insert — curve |

## Render modes (all 10)

A "render mode" is a sprite config selected before calling `disp_write`. The
config loader functions are laid out contiguously at `0xD0C2 + cfg*8` (each is
`PUSH AX; MOV A,#cfg; CALL disp_read_cfg; POP AX; RET`):

| cfg | loader | w × h | data | purpose |
|-----|--------|-------|------|---------|
| 0 | `sub_d0c2` | 29×15 | bank2 0x0000 | boot logo |
| 1 | `sub_d0ca` | 10×16 | bank2 0x2000 | icons (menu borders) |
| 2 | `sub_d0d2` | 16×16 | bank2 0x3000 (+0x5000 offset) | **JAPANESE font** |
| 3 | `sub_d0da` | 8×8 | bank2 0x5800 | **ENGLISH text font** |
| 4 | `sub_d0e2` | 16×16 | bank2 0x6000 | large sprite (arrow) |
| 5 | `sub_d0ea` | 5×10 | bank2 0x8000 | **small font** |
| 6 | `sub_d0f2` | 6×8 | bank2 0x8800 | **small font** |
| 7 | `sub_d0fa` | 32×32 | bank2 0x9000 | **pattern-design curve icons** |
| 8 | `sub_d102` | 16×7 | bank2 0xA000 | 16×7 sprite |
| — | `disp_read_def` (0x17BA) | 6×6 | bank0 0x0200 | **symbol font** (not in table) |

Named entry points: `disp_write_c1`(cfg1) 0x1357, `disp_write_c3`(cfg3) 0x136F,
`disp_write_c7`(cfg7) 0x139F, `disp_write_c8`(cfg8) 0x13AB. Configs 0/2/4/5/6
are selected by calling the `sub_d0xx` loader directly then `disp_write`.

### Sprite index encoding (`disp_sprite_base` 0x1762)

- **Base glyph `i`**: AX = `0x9000 + i` → address `base + i*stride` (full height).
- **Offset glyph `i`**: AX = `0x8000 + i` → address `offset + i*stride`, drawn at
  **HALF height** (`height/2`). Used for the Japanese offset font.

### Width dispatch (`disp_write` 0x13D3)

`bytes_per_row = ceil(width/8)` selects the column writer:
- 1 → `disp_update_col_8` (0x1437)
- 2 → `disp_update_col_16` (0x14C9)
- 4 → `disp_update_col_32` (0x15A8)

The loop iterates **height** times; each pass reads one column's worth of data
(`disp_sprite_read_col`) and advances `cursor_col`.

### Blend flags (`disp_flags`, byte `mem_febc`)

| bit | meaning |
|-----|---------|
| 0 | skip buffer write (LCD only) |
| 1 | skip AND-clear (draw as pure OR) |
| 2 | XOR blend (invert-on-overwrite) |
| 3 | alternate blend (OR then AND) |
| 4 | invert sprite (XOR 0xFF) |
| 5 | blank (draw nothing, just advance) — used for space `0x20` |
| 6 | skip LCD write (buffer only) |

### Character → glyph map (`string_machine_table` 0xE4CB)

| chars | glyph |
|-------|-------|
| `'0'`–`'9'` (0x30–0x39) | `c − 0x30` (0–9) |
| `'A'`–`'Z'` (0x41+) | `c − 0x31` (16–41) |
| `','` | 10 |
| `'('` | 11 |
| `')'` | 12 |
| `'-'` | 13 |
| `'/'` | 14 |
| `'.'` | 15 |
| `'&'` | 42 |
| `' '` (0x20) | blank (flag5) |
| anything else | 43 (fallback) |

## Render pipeline (functions)

| function | addr | purpose |
|----------|------|---------|
| `disp_write` | 0x13D3 | draws a sprite (width 8/16/32 → col_8/16/32) |
| `disp_write_c1/c3/c7/c8` | 0x1357/136F/13AB/139F | load cfg then disp_write |
| `disp_sprite_read_col` | 0x17F5 | reads 2 words of sprite data (2 rows) |
| `disp_update_col_8` | 0x1437 | transpose 8-wide → column byte |
| `disp_update_col_16` | 0x14C9 | transpose 16-wide |
| `disp_update_col_32` | 0x15A8 | transpose 32-wide |
| `disp_sprite_base` | 0x1762 | sprite address calc |
| `disp_read_cfg` | 0x179E | load config table entry |
| `disp_read_def` | 0x17BA | set 6×6 font |
| `disp_pixel_addr_mask` | 0x1CF2 | buffer addr = `0x0103 + col*4 + page` |
| `disp_buf_write` | 0x1AAC | write byte to SRAM buffer |
| `disp_lcd01_refresh` | 0x182F | copy buffer → LCD (4 pages) |
| `lcd_write` | 0x1A8C | direct LCD write (sets page+column) |
| `lcd_setcolumn` | 0x1B13 | column command, chip split at 61 |
| `lcd_writedata` | 0x1B26 | data byte |
| `lcd01_setRAMpage` | 0x1B37 | page command (both chips) |
| `lcd0/1_ctrl` | 0x1E37/0x1E49 | command strobe (P3.6/P3.7) |
| `lcd0/1_data` | 0x1E6F/0x1E89 | data strobe (+ XOR if `mem_febf.7`) |

## Display buffer (SRAM bank 8)

`buf[0x0103 + col*4 + page]`, 122 columns × 4 pages. Written by
`disp_buf_write`, copied to LCD by `disp_lcd01_refresh` (stride 4).

## Bugs fixed (emu/cpu78k2.py) — do NOT regress

1. `CMP` must not write back to the register (flags only).
2. `MOVW rp,rp'` dest = `(o>>5)&3` (not `(o>>4)&3`).
3. A/X bit-op register select `r = (o>>3)&1` (X=0, A=1) — was inverted.
4. `SHLW`/`SHRW` are **16-bit register-pair** ops; `ROR`/`ROL` (non-carry)
   wrap bit0↔bit7 (not via CY).
5. `DIVUW r` remainder goes **into the divisor register r** (not always C).
6. **Register bank (RBN)**: `SEL RBn` must update PSW bits 3–2, and `RETI` /
   `POP PSW` must restore `rbn` from those bits. Before this fix the tick ISR
   (`intc10_irq` 0x1000, which does `SEL RB1` but no restore) left `rbn` stuck
   at 1, so the main code and the ISR shared bank 1 and the ISR clobbered the
   main code's registers — this was the diag-menu 0xFF contamination and the
   `'B'`/`'E'` mis-render.

## Still open

- **cfg0 (29×15) col32 rendering for non-page-aligned rows**: the language
  option kanji (cfg0 glyphs 98/99/100) draw at cursor_row=17 (page offset 1)
  but the column bytes come out shifted/garbled (e.g. glyph 98 col0 = `0x55`
  should land as `0xAA` after the 1-bit shift, but writes `0x54`). The
  `disp_update_col_32` multi-byte rotate (ROLC C/B/E/D/L/H) for row offsets
  > 0 needs tracing. Page-aligned 8×8 text is correct.
- **cfg 2 (16×16) regular Japanese font**: where/if the 16×16 Japanese font is
  used in the UI is still TBD (the language option uses cfg 0, not cfg 2).

Full glyph bitmaps for every font are in `fonts_atlas.txt` (regenerate with
`python dump_fonts.py`).
