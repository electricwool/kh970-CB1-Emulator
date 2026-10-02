# CB-1 CSI bridge — RP2040 firmware (pico-sdk build)
#
# Build:
#   set PICO_SDK_PATH=C:\pico-sdk        (or pass -DPICO_SDK_PATH=...)
#   cmake -G Ninja -B build .
#   cmake --build build
# Output: build/csi_bridge.uf2

cmake_minimum_required(VERSION 3.13)

include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(csi_bridge C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(PICO_BOARD pico)

pico_sdk_init()

add_executable(csi_bridge
    csi_bridge.c
    usb_descriptors.c
)

# Generate csi_bridge.pio.h from csi_bridge.pio
pico_generate_pio_header(csi_bridge ${CMAKE_CURRENT_LIST_DIR}/csi_bridge.pio)

target_include_directories(csi_bridge PRIVATE ${CMAKE_CURRENT_LIST_DIR})

target_link_libraries(csi_bridge PRIVATE
    pico_stdlib
    hardware_pio
    pico_multicore
    tinyusb_device
)

# Emit .uf2 / .bin / .hex
pico_add_extra_outputs(csi_bridge)

# ---- DIN logic analyzer (standalone capture tool) ------------------------
add_executable(din_la
    la_main.c
    usb_descriptors.c
)

pico_generate_pio_header(din_la ${CMAKE_CURRENT_LIST_DIR}/la.pio)

target_include_directories(din_la PRIVATE ${CMAKE_CURRENT_LIST_DIR})

target_link_libraries(din_la PRIVATE
    pico_stdlib
    hardware_pio
    hardware_dma
    tinyusb_device
)

pico_add_extra_outputs(din_la)
