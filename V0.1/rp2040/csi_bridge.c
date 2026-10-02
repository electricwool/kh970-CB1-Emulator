/*
 * CB-1 CSI bridge — RP2040 (bit-banged CSI master <-> USB CDC <-> PC emulator).
 *
 * Data path:
 *   KH-970 machine (CSI slave) <-- SIO bit-bang master --> USB CDC --> PC
 *
 * The CB-1 is the CSI MASTER: it generates SCK.  The machine (slave) asserts
 * CS to request a byte and shifts its data out in response to our clock.  The
 * power-on "ack" is the first rising edge of the first byte.
 *
 * The RP2040 serves the wire protocol AUTONOMOUSLY: the emulator cannot
 * answer a live SCK edge over WebSerial (ms latency vs us wire timing).
 * csi_step() is a byte-exact port of the CB-1's
 * p0_irq_handler / csi_irq_handler / sub_dd71 logic (machine_link.md),
 * verified against a live 30 s capture of the real machine.
 *
 * The emulator remains the source of truth: it pushes needle rows, row
 * codes and counters over T_STATE records; the RP2040 reports T_EVENT
 * records (start, row advance/back, counter read, abort) back.
 */
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/time.h"
#include "pico/multicore.h"
#include "hardware/sync.h"
#include "hardware/pio.h"
#include "hardware/gpio.h"
#include "tusb.h"

#include "csi_bridge.pio.h"

#define PIN_SCK   4
#define PIN_CS    5
#define PIN_DIN   7
#define PIN_DOUT  8
#define PIN_VCCA  6    // level-converter VCCA (3.3 V low-side rail), GPIO held high
#define MANUAL_HANDSHAKE 0   // 1 = PC script drives pins; 0 = automatic handshake

// ---- transport framing (mirror of emu/csi.py) ---------------------------
#define SYNC        0xAA
#define T_CSI_BYTE  0x01   // RP2040 -> emulator (captured master byte)
#define T_EVENT     0x03
#define T_STATE     0x04   // emulator -> RP2040 (needle row / row code / ...)
#define T_PIN       0x05   // RP2040 -> emulator (pin change: [pin, level])

static uint8_t crc8(const uint8_t *p, size_t n) {
    uint8_t c = 0;
    while (n--) {
        c ^= *p++;
        for (int i = 0; i < 8; i++)
            c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
    }
    return c;
}

// The CSI wire is MSB-first (verified from the Saleae capture), but the PIO
// `out pins,1` / `in pins,1` shift LSB-first, so bytes are bit-reversed at the
// FIFO boundary.
static uint8_t bitrev8(uint8_t b) {
    b = (uint8_t)((b & 0xF0) >> 4 | (b & 0x0F) << 4);
    b = (uint8_t)((b & 0xCC) >> 2 | (b & 0x33) << 2);
    b = (uint8_t)((b & 0xAA) >> 1 | (b & 0x55) << 1);
    return b;
}

// stream deframer state
static uint8_t rxbuf[300];
static size_t   rxlen = 0;

static uint8_t frame_payload[260];   // copy-out buffer: payload survives the rxbuf memmove

static bool try_deframe(uint8_t *type, uint8_t **payload, size_t *len) {
    // scan for SYNC, then validate length + crc; on success consume the frame
    size_t i = 0;
    for (; i < rxlen && rxbuf[i] != SYNC; i++) {}
    if (i) { memmove(rxbuf, rxbuf + i, rxlen - i); rxlen -= i; }
    if (rxlen < 3) return false;
    size_t need = 4 + rxbuf[2];
    if (rxlen < need) return false;
    if (crc8(rxbuf + 1, 2 + rxbuf[2]) != rxbuf[need - 1]) {
        memmove(rxbuf, rxbuf + 1, rxlen - 1); rxlen--;  // resync
        return false;
    }
    *type = rxbuf[1];
    *len = rxbuf[2];
    // Copy the payload OUT before consuming the frame: *payload = rxbuf + 3
    // pointed into rxbuf, but the memmove below shifts rxbuf and left the
    // caller reading the NEXT frame's bytes.  That mis-shifted every needle
    // row (A0->A1, A1 dropped, A2->next index), leaving pattern[0]/[2] as the
    // 0xAA boot pre-fill — the "0xAA, real, 0xAA" knit.
    memcpy(frame_payload, rxbuf + 3, rxbuf[2]);
    *payload = frame_payload;
    memmove(rxbuf, rxbuf + need, rxlen - need); rxlen -= need;
    return true;
}

static void build_frame(uint8_t type, const uint8_t *payload, size_t len, uint8_t *f) {
    // build: SYNC | type | len | payload[len] | crc8(type,len,payload)
    f[0] = SYNC; f[1] = type; f[2] = (uint8_t)len;
    memcpy(f + 3, payload, len);
    f[3 + len] = crc8(f + 1, 2 + len);
}

// ---- cross-core USB TX ring buffer ---------------------------------------
// Core 0 (the timing-critical CSI bit-bang) produces framed bytes; core 1
// (USB/serial) consumes them and pushes them to the CDC port.  Single-producer
// single-consumer, lock-free; frames are dropped under backpressure.
#define USB_TX_SIZE 4096
static uint8_t usb_tx[USB_TX_SIZE];
static volatile uint32_t usb_tx_head = 0;   // written by producer (core 0)
static volatile uint32_t usb_tx_tail = 0;   // written by consumer (core 1)

static void usb_tx_push(const uint8_t *p, size_t n) {
    uint32_t head = usb_tx_head;
    uint32_t tail = usb_tx_tail;
    if (head - tail + n >= USB_TX_SIZE) return;   // full -> drop (best effort)
    for (size_t i = 0; i < n; i++) usb_tx[head++ & (USB_TX_SIZE - 1)] = p[i];
    __dmb();
    usb_tx_head = head;
}

static size_t usb_tx_pop(uint8_t *dst, size_t maxn) {
    uint32_t tail = usb_tx_tail;
    __dmb();
    uint32_t head = usb_tx_head;
    size_t n = head - tail;
    if (n > maxn) n = maxn;
    for (size_t i = 0; i < n; i++) dst[i] = usb_tx[tail++ & (USB_TX_SIZE - 1)];
    usb_tx_tail = tail;
    return n;
}

// Core 0 path: stage the frame into the ring buffer (non-blocking).
static void send_frame(uint8_t type, const uint8_t *payload, size_t len) {
    uint8_t f[260];
    build_frame(type, payload, len, f);
    usb_tx_push(f, 4 + len);
}

// Core 1 path: write the frame straight to the USB CDC (core 1 owns TinyUSB).
static void send_frame_usb(uint8_t type, const uint8_t *payload, size_t len) {
    uint8_t f[260];
    build_frame(type, payload, len, f);
    tud_cdc_write(f, 4 + len);
    tud_cdc_write_flush();
}

static void send_event_usb(uint8_t ev, uint8_t arg) {
    uint8_t p[2] = {ev, arg};
    send_frame_usb(T_EVENT, p, 2);
}

// ---- CSI transaction state machine -------------------------------------
// Byte-exact port of the CB-1's p0_irq_handler / csi_irq_handler / sub_dd71
// (machine_link.md), verified against a live 30 s capture.  csi_step(m) is
// called with the master byte just captured and returns the reply byte to
// stage for the NEXT exchange (the PIO preloads the reply before CS falls,
// matching the CB-1 staging its reply on the INTP0 attention edge).
//
//   byte1  M=command      R=0xE2 (idle marker)
//   byte2  M=0xE2 (sync)  R=command (echo)
//   byte3  M=0xE2 (pull)  R=0xE0 (control) | fe7e (0x80/0x81) | data[0]
//   ...    M=echo         R=data[k+1] ... | 0xE1 (done)
//
// Command classes (0xDBFF-0xDC9B): high nibble 0x50 -> counter, 0x90 -> row
// code, 0xA0 -> needle row (data reads); 0x80/0x81 -> ack; everything else ->
// control (0xE0 ack).

#define N_NEEDLE   25            // 200 needles / 8 bits (verified live)
#define N_ROWS     22            // pattern rows (fddb = 0x16, set at 0x3B2D)
#define N_COUNTERS 4

// T_EVENT payload[0] codes (see CSI_BRIDGE.md)
#define EV_START    0x01         // 0xB0
#define EV_ROW_ADV  0x02         // 0xB1
#define EV_ROW_BACK 0x03         // 0xB2
#define EV_COUNTER  0x04         // 0x50-0x53 read
#define EV_ABORT    0x05         // bad byte -> resync

// Boot-handshake progress (diagnostic; T_EVENT payload[0])
#define HEV_CS    0x10   // machine asserted CS (attention seen)
#define HEV_ACK   0x11   // ack asserted (GPIO 4 driven high -> SCK high)
#define HEV_DIN   0x12   // machine raised DIN
#define HEV_UP    0x13   // link-up complete
#define HEV_WAIT  0x14   // DIN timeout -> retry
#define HEV_PINS  0x15   // periodic pin-state report: bit0=SCK bit1=CS bit2=DIN bit3=DOUT
#define HEV_BYTES 0x16   // periodic report: valid transactions completed (arg = low byte)
#define HEV_RAW   0x17   // periodic report: raw PIO bytes captured (arg = low byte)
#define HEV_PIO   0x18   // periodic report: PIO PC (arg bit7=SM enabled, bits4-0=PC)
#define HEV_FIFO  0x19   // periodic report: PIO FIFOs (arg bits6-4=TX level, bits1-0=RX level)
#define HEV_OFF   0x1A   // periodic report: PIO program offset (arg)
#define HEV_CLK   0x1B   // periodic report: PIO clkdiv INT (arg; 250 expected)
#define HEV_WRAP  0x1C   // periodic report: PIO wrap target (arg; 14 expected)
#define HEV_FS    0x1D   // periodic report: GPIO FUNCSEL (arg = SCK<<4 | DOUT; 0x66 expected)
#define HEV_TST   0x1E   // periodic report: PIO drive test (1=SCK drivable via SET PINS)
#define HEV_P80   0x1F   // periodic report: 0x80 keep-alive poll count (low byte)
#define HEV_P81   0x20   // periodic report: 0x81 knit-flag poll count (low byte)
#define HEV_FE12  0x21   // report at 0xB0: fe12 row counter (gates the A1 read)

// State the emulator pushes (T_STATE, written on core 1 by cdc_task) and the
// machine reads back (read on core 0 during the bit-bang).  volatile: shared
// across cores.
static volatile uint8_t row_codes[3] = {0, 0, 0};          // 0x90/0x91/0x92 window
// Full contiguous pattern buffer, mirroring the CB-1's 0x02EB + fe7c*25 layout.
// A0 = row[row_idx] (current, sub_0eb9); A1 = row[row_idx+1] (next, sub_0ed9).
// Pre-filled 0xAA in main() so the machine never reads uninitialized rows.
static volatile uint8_t pattern[N_ROWS][N_NEEDLE];
static volatile uint8_t row_idx = 0;                       // fe7c row pointer
static volatile uint8_t counters[N_COUNTERS] = {10, 12, 0, 0};  // 0x50-0x53
static volatile uint8_t fe7e = 0xD0;                       // expected ack (boot = 0xD0)
static volatile uint8_t knit_flag = 0;                     // fe67.0 -> 0x81 reply
static volatile uint8_t fe7e_ready = 0;   // 1 once the emulator pushed fe7e=0xD2 (sub_3e50)
static volatile uint8_t dout_idle = 1;    // idle DOUT level: 1=armed (HIGH), 0=re-armed (LOW)

enum { ST_IDLE, ST_SYNC, ST_CONTROL, ST_ACK, ST_DATA, ST_DONE };
static int  st = ST_IDLE;
static uint8_t cmd;                    // mem_fe7d
static const volatile uint8_t *data_ptr;  // HL
static uint8_t data_idx;               // mem_fe81
static uint8_t data_count;             // mem_fe82
static volatile uint32_t txn_count = 0;  // valid transactions (core 0 writes, core 1 reports)
static volatile uint32_t raw_bytes = 0;  // every byte clocked in (garbage included)
static volatile uint32_t poll80 = 0;     // 0x80 keep-alive polls completed
static volatile uint32_t poll81 = 0;     // 0x81 knit-flag polls completed
static volatile uint8_t fe12 = 0;        // row counter (0xB1 INC, 0xB2 DEC) — gates the A1 read
static volatile uint8_t rearm_pending = 0; // 1 = re-arm (D0) staged; 2 = DOUT dropped, waiting to ready
static volatile uint32_t rearm_at = 0;     // time_us_32() when DOUT was dropped
static volatile uint8_t rearm_diag = 0;    // 1 = diagnostic re-arm (do NOT auto-raise to D2)
static volatile uint8_t klg_mode = 0;      // 1 = KLG TEST active (0xB0 must NOT set fe7e=D6)

static void send_event(uint8_t ev, uint8_t arg) {
    uint8_t p[2] = {ev, arg};
    send_frame(T_EVENT, p, 2);
}

// Report a live pin level to the emulator (T_PIN).  The emulator's boot
// handshake parks at the CS poll until it sees CS go low, so this record is
// what releases it (pin 0 = CS, pin 1 = DIN, level = raw wire level).
static void send_pin(uint8_t pin, uint8_t level) {
    uint8_t p[2] = {pin, level};
    send_frame(T_PIN, p, 2);
}

// Command bytes worth forwarding to the emulator.  The keep-alive 0x80/0x81
// polls and the in-band sync/marker bytes (0xE0-0xE3) are answered entirely on
// the RP2040 and never relayed — the emulator only needs commands that carry
// meaning (needle rows, row codes, counters, dispatch, sensors, config).
static bool is_valuable(uint8_t b) {
    if (b <= 0x4F) return true;                 // config nibbles (0x00-0x4F)
    if (b >= 0x50 && b <= 0x53) return true;    // counter reads
    if (b >= 0x60 && b <= 0x6D) return true;    // sensor bitmap
    if (b >= 0x90 && b <= 0x92) return true;    // row codes
    if (b == 0xA0 || b == 0xA1) return true;    // needle rows
    if (b >= 0xB0 && b <= 0xBE) return true;    // dispatch commands (0xB9-0xBE too)
    if (b >= 0xC0 && b <= 0xCD) return true;    // tri-state groups
    return false;                               // 0x80/0x81 polls, 0xE0-0xE3 markers
}

// p0_irq_handler: the reply byte staged for the next exchange.
static uint8_t stage_reply(void) {
    switch (st) {
    case ST_IDLE:    return 0xE2;
    case ST_SYNC:    return cmd;                          // echo the command
    case ST_CONTROL: return 0xE0;
    case ST_ACK:     return (cmd == 0x81) ? (uint8_t)(knit_flag ? 0xD8 : 0xD9) : fe7e;
    case ST_DATA:    return data_ptr[data_idx];
    case ST_DONE:    return 0xE1;
    }
    return 0xE1;
}

// 0xDBFF-0xDC9B: classify the command byte (fe7d) after the sync exchange.
static void classify_cmd(void) {
    uint8_t c = cmd;
    if (c == 0x80 || c == 0x81) {
        st = ST_ACK;
    } else if (c >= 0x50 && c <= 0x53) {
        data_ptr = &counters[c - 0x50]; data_count = 1; data_idx = 0; st = ST_DATA;
    } else if (c >= 0x90 && c <= 0x92) {
        data_ptr = &row_codes[c - 0x90]; data_count = 1; data_idx = 0; st = ST_DATA;
    } else if (c == 0xA0) {
        data_ptr = pattern[row_idx % N_ROWS]; data_count = N_NEEDLE; data_idx = 0; st = ST_DATA;
    } else if (c == 0xA1) {
        data_ptr = pattern[(row_idx + 1) % N_ROWS]; data_count = N_NEEDLE; data_idx = 0; st = ST_DATA;
    } else {
        st = ST_CONTROL;
    }
}

// sub_dd71: dispatch on transaction completion.  Only the wire-visible side
// effects are replicated (fe7e, knit flag, events); the full flag state
// (fe56/fe57/fe66/fe67/fe69/fdbb..) lives in the emulator.
static void dispatch_cmd(void) {
    txn_count++;                         // a full framed transaction completed
    // Forward only the command byte of a valid completed transaction.  Garbage
    // (e.g. power-down noise) never completes a framed transaction, so it is
    // filtered here at the source and never reaches the emulator.
    if (is_valuable(cmd)) send_frame(T_CSI_BYTE, &cmd, 1);
    switch (cmd) {
    case 0xB0:
        if (klg_mode) {
            // KLG TEST: the ROM's 0xB0 dispatch (lab_dd8e) hits `BT fe55.3 ->
            // RET`, so it does NOT set fe7e=D6, does NOT advance the row, and
            // does NOT re-arm -- the machine is just cycling its tri-state
            // groups.  Setting D6 here would clobber the D2 KLG ack and break
            // the handshake.  Still forward EV_START so the emulator applies
            // the flag side effects (fea9.3 / fe67.0/1 / fe56.1 / fe57.0).
            send_event(EV_START, cmd);
        } else {
            fe7e = 0xD6; knit_flag = 0; send_event(EV_START, cmd);
            row_idx = (uint8_t)((row_idx + 1) % N_ROWS);       // sub_a708 row advance
            send_event(HEV_FE12, fe12);                        // report the gate value
            // Re-arm unconditionally on a knit start.  The ROM gates this on
            // fe12 in [1,FE] (lab_dda6), but that fe12 is maintained by the
            // ROM's flag-gated B1/B2 dispatch (fea9.2 / fe66.7 / fe69.1/2),
            // which the bridge's simplified fe12 does NOT track.  With an
            // alternating carriage direction (B2,B1,B0 then B1,B2,B0) the
            // simplified counter landed on 0 at 0xB0, the re-arm was skipped,
            // and the machine gave up and re-booted after a few rows.
            // Re-arming on every knit 0xB0 matches the ROM's net behaviour.
            fe7e = 0xD0;
            rearm_diag = 0;              // this is the knit re-arm, not a diag exit
            rearm_pending = 1;
        }
        break;
    case 0x91: fe7e = 0xD1; break;
    case 0xB1:
        knit_flag = 1; send_event(EV_ROW_ADV, cmd);
        if (fe12 != 1) fe12++;                             // INC fe12 (0 -> 1, stays 1)
        break;
    case 0xB2:
        send_event(EV_ROW_BACK, cmd);
        // DEC fe12, but CLAMP at 0.  The ROM's lab_de58 only decrements when
        // its flags allow it (fe66.7/fea9.2/fe69.2), so in the normal knit
        // flow fe12 is 1 when 0xB2 arrives (B1 preceded it).  When the
        // carriage leads with 0xB2 (back-and-forth knitting) fe12 is 0, and
        // the old `fe12 != 0xFF` guard underflowed 0 -> 0xFF; the following
        // 0xB1 then overflowed 0xFF -> 0x00, so the 0xB0 re-arm gate
        // (`fe12 >= 1`) never fired and the machine stopped after the first
        // pair of rows.  Clamping mirrors the ROM's net effect (0 stays 0).
        if (fe12 > 0) fe12--;
        break;
    case 0x50: case 0x51: case 0x52: case 0x53:
        send_event(EV_COUNTER, cmd); break;
    case 0x80:
        poll80++;
        if (rearm_pending == 1) {
            // The machine's poll released the re-arm (fe56.5 cleared):
            // sub_3e7e drops DOUT; the main loop raises it after a delay.
            dout_idle = 0;
            rearm_at = time_us_32();
            rearm_pending = 2;
        }
        // Boot keep-alive: the ROM drops DOUT low after the machine's first
        // 0x80 poll (sub_3e7e "re-arm") and only raises it again when it is
        // ready to knit (sub_3e57, alongside fe7e=0xD2).  The machine waits
        // for that DOUT rising edge before polling 0x80 again — without it the
        // machine polls once after boot and then idles forever (never knitting).
        if (!fe7e_ready) dout_idle = 0;
        break;
    case 0x81:
        poll81++;
        break;
    default:
        // Only the version-MAJOR nibble (0x00-0x0F, fdbb) marks a fresh machine
        // (re)boot: re-arm the expected-ack to 0xD0 so the boot handshake
        // completes.  The REST of the config relay (0x10-0x4F: version minor +
        // width nibbles) must NOT re-arm — the machine re-sends width nibbles
        // (0x20-0x40) during the diagnostic POS/KLG tests, and re-arming on
        // those clobbers the test-mode ack (0xD3/D4/D5) back to 0xD0, which is
        // what killed the POS handshake.  This lives HERE (immediate, on the
        // wire) rather than in the emulator's T_STATE push, whose latency made
        // it arrive after the machine's 0x91 read and clobber the 0xD1 the
        // dispatch just set.
        if (cmd <= 0x0F) { fe7e = 0xD0; fe7e_ready = 0; dout_idle = 1; }
        break;
    }
}

// csi_irq_handler: advance the state machine on master byte `m`, then return
// the reply byte to stage for the NEXT exchange.
static uint8_t csi_step(uint8_t m) {
    switch (st) {
    case ST_IDLE:
        cmd = m;
        st = ST_SYNC;
        break;
    case ST_SYNC:
        if (m != 0xE2) { st = ST_IDLE; send_event(EV_ABORT, cmd); break; }
        classify_cmd();
        break;
    case ST_CONTROL:
    case ST_ACK:
        st = ST_DONE;                       // ack was pulled by the machine
        break;
    case ST_DATA:
        data_idx++;
        if (data_idx >= data_count) st = ST_DONE;
        break;
    case ST_DONE:
        dispatch_cmd();
        st = ST_IDLE;
        break;
    }
    return stage_reply();
}

// ---- boot link-up (ROM reset @ 0x2D23 .. lab_2e22) ----------------------
// We are the CSI MASTER: we clock every byte, so there is no separate ack
// phase to handshake — the machine asserts CS (attention) and our PIO's first
// byte IS the link-up:
//   1. machine: CS  = LOW       (attention; debounced here)
//   2. PIO:     SCK rises       (SET1 P3.2 @ 0x2DFB — the ack, = bit 0 clock)
//   3. machine: DIN = HIGH      (~1.4 us later; the machine answering)
//   4. PIO:     DOUT = HIGH     (reply bit 0 = 0xE2 MSB), 8 clocks complete
//   5. machine: CS  = HIGH      byte 0 done (machine sent 0x01 version relay)
// The PIO then serves every later CS pulse as a normal byte; re-attention
// needs no special handling.

static void csi_handshake(void) {
    // DOUT idles LOW until the first byte (CLR1 P3.3 @ 0x2DCD); its rise to
    // reply bit 0 (0xE2 MSB = 1) is what the machine latches as link-up.
    gpio_init(PIN_DOUT);
    gpio_set_dir(PIN_DOUT, GPIO_OUT);
    gpio_put(PIN_DOUT, 0);

    // SCK is OUR clock (we are the CSI master): hold it low (CPOL=0) while we
    // wait.  The main loop bit-bangs the clock after link-up.
    gpio_init(PIN_SCK);
    gpio_set_dir(PIN_SCK, GPIO_OUT);
    gpio_put(PIN_SCK, 0);

    // CS (machine attention) and DIN (machine data) are inputs.  DIN idles high
    // (active-low data); the pull-up stops it floating and picking up the
    // Pico's 12 MHz crystal crosstalk.
    gpio_init(PIN_CS);
    gpio_set_dir(PIN_CS, GPIO_IN);
    gpio_pull_up(PIN_CS);
    gpio_init(PIN_DIN);
    gpio_set_dir(PIN_DIN, GPIO_IN);
    gpio_pull_up(PIN_DIN);

    uint32_t last_pin_report = 0;

    // Wait for the machine's power-on attention (CS low) so we can release the
    // emulator's boot park, then hand the wire to the main loop, whose first
    // byte clocks the ack (its first SCK rising edge).  The link-up and the
    // byte protocol are the same thing; later re-attentions are just the next
    // byte and need no special handling.
    for (;;) {
        // (Core 1 keeps the USB device stack alive while we wait.)
        while (gpio_get(PIN_CS)) {
            tight_loop_contents();
            uint32_t now = time_us_32();
            if (now - last_pin_report > 250000) {
                last_pin_report = now;
                uint8_t pins = (uint8_t)((gpio_get(PIN_SCK) ? 0x01 : 0) |
                                         (gpio_get(PIN_CS)  ? 0x02 : 0) |
                                         (gpio_get(PIN_DIN) ? 0x04 : 0) |
                                         (gpio_get(PIN_DOUT) ? 0x08 : 0));
                send_event(HEV_PINS, pins);
            }
        }
        // Debounce CS (the machine's CS pulse is ~1 ms — a 2 ms sleep would
        // miss it and treat every pulse as a glitch).
        sleep_us(20);
        if (gpio_get(PIN_CS)) continue;
        sleep_us(20);
        if (gpio_get(PIN_CS)) continue;

        send_event(HEV_CS, 0);                   // machine asserted CS
        send_pin(0, 0);                          // -> emulator: CS low, release boot park
        {
            uint8_t pins = (uint8_t)((gpio_get(PIN_SCK) ? 0x01 : 0) |
                                     (gpio_get(PIN_CS)  ? 0x02 : 0) |
                                     (gpio_get(PIN_DIN) ? 0x04 : 0) |
                                     (gpio_get(PIN_DOUT) ? 0x08 : 0));
            send_event(HEV_PINS, pins);
        }
        send_event(HEV_UP, 0);                   // link-up: the main loop clocks the ack byte
        break;
    }
}

// Manual handshake: pump the CDC and let a PC script drive the pins step by
// step over USB.  Commands are single bytes:
//   'P' -> reply with one raw byte (bit0=SCK bit1=CS bit2=DIN bit3=DOUT)
//   'H' -> drive SCK (GPIO 4) high
//   'L' -> release SCK back to input
//   'O' -> drive DOUT (GPIO 8) high
//   'I' -> drive DOUT (GPIO 8) low
static void manual_handshake(void) {
    gpio_init(PIN_DOUT);
    gpio_set_dir(PIN_DOUT, GPIO_OUT);
    gpio_put(PIN_DOUT, 0);

    gpio_init(PIN_CS);
    gpio_set_dir(PIN_CS, GPIO_IN);
    gpio_pull_up(PIN_CS);
    gpio_init(PIN_DIN);
    gpio_set_dir(PIN_DIN, GPIO_IN);
    gpio_init(PIN_SCK);
    gpio_set_dir(PIN_SCK, GPIO_IN);
    gpio_disable_pulls(PIN_SCK);

    for (;;) {
        tud_task();
        if (tud_cdc_available()) {
            uint8_t cmd = 0;
            if (tud_cdc_read(&cmd, 1) == 1) {
                uint8_t pins;
                switch (cmd) {
                case 'P':
                    pins = (uint8_t)((gpio_get(PIN_SCK) ? 0x01 : 0) |
                                     (gpio_get(PIN_CS)  ? 0x02 : 0) |
                                     (gpio_get(PIN_DIN) ? 0x04 : 0) |
                                     (gpio_get(PIN_DOUT) ? 0x08 : 0));
                    tud_cdc_write(&pins, 1);
                    tud_cdc_write_flush();
                    break;
                case 'H':
                    gpio_set_dir(PIN_SCK, GPIO_OUT);
                    gpio_put(PIN_SCK, 1);
                    break;
                case 'L':
                    gpio_set_dir(PIN_SCK, GPIO_IN);
                    gpio_disable_pulls(PIN_SCK);
                    break;
                case 'O':
                    gpio_put(PIN_DOUT, 1);
                    break;
                case 'I':
                    gpio_put(PIN_DOUT, 0);
                    break;
                default:
                    break;
                }
            }
        }
    }
}

// ---- CSI master bit-bang (SIO) ------------------------------------------
// Bit-bang one full-duplex CSI byte as the master.  `reply` is staged (MSB
// first, active-high); the machine samples it on each SCK rising edge.  The
// master byte is sampled from DIN on each SCK falling edge (MSB first,
// ACTIVE-LOW — the caller inverts with ^0xFF).  SCK period ~113 us (~56 us
// half periods).  DOUT idles HIGH between bytes (the CB-1 "armed" level — the
// machine waits for it before requesting the next byte).
static uint8_t bitbang_byte(uint8_t reply) {
    uint8_t m = 0;
    for (int k = 0; k < 8; k++) {
        gpio_put(PIN_DOUT, (reply >> (7 - k)) & 1);   // present reply bit k
        gpio_put(PIN_SCK, 1);                         // rising: machine samples DOUT
        sleep_us(56);
        gpio_put(PIN_SCK, 0);                         // falling: DIN bit k valid
        m = (uint8_t)((m << 1) | (gpio_get(PIN_DIN) ? 1 : 0));
        sleep_us(56);
    }
    gpio_put(PIN_DOUT, 1);                            // idle DOUT high (armed)
    return m;
}

// ---- core 1: USB / serial (not timing critical) --------------------------
void cdc_task(void);   // defined below

static void core1_main(void) {
    uint32_t last_bytes_report = 0;
    uint32_t last_pins_report = 0;
    while (1) {
        tud_task();                     // TinyUSB device task
        cdc_task();                     // read T_STATE, update shared data

        // Drain the TX ring buffer produced by core 0's bit-bang.
        uint8_t chunk[128];
        size_t n = usb_tx_pop(chunk, sizeof(chunk));
        if (n) {
            tud_cdc_write(chunk, n);
            tud_cdc_write_flush();
        }

        // Periodic link-health reports (reading core 0's counters).
        uint32_t now = time_us_32();
        if (now - last_bytes_report > 250000) {
            last_bytes_report = now;
            send_event_usb(HEV_BYTES, (uint8_t)(txn_count & 0xFF));
        }
        if (now - last_pins_report > 500000) {
            last_pins_report = now;
            uint8_t pins = (uint8_t)((gpio_get(PIN_SCK)  ? 0x01 : 0) |
                                     (gpio_get(PIN_CS)   ? 0x02 : 0) |
                                     (gpio_get(PIN_DIN)  ? 0x04 : 0) |
                                     (gpio_get(PIN_DOUT) ? 0x08 : 0));
            send_event_usb(HEV_PINS, pins);
            send_event_usb(HEV_RAW, (uint8_t)(raw_bytes & 0xFF));
            send_event_usb(HEV_P80, (uint8_t)(poll80 & 0xFF));
            send_event_usb(HEV_P81, (uint8_t)(poll81 & 0xFF));
            // Pattern-cache debug: first byte of rows 0..2 + row_idx.  Lets the
            // PC see what the machine will read for A0/A1 (0xAA = stale).
            send_event_usb(0x22, pattern[0][0]);
            send_event_usb(0x23, pattern[1][0]);
            send_event_usb(0x24, pattern[2][0]);
            send_event_usb(0x25, (uint8_t)row_idx);
        }
    }
}

int main(void) {
    // Boot indicator: blink the on-board LED (GPIO 25) 3 times so we can tell
    // the firmware reached main() (a hang/crash earlier would leave it dark).
    gpio_init(25);
    gpio_set_dir(25, GPIO_OUT);
    for (int i = 0; i < 3; i++) {
        gpio_put(25, 1); sleep_ms(80);
        gpio_put(25, 0); sleep_ms(80);
    }

    stdio_init_all();

    // Initialize the TinyUSB device stack.  stdio_init_all() does NOT do this
    // when PICO_STDIO_USB is off, so it must be called explicitly (otherwise
    // the USB peripheral is never enabled and the CDC port never appears).
    tusb_init();

    // Power the level-converter's low-side rail (VCCA, 3.3 V) from this GPIO
    // held high before touching the bus.
    gpio_init(PIN_VCCA);
    gpio_set_dir(PIN_VCCA, GPIO_OUT);
    gpio_put(PIN_VCCA, 1);

    // Pre-fill the whole pattern buffer with 0xAA before core 1 starts, so the
    // machine never reads uninitialized rows (even before the PC pushes data).
    for (int r = 0; r < N_ROWS; r++)
        for (int i = 0; i < N_NEEDLE; i++)
            pattern[r][i] = 0xAA;

    // Launch core 1 for all USB/serial.  TinyUSB is NOT thread-safe, so every
    // tud_* call lives on core 1; core 0 below runs ONLY the timing-critical
    // CSI bit-bang (no USB, no serial, no interrupt sources).
    multicore_launch_core1(core1_main);

    // Establish the machine link: wait for the machine's attention, then
    // bit-bang the link-up (the first byte's first SCK rising edge is the ack).
#if MANUAL_HANDSHAKE
    manual_handshake();     // never returns; the PC script drives the pins
#else
    csi_handshake();
#endif

    // CSI master loop (core 0): bit-bang every byte with rock-solid SCK timing.
    uint8_t next_reply = 0xE2;         // staged reply for the first (link-up) byte
    uint32_t last_byte_at = 0;         // time_us_32() of the last completed byte
    uint8_t last_cs = 1;               // CS idles high
    uint32_t last_cs_evt = 0;          // throttle the CS-event flood (T_PIN/HEV_CS)

    while (1) {
        // Report CS edges to the emulator, but THROTTLED: during knitting the
        // machine polls 0x80 at ~4.6 ms, and two T_PIN + one HEV_CS per byte
        // (~15 KB/s) overflows the USB CDC ring buffer, which then drops the
        // T_CSI_BYTE command records.  The emulator's boot handshake only needs
        // the first CS fall after a long silence, so a 1 ms minimum interval
        // keeps that working while cutting the flood below USB capacity.
        uint8_t cs = gpio_get(PIN_CS) ? 1 : 0;
        if (cs != last_cs) {
            last_cs = cs;
            uint32_t now_us = time_us_32();
            if (now_us - last_cs_evt > 1000) {
                last_cs_evt = now_us;
                send_pin(0, cs);                  // T_PIN: CS level -> emulator
                if (!cs) send_event(HEV_CS, 0);   // fresh attention -> machine present
            }
        }
        // Bit-bang one byte per CS-low pulse (the machine holds CS low while it
        // waits for our clock).  The reply is staged BEFORE the byte; after it
        // we advance the state machine and stage the next reply.
        if (!gpio_get(PIN_CS)) {
            // A long silence (>100 ms) means the machine re-attentioned (the
            // link dropped / it powered back on).  Reset the transaction state
            // machine for a fresh link-up, matching the ROM re-running its
            // reset path.  Normal bytes are ~1 ms apart, so this never fires
            // mid-transaction.
            if (time_us_32() - last_byte_at > 100000) {
                st = ST_IDLE;
                next_reply = 0xE2;
            }
            sleep_us(110);          // CS fall -> first clock (ack latency)
            uint8_t raw = bitbang_byte(next_reply);
            while (!gpio_get(PIN_CS)) { tight_loop_contents(); }  // wait CS high
            uint8_t master = raw ^ 0xFF;   // DIN is active-low
            raw_bytes++;
            next_reply = csi_step(master);
            last_byte_at = time_us_32();
        } else {
            // Idle (CS high): hold DOUT at the armed state.  The machine gates
            // its next 0x80 keep-alive poll on this line's rising edge, so the
            // re-arm LOW (sub_3e7e) must be held until "ready" raises it HIGH
            // (sub_3e57).  Only apply it BETWEEN transactions (ST_IDLE): CS is
            // also high between the bytes of a single transaction, and DOUT must
            // stay HIGH (armed) there or the machine stops pulling bytes.
            if (st == ST_IDLE) {
                if (rearm_pending == 2 && time_us_32() - rearm_at > 2000) {
                    if (rearm_diag) {
                        // Diagnostic test exit: sub_3e7e dropped DOUT and the
                        // link stays re-armed (DOUT low) until the menu enters
                        // the next test mode (its fe7e push raises DOUT again).
                        // Do NOT auto-raise to D2 here — that is the knit path.
                        rearm_pending = 0;
                    } else {
                        fe7e = 0xD2;            // sub_3e50 ready
                        dout_idle = 1;          // sub_3e57 raise DOUT
                        rearm_pending = 0;
                    }
                }
                gpio_put(PIN_DOUT, dout_idle ? 1 : 0);
            }
        }
        tight_loop_contents();
    }
    return 0;
}

void cdc_task(void) {
    uint8_t buf[64];
    if (!tud_cdc_available()) return;
    uint32_t n = tud_cdc_read(buf, sizeof(buf));
    memmove(rxbuf + rxlen, buf, n);
    rxlen += n;

    uint8_t type, *payload; size_t len;
    while (try_deframe(&type, &payload, &len)) {
        if (type == T_STATE) {
            // Emulator pushes the data the machine will read back.
            // payload[0] selects what the following bytes update:
            //   0x50-0x53  counter value        (1 byte)
            //   0x80       expected-ack fe7e    (1 byte; D0 / D3-D6)
            //   0x81       knit flag fe67.0     (1 byte; -> D8/D9 reply)
            //   0x90-0x92  row-code window      (1 byte)
            //   0xA0/0xA1  needle row           (25 bytes -> row 0 / row 1)
            //   0xA2       needle row at index  (1 byte index + 25 bytes)
            if (len >= 1) {
                uint8_t sel = payload[0];
                if (sel >= 0x90 && sel <= 0x92 && len >= 2) {
                    row_codes[sel - 0x90] = payload[1];
                } else if (sel == 0xA0 && len >= 1 + N_NEEDLE) {
                    for (int i = 0; i < N_NEEDLE; i++) pattern[0][i] = payload[1 + i];
                } else if (sel == 0xA1 && len >= 1 + N_NEEDLE) {
                    for (int i = 0; i < N_NEEDLE; i++) pattern[1][i] = payload[1 + i];
                } else if (sel == 0xA2 && len >= 2 + N_NEEDLE) {
                    uint8_t r = payload[1] % N_ROWS;
                    for (int i = 0; i < N_NEEDLE; i++) pattern[r][i] = payload[2 + i];
                } else if (sel >= 0x50 && sel <= 0x53 && len >= 2) {
                    counters[sel - 0x50] = payload[1];
                } else if (sel == 0x80 && len >= 2) {
                    uint8_t v = payload[1];
                    fe7e = v;
                    switch (v) {
                    case 0xD2:   // "ready to knit" (sub_3e50/57)
                    case 0xD3:   // PH TEST  (0xE929)
                    case 0xD4:   // SOL TEST (0xE9A4)
                    case 0xD5:   // POS TEST (0xEB04)
                        // Arm DOUT high (sub_3e57) so the machine's next
                        // 0x80 poll reads the new ack.
                        fe7e_ready = 1;
                        dout_idle = 1;
                        rearm_pending = 0;
                        rearm_diag = 0;
                        break;
                    case 0xD0:
                        // Diagnostic test exit (sub_3e69 re-arm): stage the
                        // D0 + DOUT drop, but only drop DOUT after the
                        // machine's next 0x80 poll (sub_3e7e), exactly like
                        // the knit re-arm.  rearm_diag keeps the idle loop
                        // from auto-raising D2 after the drop.
                        fe7e_ready = 0;
                        rearm_pending = 1;
                        rearm_diag = 1;
                        break;
                    default:
                        // 0xD1 (row-code seen) / 0xD6 (start ack): just
                        // update the expected-ack; DOUT keeps its level.
                        break;
                    }
                } else if (sel == 0x81 && len >= 2) {
                    knit_flag = payload[1] & 1;
                } else if (sel == 0x82 && len >= 2) {
                    klg_mode = payload[1] & 1;
                }
            }
        }
    }
}
