#!/usr/bin/env python3
"""KY-040 rotary encoder + push-button reader using periphery.GPIO edge events.

Copied from gar-stream-rx (same board-agnostic driver - only the GPIO pin
numbers passed in differ between projects). Uses the same simple "CLK
changed, compare against DT" quadrature approach as most KY-040 tutorials
(not a full Gray-code state machine) - good enough for a casual demo, with a
small time-based debounce to filter contact bounce.

Note: most KY-040 breakout boards don't have onboard pull-ups/downs on
CLK/DT/SW. If the encoder is jittery, add 10k pull-up resistors to 3.3V on
CLK/DT/SW (see README). GAR_GPIO_CHIP selects the Linux GPIO character device;
omitting it retains compatibility with the legacy sysfs backend.
"""
import os
import threading
import time

from periphery import GPIO


def _open_gpio(line, direction):
    chip = os.environ.get("GAR_GPIO_CHIP")
    return GPIO(chip, line, direction) if chip else GPIO(line, direction)


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
        # One detent produces a rising and a falling CLK edge.  Handling only
        # the falling edge makes one physical detent exactly one UI step.
        self.clk.edge = "falling"
        self.sw.edge = "falling"

        self.on_rotate = on_rotate
        self.on_press = on_press
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
        last_time = 0.0
        while self._running:
            if not self.clk.poll(0.5):
                continue
            event_level = _read_edge(self.clk)
            clk_state = self.clk.read() if event_level is None else event_level
            now = time.monotonic()
            if now - last_time < self.bounce_s:
                continue
            last_time = now
            dt_state = self.dt.read()
            direction = -1 if dt_state == clk_state else 1
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
