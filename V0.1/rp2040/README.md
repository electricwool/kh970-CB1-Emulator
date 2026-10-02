# RP2040 CSI bridge

Pico firmware that sits between the KH-970 machine (CSI **slave**) and the
CB-1 emulator (over USB CDC → WebSerial). The Pico is the machine-facing CSI
**master**: it drives SCK, captures the bytes the machine shifts out on DIN,
and presents the reply bytes the emulator supplied on DOUT.

```
KH-970 (slave) <--PIO CSI master--> RP2040 <--USB CDC--> PC (WebSerial --> emulator)
```

Compiles with the pico-sdk and TinyUSB into a UF2 (see Build, below). Key files:

* `csi_bridge.pio` — PIO program: CSI master (drives SCK, captures CS/DIN,
                     drives DOUT).
* `csi_bridge.c`   — TinyUSB CDC + PIO wiring + byte framing + the transaction
                     state-machine stub (the part that must be filled in).
* `usb_descriptors.c` / `tusb_config.h` — single CDC (USB serial) device.
* `CMakeLists.txt` — pico-sdk build.

## Build

Prerequisites: `arm-none-eabi-gcc`, `cmake`, `ninja`, and the Pico SDK
(`C:\pico-sdk` here; `--recurse-submodules` pulls in TinyUSB).

```powershell
cd c:\esp32\cb1\rp2040
cmake -G Ninja -B build -DPICO_SDK_PATH=C:\pico-sdk
cmake --build build
```

Output — copy **`build\csi_bridge.uf2`** to a board in BOOTSEL mode:

```
build\csi_bridge.uf2   (drag onto the Pico mass-storage drive)
build\csi_bridge.bin   (raw flash image)
build\csi_bridge.hex
build\csi_bridge.elf
```

## Wiring

| Pico GPIO | signal | role |
|-----------|--------|------|
| 4 | SCK  | clock (output, CB-1 drives — the master) |
| 5 | CS   | chip-select / attention (input, active low) |
| 7 | din  | machine → CB-1 data (input) |
| 8 | dout | CB-1 → machine data (output) |

Change `PIN_SCK`/`PIN_CS`/`PIN_DIN`/`PIN_DOUT` in `csi_bridge.c` to move pins.

## Physical layer (VERIFIED from the Saleae captures)

Confirmed twice: once from the archived `digital.csv` and again from a fresh
10 s @ 1 MHz capture of the real machine (`c:\esp32\cb1\_capture\digital.csv`,
1691 bytes).

* **CPOL=0** — SCK idle low. **One CS pulse per byte, exactly 8 clocks per
  byte** (1691/1691 frames in the fresh capture). Clock ~8.8 kHz (median
  period ~113.5 µs).
* **MSB-first** bit order (not LSB-first).
* **CS is active-low**, falling edge triggers the CB-1's `INTP0`.

### Saleae channel mapping (corrected + confirmed live)

| channel | signal | level |
|---------|--------|-------|
| Ch1 | **SCK** (clock, CB-1-driven) | idle low |
| Ch3 | **CS** (frame, active low) | idle high |
| Ch5 | **CB-1 → machine data** (reply) | active **high**, sample on rising |
| Ch0 | **machine → CB-1 data** (master byte) | **ACTIVE-LOW**, sample on falling |
| Ch2,4,6,7 | unused (static) | — |

Ch0 is the machine's data line: it was misread as "unrelated" in the old
capture because it is **inverted** (active-low) and only valid on the falling
edge.  With those two corrections it decodes byte-exact to the same commands
the reply echoes, e.g. `80 E2 E2 D5` / `40 E2 E2 E0` / `33 E2 E2 E0`.

The reply stream decodes byte-for-byte to the `machine_link.md` protocol
(`E2` idle, command echo, `E0` ack, `0A` = the `&!0009` counter), confirming
the framing and MSB-first order.

`csi_bridge.c` bit-reverses at the PIO FIFO boundary (wire is MSB-first, PIO
shifts LSB-first) and inverts `din` (active-low).  The PIO samples `din` on the
falling SCK edge and presents `dout` for the machine's rising-edge sample.

## The transaction state machine

The PIO only moves bits; the correctness of the link lives in the **transaction
state machine** — a byte-exact port of the CB-1's `p0_irq_handler` /
`csi_irq_handler` / `sub_dd71` logic (see `machine_link.md`), now implemented
in `csi_bridge.c` and verified against a live 30 s capture of the real machine.

`csi_step()` serves every wire byte autonomously — the emulator cannot answer a
live SCK edge over WebSerial (ms latency vs µs wire timing). The emulator
pushes the data the machine reads back over `T_STATE` records (payload[0] =
selector): needle rows (`0xA0`/`0xA1`, 25 bytes = 200 needles), row codes
(`0x90`–`0x92`, 1 byte), counters (`0x50`–`0x53`, 1 byte), expected-ack
(`0x80`, 1 byte → `fe7e`, e.g. `D0`/`D3`–`D6`), and knit flag (`0x81`, 1 byte
→ `D8`/`D9`). The RP2040 reports `T_EVENT` records (start, row advance/back,
counter read, abort) back. The per-transaction flag state
(`fe56`/`fe57`/`fe66`/`fe67`/`fe69`/`fdbb..`) stays in the emulator.
