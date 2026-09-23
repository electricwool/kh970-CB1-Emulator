# CB-1 Menu Tree (English)

Reverse-engineered from the Brother KH-970 CB-1 (`kh970CB1v1.0-AM27C040@DIP32.bin`)
via the 78K/II emulator in `emu/`.

## Navigation keys

Verified against ROM `keyb_scan_1x4` (0xD202) and the menu loop `lab_c746` (0xC746).

| Key | P7 col | key code | menu flag | action |
|-----|--------|----------|-----------|--------|
| OK | 0 | 0x0F | mem_fec0.3 | enter / confirm |
| M/B | 1 | 0x0E | mem_fec0.2 | **back** (returns to previous menu) |
| ADVICE | 2 | 0x0C | mem_fec0.0 | show context help (press again to dismiss) |
| DC | 3 | 0x0D | mem_fec0.1 | (varies by screen) |
| arrows | P2.2–2.5 | — | — | navigate |

Arrow pad: UP = P2.2 (0x01), DOWN = P2.3 (0x02), **RIGHT = P2.4 (0x04)**, **LEFT = P2.5 (0x08)**.
(Note: this LEFT/RIGHT mapping was swapped in an early emulator build and fixed.)

Menu selection index `B`: RIGHT → `B++`, LEFT → `B--`. `B` is bounded by the menu
size (LEFT ignored at the first item, RIGHT ignored at the last).

---

## Boot

```
Power on
  └─ Language selection (horizontal scroll, 7 options, B = 1..7)
       ├─ 日本語 (Japanese)
       ├─ English          ← press RIGHT once (B=2), then OK
       ├─ Deutsch
       ├─ Nederlands
       ├─ Español
       ├─ Français
       └─ 中文 (Chinese)
  └─ OK → MAIN MENU
```

---

## MAIN MENU

10 items in a horizontal scroll (←/→). Default selection = item 3 (KNITTING PROGRAM).
**OK** enters an item, **ADVICE** describes it, **M/B** returns to the previous screen.

| # | item | ADVICE text ("YOU CAN …") |
|---|------|---------------------------|
| 1 | ROW COUNTER | SET ROW COUNTER TO NUMBER WHICH YOU WISH. |
| 2 | PATTERN ROW | SET PATTERN ROW NO. WHERE YOU WISH TO START. |
| 3 | KNITTING PROGRAM ★ | SELECT KNITTING PROGRAM. |
| 4 | POSITIONING PROGRAM | SELECT POSITIONING PROGRAM. |
| 5 | VARY PATTERN(S) | VARY PATTERN(S). |
| 6 | DESIGN PATTERN/OR GARMENT | DESIGN PATTERN OR GARMENT. |
| 7 | MEMO INFORMATION | PROGRAM MEMO INFORMATION. |
| 8 | ENTER DATA | ENTER DATA ON SPECIFIC ROW IN COMPUTER. |
| 9 | TRANSFER DATA | TRANSFER DATA FROM/TO CARTRIDGE/DISK. |
| 10 | SELECT LANGUAGE | SELECT LANGUAGE. |

---

## Sub-menu tree

```
MAIN MENU
├─ 1. ROW COUNTER
│     └─ "ROW-COUNTER SET-UP" → numeric prompt "?0_"
│          (enter row number where the row counter should start;
│           ADVICE: "ENTER ROW NO. WHERE YOU WISH TO START AT ROW COUNTER.")
│
├─ 2. PATTERN ROW
│     └─ ⚠ "PATTERN HAS NOT BEEN POSITIONED IN COMPUTER."
│          (requires a pattern already positioned via item 4)
│
├─ 3. KNITTING PROGRAM  (★ default)
│     └─ ⚠ "CB-1 AND KM BODY ARE NOT CONNECTED."
│          (requires the knitting machine body connected to the CB-1)
│
├─ 4. POSITIONING PROGRAM
│     └─ "POSITIONING PROGRAM" — toolbar of 4 icon options (move/cancel/etc.)
│          (ADVICE context: "CANCEL POSITION OF PATTERN(S)")
│
├─ 5. VARY PATTERN(S)
│     └─ "PATTERN VARIATION" — pattern boxes (scale/duplicate the pattern)
│
├─ 6. DESIGN PATTERN / GARMENT
│     └─ "DESIGNING PROGRAM" — toolbar (shape/garment outline editor)
│
├─ 7. MEMO INFORMATION
│     └─ ⚠ "PATTERN HAS NOT BEEN STORED IN COMPUTER."
│          (requires a pattern stored first)
│
├─ 8. ENTER DATA
│     └─ returns to MAIN MENU immediately (leaf; needs stored pattern + machine)
│
├─ 9. TRANSFER DATA
│     └─ "DATA TRANSFER" — 4 options (document ↔ cartridge / envelope / disk)
│          (ADVICE context: "SAVE DATA TO CARTRIDGE")
│
└─ 10. SELECT LANGUAGE
      └─ "LANGUAGE SELECT" — same 7-language list as boot
```

---

## Notes / pending

- Items 3/6 (knitting + design) and 4/5 (positioning + vary) are the core
  pattern-editing applications. Their deep sub-menus require a stored pattern
  and the machine attached, so they only reach an error screen on a clean SRAM.
- Error screens use a fixed format: `!<code> <message>` with an icon box on the
  left. Dismiss with **M/B** (DC does not dismiss them).
- Sub-menu ADVICE screens show the selected option's **icon** on the left and a
  mode-level text on the right (the text does not change per option — the icon
  does). Worth double-checking whether this is correct firmware behaviour or an
  emulator limitation.
- The `MAIN MENU` title is drawn at row 0, column 33 in the 8×8 English font
  (cfg 3, ROM 0x025800); advice body text at row 16, column 1.
