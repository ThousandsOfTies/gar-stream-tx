"""Minimal ILI9341 SPI driver for Linux (spidev + periphery GPIO).

Copied from gar-stream-rx (board-agnostic - only used here for gar-stream-tx's
optional local preview branch). No framebuffer/fbtft kernel driver needed -
this talks to the panel directly over /dev/spidevX.Y and toggles DC/RESET
through sysfs GPIO (periphery.GPIO).

Only a write-only subset is implemented (enough to blit RGB565 frames),
since that's all a live-preview appsink callback needs.
"""
import sys
import time

import spidev
from periphery import GPIO

# ILI9341 command set (subset)
_SWRESET = 0x01
_SLPOUT = 0x11
_DISPOFF = 0x28
_DISPON = 0x29
_CASET = 0x2A
_PASET = 0x2B
_RAMWR = 0x2C
_MADCTL = 0x36
_PIXFMT = 0x3A

# (command, [data bytes], post-delay-ms) - widely used ILI9341 init sequence.
_INIT_SEQUENCE = [
    (_SWRESET, [], 150),
    (_DISPOFF, [], 10),
    (0xCF, [0x00, 0x83, 0x30], 0),
    (0xED, [0x64, 0x03, 0x12, 0x81], 0),
    (0xE8, [0x85, 0x01, 0x79], 0),
    (0xCB, [0x39, 0x2C, 0x00, 0x34, 0x02], 0),
    (0xF7, [0x20], 0),
    (0xEA, [0x00, 0x00], 0),
    (0xC0, [0x26], 0),  # Power control 1
    (0xC1, [0x11], 0),  # Power control 2
    (0xC5, [0x35, 0x3E], 0),  # VCOM control 1
    (0xC7, [0xBE], 0),  # VCOM control 2
    (_PIXFMT, [0x55], 0),  # 16 bits/pixel
    (0xB1, [0x00, 0x1B], 0),  # Frame rate control
    (0xF2, [0x08], 0),  # Gamma function disable
    (0x26, [0x01], 0),  # Gamma curve selected
    (0xE0, [0x1F, 0x1A, 0x18, 0x0A, 0x0F, 0x06, 0x45, 0x87,
            0x32, 0x0A, 0x07, 0x02, 0x07, 0x05, 0x00], 0),  # Positive gamma
    (0xE1, [0x00, 0x25, 0x27, 0x05, 0x10, 0x09, 0x3A, 0x78,
            0x4D, 0x05, 0x18, 0x0D, 0x38, 0x3A, 0x1F], 0),  # Negative gamma
    (_SLPOUT, [], 150),
    (_DISPON, [], 100),
]

# rotation -> (MADCTL bits without BGR, width, height)
_ROTATIONS = {
    0: (0x40, 240, 320),
    1: (0x20, 320, 240),
    2: (0x80, 240, 320),
    3: (0xE0, 320, 240),
}

_SPI_CHUNK = 4096  # keep well under typical spidev bufsiz limits


class ILI9341:
    def __init__(self, spi_bus, spi_device, dc_gpio, rst_gpio,
                 spi_max_hz=10_000_000, rotation=1, bgr=True):
        if rotation not in _ROTATIONS:
            raise ValueError("rotation must be 0, 1, 2 or 3")

        madctl, self.width, self.height = _ROTATIONS[rotation]
        if bgr:
            madctl |= 0x08
        self._madctl = madctl

        self.dc = GPIO(dc_gpio, "out")
        self.rst = GPIO(rst_gpio, "out")

        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_device)
        self.spi.mode = 0
        self.spi.max_speed_hz = spi_max_hz

        self._hard_reset()
        self._run_init_sequence()
        self._write_cmd(_MADCTL, [self._madctl])

    def close(self):
        self.spi.close()
        self.dc.close()
        self.rst.close()

    # -- low level -------------------------------------------------------
    def _hard_reset(self):
        self.rst.write(True)
        time.sleep(0.01)
        self.rst.write(False)
        time.sleep(0.02)
        self.rst.write(True)
        time.sleep(0.15)

    def _write_cmd(self, cmd, data=None):
        self.dc.write(False)
        self.spi.writebytes([cmd])
        if data:
            self.dc.write(True)
            self._write_raw(bytes(data))

    def _write_raw(self, buf):
        # spidev has a limit on a single transfer size; chunk long buffers.
        mv = memoryview(buf)
        for offset in range(0, len(mv), _SPI_CHUNK):
            self.spi.writebytes(list(mv[offset:offset + _SPI_CHUNK]))

    def _run_init_sequence(self):
        for cmd, data, delay_ms in _INIT_SEQUENCE:
            self._write_cmd(cmd, data)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)

    def _set_window(self, x0, y0, x1, y1):
        self._write_cmd(_CASET, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self._write_cmd(_PASET, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self._write_cmd(_RAMWR)

    # -- drawing helpers ---------------------------------------------------
    @staticmethod
    def rgb565(r, g, b):
        value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        return bytes([value >> 8, value & 0xFF])

    def fill_rect(self, x, y, w, h, color565):
        x = max(0, min(x, self.width - 1))
        y = max(0, min(y, self.height - 1))
        w = max(0, min(w, self.width - x))
        h = max(0, min(h, self.height - y))
        if w == 0 or h == 0:
            return
        self._set_window(x, y, x + w - 1, y + h - 1)
        self.dc.write(True)
        # bytes multiplication is a fast C-level repeat, fine for a full screen.
        self._write_raw(color565 * (w * h))

    def fill_screen(self, color565):
        self.fill_rect(0, 0, self.width, self.height, color565)

    def blit(self, x, y, w, h, pixel_buf):
        """pixel_buf must already be w*h RGB565 bytes (big-endian pairs)."""
        self._set_window(x, y, x + w - 1, y + h - 1)
        self.dc.write(True)
        self._write_raw(pixel_buf)

    def blit_native_rgb565(self, x, y, w, h, pixel_buf):
        """Draw host-native RGB565, converting it to the panel's MSB-first order."""
        if sys.byteorder == "little":
            pixels = bytearray(pixel_buf)
            high_bytes = pixels[1::2]
            pixels[1::2] = pixels[0::2]
            pixels[0::2] = high_bytes
            pixel_buf = pixels
        self.blit(x, y, w, h, pixel_buf)
