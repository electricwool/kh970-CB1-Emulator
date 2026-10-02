# RP2040 CSI bridge — pinout

GPIO assignments for `csi_bridge.c` (the `#define` names in parentheses).

| Pico GPIO | signal | CSI role | direction (Pico) | connects to |
|-----------|--------|----------|------------------|-------------|
| **GPIO 4** | `PIN_SCK`  | **SCK** — serial clock | input | CB-1 `P3.2` (machine drives the clock) |
| **GPIO 5** | `PIN_CS`   | **CS** — chip-select / attention | input | CB-1 `P2.1` (active **low**, machine drives it) |
| **GPIO 6** | `PIN_VCCA` | (not CSI) shifter low-side 3.3 V supply (VCCA) | output **high** | shifter `VCCA` |
| **GPIO 7** | `PIN_DIN`  | **MISO** — machine → CB-1 data | input | CB-1 `P2.7` |
| **GPIO 8** | `PIN_DOUT` | **MOSI** — CB-1 → machine data | output | CB-1 `P3.3` |

## Wire summary

```
Pico                        KH-970 machine link
----                        -------------------
GPIO 4  (SCK)   <---------- SCK   (clock, machine-driven)
GPIO 5  (CS)    <---------- CS    (chip-select, active-low)
GPIO 6  (VCCA)  ----------> level-converter VCCA (3.3 V rail, held HIGH)
GPIO 7  (din)   <---------- MISO  (machine -> CB-1 data)
GPIO 8  (dout)  ----------> MOSI  (CB-1 -> machine data)
GND     -------------------- GND
```

- **SCK** = the machine's clock. Idle **low** (CPOL=0), ~8.8 kHz. One CS
  pulse per byte, exactly 8 clocks each.
- **CS** = chip-select / attention. **Active low** — its falling edge is the
  CB-1's `INTP0` trigger.
- **din** (`PIN_DIN`, machine → CB-1) = **ACTIVE-LOW** (a low level is a data
  bit 1) and must be sampled on the **falling** SCK edge; the machine changes
  it just after each rising edge. MSB-first.
- **dout** (`PIN_DOUT`, CB-1 → machine) = active-**high**, MSB-first. The
  machine samples it on the **rising** SCK edge, so bit 0 (MSB) is presented
  at CS fall and bits 1..7 shift out during the low phase.

## Your logic-analyzer harness (8ch + CLK + GND)

The harness used for the Saleae captures, and the Pico GPIO each wire goes to
(analyzer channel numbering is 1-based; the Saleae CSV was 0-based, so ch1 =
CSV `Channel 0`):

| analyzer pin | wire color | signal | Pico GPIO |
|--------------|-----------|--------|-----------|
| ch1 | red | **DIN** — machine → CB-1 data (active-low) | **GPIO 7** |
| ch2 | orange | **SCK** — clock | **GPIO 4** |
| ch3 | black | unused (static low) | — |
| ch4 | blue | **CS** — chip-select (active-low) | **GPIO 5** |
| ch5 | green | **+5 V VCC** (machine power rail) | level-converter high-side |
| ch6 | yellow | **DOUT** — CB-1 → machine data | **GPIO 8** |
| ch7 | brown | unused (static low) | — |
| ch8 | — | unused (static high) | — |
| CLK | — | leave unconnected | — |
| GND (pin 10) | bare | ground | **Pico GND** |

Only SCK, CS, DIN and DOUT carry signals; black/brown/ch8 and the CLK pin
stay disconnected.  `GPIO 6` is not part of the harness — it powers the
level-converter VCCA rail (3.3 V, held high).

### Power

The machine provides **+5 V on green** and **GND on bare**; use them for the
level converter's high-side rail.  The converter's low-side rail (VCCA, 3.3 V)
is powered from `GPIO 6` held high (the board draws well under 2 mA); the
Pico's `3V3` pin is the alternative "proper" rail.  Ground must be common to
the machine, the converter and the Pico.

## PIO constraint — SCK and CS must be consecutive

The PIO program uses JMP-pin-relative waits: `wait pin 0` = **SCK** and
`wait pin 1` = **CS**. That means `PIN_CS` **must equal `PIN_SCK + 1`**
(they are GPIO 4 and 5 here). If you move them, keep them adjacent with SCK
on the lower-numbered pin, or the program's `wait` instructions will target
the wrong pin. `PIN_DIN` / `PIN_DOUT` can be moved freely.

## To change the pins

Edit the `#define`s at the top of `csi_bridge.c`:

```c
#define PIN_SCK   4
#define PIN_CS    5    // must be PIN_SCK + 1
#define PIN_VCCA  6    // level-converter VCCA (3.3 V rail), GPIO held high
#define PIN_DIN   7
#define PIN_DOUT  8
```

then rebuild:

```powershell
cd c:\esp32\cb1\rp2040
cmake --build build
```

## Voltage

The CB-1 is a 5 V part (78K/II); the RP2040 pins are **3.3 V only**. Verify
the machine-link logic level before connecting directly — if it is 5 V, add a
level shifter (or series protection on the inputs; the `dout` output must be
boosted if the CB-1 needs 5 V thresholds).
