#!/usr/bin/env python3
"""KY-040 rotary encoder + push-button reader using periphery.GPIO edge events.

The decoder consumes both phases as a four-transition Gray-code cycle.  This
emits exactly one menu step per completed detent and rejects contact bounce.

Note: most KY-040 breakout boards don't have onboard pull-ups/downs on
CLK/DT/SW. If the encoder is jittery, add 10k pull-up resistors to 3.3V on
CLK/DT/SW (see README). GAR_GPIO_CHIP selects the Linux GPIO character device;
omitting it retains compatibility with the legacy sysfs backend.
"""
import os
import sys
import threading
import time

from periphery import GPIO

from quadrature import QuadratureDecoder


def _open_gpio(line, direction):
    chip = os.environ.get("GAR_GPIO_CHIP")
    gpio = GPIO(chip, line, direction) if chip else GPIO(line, direction)
    if direction == "in":
        try:
            # KY-040 contacts close to ground.  The Raspberry Pi 5 must
            # therefore bias CLK/DT/SW high while they are idle; otherwise
            # the unused inputs float and make false Gray-code transitions.
            gpio.bias = "pull_up"
        except (NotImplementedError, OSError) as error:
            # The legacy sysfs backend has no bias control.  Keep it usable
            # with external pull-ups, but never conceal that protection is
            # unavailable on this backend.
            print(f"[ky040] cannot enable GPIO{line} pull-up: {error}", file=sys.stderr)
    return gpio


def _read_edge(gpio):
    """Consume a pending GPIO event and return its level when available.

    python-periphery's character-device backend requires ``read_event()``
    after ``poll()``.  The legacy sysfs backend has no such method, and its
    normal ``read()`` both obtains the level and clears the pending event.
    """
    try:
        event = gpio.read_event()
    except NotImplementedError:
        return None
    return event.edge == "rising"


class KY040:
    def __init__(self, clk_gpio, dt_gpio, sw_gpio,
                 on_rotate=None, on_press=None, bounce_ms=2, press_debounce_ms=30):
        self.clk = _open_gpio(clk_gpio, "in")
        self.dt = _open_gpio(dt_gpio, "in")
        self.sw = _open_gpio(sw_gpio, "in")
        self.clk.edge = "both"
        self.dt.edge = "both"
        self.sw.edge = "falling"

        self.on_rotate = on_rotate
        self.on_press = on_press
        # Retain the public argument for product configuration compatibility.
        # Gray-code transition accumulation, rather than a timing threshold,
        # rejects rotary contact bounce without dropping a fast valid detent.
        self.bounce_s = bounce_ms / 1000.0
        self.press_debounce_s = press_debounce_ms / 1000.0

        self.counter = 0
        self._running = False
        self._rotate_thread = None
        self._button_thread = None

    def start(self):
        self._running = True
        self._rotate_thread = threading.Thread(target=self._rotate_loop, daemon=True)
        self._button_thread = threading.Thread(target=self._button_loop, daemon=True)
        self._rotate_thread.start()
        self._button_thread.start()

    def stop(self):
        self._running = False
        for t in (self._rotate_thread, self._button_thread):
            if t is not None:
                t.join(timeout=1.0)
        self.clk.close()
        self.dt.close()
        self.sw.close()

    def _rotate_loop(self):
        clock_state = self.clk.read()
        data_state = self.dt.read()
        decoder = QuadratureDecoder(clock_state, data_state)
        while self._running:
            clock_ready = self.clk.poll(0.1)
            data_ready = self.dt.poll(0)
            if clock_ready:
                _read_edge(self.clk)
            if data_ready:
                _read_edge(self.dt)
            if not clock_ready and not data_ready:
                continue
            # An edge may already have arrived on the other phase by the time
            # this thread runs. Decode one fresh two-line snapshot instead
            # of combining a new phase with a stale value from a prior event.
            clock_state = self.clk.read()
            data_state = self.dt.read()
            self._emit_detent(decoder, clock_state, data_state)

    def _emit_detent(self, decoder, clock_state, data_state):
        direction = decoder.update(clock_state, data_state)
        if direction is None:
            return
        # The prior TX falling-edge implementation reported the opposite sign
        # to the shared decoder. Preserve the established physical direction.
        direction = -direction
        self.counter += direction
        if self.on_rotate:
            self.on_rotate(direction, self.counter)

    def _button_loop(self):
        last_time = 0.0
        while self._running:
            if not self.sw.poll(0.5):
                continue
            if _read_edge(self.sw) is None:
                self.sw.read()
            now = time.monotonic()
            if now - last_time < self.press_debounce_s:
                continue
            last_time = now
            if self.on_press:
                self.on_press()
