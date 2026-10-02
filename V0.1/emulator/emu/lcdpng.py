"""Dependency-free PNG writer for the LCD framebuffer (0/1 pixels)."""
import struct
import zlib


def save_png(path, pixels, w, h, scale=4):
    """Write a w x h framebuffer (0/1 per pixel) as a white-on-black PNG,
    scaled up by `scale` with nearest-neighbour so text is readable."""
    raw = b''
    for y in range(h):
        line = bytearray()
        for x in range(w):
            v = 255 if pixels[y * w + x] else 0
            line += bytes([v]) * scale
        scanline = b'\x00' + bytes(line)      # filter type 0 (None)
        raw += scanline * scale               # vertical scale

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w * scale, h * scale, 8, 0, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 9))
    png += chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)
