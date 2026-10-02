# SCK ack driver — hardware add-on for the CB-1 CSI bridge

The RP2040 bridge needs to drive the machine's **SCK** line **high** for a
short period during the power-on link-up handshake. This document describes
the two-transistor circuit that does it, and how it connects to the firmware
(`PIN_ACK` = GPIO 9 in `csi_bridge.c`).

## Why it's needed

- The KH-970 machine holds **SCK idle LOW** and actively drives it low while
  it waits for the CB-1's attention.
- The BSS138 level-converter board is **open-drain**: it can only pull a line
  **low**; its "high" is a 10 kΩ pull-up that cannot overcome the machine's
  active low driver. (The machine's low wins.)
- The real CB-1 actively drives SCK **high** (push-pull) to acknowledge. This
  add-on reproduces that with two discrete transistors.

## Parts

| Part | Value / type | Role |
|------|--------------|------|
| Q1 | **2N3906** PNP (TO-92, EBC) | high-side switch — drives SCK to +5 V |
| Q2 | **P2N2222** NPN (TO-92, EBC) | low-side driver — pulls Q1's base low |
| R1 | **100 Ω** | Q1 collector → SCK; limits contention current |
| R2 | **1 kΩ** | Q1 base → Q2 collector; limits Q1 base current |
| R3 | **4.7 kΩ** | +5 V → Q1 base; pull-up (holds Q1 off) |
| R4 | **4.7 kΩ** | GPIO 9 → Q2 base; limits Q2 base current |
| D1 | **3.6 V zener** (optional) | on GPIO 4; clamps the SCK read input |

TO-92 pinout (flat side toward you, leads down): **E**mitter left,
**B**ase middle, **C**ollector right — for *both* transistors.

## Circuit

```
                    +5 V ────────────────── VCCB (level board)
                     │
                     ├──────────────[4.7 kΩ]─────────┐
                     │                              │
                 ┌───┴───┐                          │
                 │ 2N3906 │  PNP                     │
                 └───┬───┘                          │
                     │                              │
                  C (collector)                 B (base)
                     │                              │
                   [100 Ω]                        [1 kΩ]
                     │                              │
                     ├──── SCK wire ──→ machine      │
                     │                              C
                     │                          ┌───┴────┐
                     │                          │ P2N2222 │ NPN
                     │                          └───┬────┘
                     │                              │
                     │                          B ──[4.7 kΩ]── GPIO 9  (PIN_ACK)
                     │                          E ── GND
                     │
                     └──── B1 (level board ch1 — reads SCK)

    GPIO 4 (SCK read) ──[3.6 V zener, cathode → GPIO 4, anode → GND]
```

## Wiring table

| # | From | To |
|---|------|----|
| 1 | Q2 (P2N2222) **emitter** (left) | GND |
| 2 | Q2 **base** (middle) → 4.7 kΩ | GPIO 9 |
| 3 | Q2 **collector** (right) → 1 kΩ | Q1 base |
| 4 | Q1 (2N3906) **base** (middle) → 4.7 kΩ | +5 V |
| 5 | Q1 **emitter** (left) | +5 V |
| 6 | Q1 **collector** (right) → 100 Ω | SCK wire |
| 7 | level board **B1** | SCK wire (already wired) |
| 8 | zener **cathode** (banded) | GPIO 4 line (level board A1) |
| 9 | zener **anode** | GND |

## Current paths

- **Path 1 — high-side drive:** `+5 V → Q1 emitter → Q1 collector → 100 Ω → SCK`
  Conducts only when Q1 is ON.
- **Path 2 — base pull-up:** `+5 V → 4.7 kΩ → Q1 base`
  Always connected; holds Q1 off unless the base is pulled down.
- **Path 3 — control:** `Q1 base → 1 kΩ → Q2 collector → Q2 emitter → GND`
  Conducts only when Q2 is ON; pulls Q1's base low.
- **Path 4 — NPN base drive:** `GPIO 9 → 4.7 kΩ → Q2 base → Q2 emitter → GND`
  Conducts when GPIO 9 is HIGH.
- **Path 5 — SCK read:** `SCK wire → level board B1 → BSS138 → A1 → GPIO 4`
  Unchanged; how the Pico reads the machine's clock.

## Operating states

| GPIO 9 (PIN_ACK) | Q2 | Q1 | SCK |
|------------------|----|----|-----|
| LOW (idle) | OFF | OFF | machine drives the clock normally |
| HIGH (ack) | ON | ON | driven HIGH (+5 V) |

- **Idle:** Q2 off → Q1 base held at +5 V via R3 → Q1 off → SCK free.
- **Ack:** Q2 on → Q1 base pulled low via R2 → Q1 on → +5 V drives SCK high
  through R1 (≤ ~50 mA contention, limited by R1).

## Firmware interface

- `#define PIN_ACK 9` in `csi_bridge.c`.
- GPIO 9 is an output, idle **low** (driver off).
- In `csi_handshake()`: set `PIN_ACK` high to acknowledge, back low to release.
  GPIO 4 (`PIN_SCK`) stays an input the whole time — it no longer toggles.
