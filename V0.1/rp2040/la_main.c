/*
 * DIN logic analyzer — RP2040 firmware.
 *
 * Captures the machine's MISO (GPIO 7) at the PIO clock (125 MHz) into RAM,
 * triggered on the first DIN-low edge, then dumps the buffer over USB CDC.
 *
 * Protocol (all from the PC over CDC):
 *   'G'  -> re-arm: wait for DIN low, capture 128 KiB, then auto-dump.
 *   'D'  -> dump the last captured buffer again.
 *   'P'  -> reply with the 4 pin levels (bit0=SCK bit1=CS bit2=DIN bit3=DOUT).
 *
 * Dump layout: 4-byte header (little-endian sample Hz) + N x 32-bit words.
 * Each 32-bit word holds 32 consecutive DIN samples, bit 0 = oldest,
 * bit 31 = newest.  Sample period = 1 / sample Hz.
 */
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "hardware/pio.h"
#include "hardware/dma.h"
#include "hardware/clocks.h"
#include "tusb.h"

#include "la.pio.h"

#define PIN_DIN   7
#define PIN_SCK   4
#define PIN_CS    5
#define PIN_DOUT  8

#define SAMPLE_HZ      125000000u   // PIO clock (system clock)
#define CAPTURE_WORDS  32768u       // 128 KiB buffer = 8.39 ms @ 125 MHz

static uint32_t cap[CAPTURE_WORDS];
static PIO pio;
static uint sm;
static int dma_chan;

static void arm(void) {
    pio_sm_set_enabled(pio, sm, false);
    pio_sm_restart(pio, sm);
    pio_sm_clear_fifos(pio, sm);

    dma_channel_config c = dma_channel_get_default_config(dma_chan);
    channel_config_set_transfer_data_size(&c, DMA_SIZE_32);
    channel_config_set_read_increment(&c, false);
    channel_config_set_write_increment(&c, true);
    channel_config_set_dreq(&c, pio_get_dreq(pio, sm, false));
    dma_channel_configure(dma_chan, &c, cap, &pio->rxf[sm], CAPTURE_WORDS, true);

    pio_sm_set_enabled(pio, sm, true);
}

static void dump(void) {
    // header: sample rate (LE)
    uint32_t hz = SAMPLE_HZ;
    tud_cdc_write(&hz, 4);
    const uint8_t *p = (const uint8_t *)cap;
    for (uint32_t off = 0; off < sizeof(cap); off += 64) {
        while (tud_cdc_write_available() < 64) tud_task();
        tud_cdc_write(p + off, 64);
        tud_task();
    }
    tud_cdc_write_flush();
}

int main(void) {
    gpio_init(25);
    gpio_set_dir(25, GPIO_OUT);
    for (int i = 0; i < 3; i++) {
        gpio_put(25, 1); sleep_ms(60);
        gpio_put(25, 0); sleep_ms(60);
    }

    stdio_init_all();
    tusb_init();

    pio = pio0;
    sm = 0;
    uint offset = pio_add_program(pio, &din_la_program);
    pio_sm_config c = din_la_program_get_default_config(offset);
    sm_config_set_in_pins(&c, PIN_DIN);
    sm_config_set_jmp_pin(&c, PIN_DIN);
    sm_config_set_in_shift(&c, true, true, 32);   // shift right, autopush 32
    sm_config_set_clkdiv(&c, 1.0f);               // 125 MHz
    pio_sm_init(pio, sm, offset, &c);

    dma_chan = dma_claim_unused_channel(true);
    arm();

    while (1) {
        tud_task();
        if (!tud_cdc_available()) continue;
        uint8_t cmd = 0;
        if (tud_cdc_read(&cmd, 1) != 1) continue;

        if (cmd == 'G') {
            // Re-arm and wait for the trigger + full capture (with timeout).
            arm();
            uint32_t t0 = time_us_32();
            while (dma_channel_is_busy(dma_chan)) {
                tud_task();
                if (time_us_32() - t0 > 15000000) break;  // 15 s timeout
            }
            pio_sm_set_enabled(pio, sm, false);
            dump();
        } else if (cmd == 'D') {
            dump();
        } else if (cmd == 'P') {
            uint8_t pins = (uint8_t)((gpio_get(PIN_SCK)  ? 0x01 : 0) |
                                     (gpio_get(PIN_CS)   ? 0x02 : 0) |
                                     (gpio_get(PIN_DIN)  ? 0x04 : 0) |
                                     (gpio_get(PIN_DOUT) ? 0x08 : 0));
            tud_cdc_write(&pins, 1);
            tud_cdc_write_flush();
        }
    }
    return 0;
}
