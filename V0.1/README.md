# CB-1 release package

| Folder | Contents |
|--------|----------|
| `emulator/` | CB-1 emulator source (Python, stdlib only) + web UI + machine ROM |
| `rp2040/`   | RP2040 CSI-bridge firmware source (pico-sdk / TinyUSB) |
| `bin/`      | Built artifacts: `CB1-Emulator.exe`, `csi_bridge.uf2`|

## Run the emulator

Requires Python 3.10+ with no third-party packages.

## Flash the RP2040

Drag `bin\csi_bridge.uf2` onto the Pico's BOOTSEL mass-storage drive
(see `rp2040/README.md` for wiring and build instructions).
