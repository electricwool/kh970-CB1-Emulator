# RP2040 CSI bridge — pinout

GPIO assignments for `csi_bridge.c` (the `#define` names in parentheses).

| Pico GPIO | signal | CSI role | direction (Pico) | connects to |
|-----------|--------|----------|------------------|-------------|
| **GPIO 4** | `PIN_SCK`  | **SCK** — serial clock | input | CB-1 `P3.2` (machine drives the clock) |
| **GPIO 5** | `PIN_CS`   | **CS** — chip-select / attention | input | CB-1 `P2.1` (active **low**, machine drives it) |
| **GPIO 7** | `PIN_DIN`  | **MISO** — machine → CB-1 data | input | CB-1 `P2.7` |
| **GPIO 8** | `PIN_DOUT` | **MOSI** — CB-1 → machine data | output | CB-1 `P3.3` |

## Wire summary

```
Pico                        KH-970 machine link
----                        -------------------
GPIO 4  (SCK)   <---------- SCK   (clock, machine-driven)
GPIO 5  (CS)    <---------- CS    (chip-select, active-low)
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

### Power

The machine is powered by 3.3V for the logic from the pico's regulator and 5V from the USB for the solenoids 

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
