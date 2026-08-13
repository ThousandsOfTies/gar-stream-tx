"""Debounced Gray-code decoder for a mechanical quadrature encoder."""

from __future__ import annotations


class QuadratureDecoder:
    """Emit one direction only after a complete return to the idle detent.

    KY-040 encoders rest with both phases high.  Counting all four legal
    Gray-code transitions rejects contact bounce because a reversed edge
    cancels the preceding transition rather than moving a menu cursor.
    """

    _TRANSITIONS = (
        0,
        -1,
        1,
        0,
        1,
        0,
        0,
        -1,
        -1,
        0,
        0,
        1,
        0,
        1,
        -1,
        0,
    )
    _IDLE_STATE = 0b11

    def __init__(self, clock: bool, data: bool) -> None:
        self._state = self._phase_state(clock, data)
        self._accumulator = 0

    def update(self, clock: bool, data: bool) -> int | None:
        """Return ``-1`` or ``1`` for one completed detent, otherwise None."""

        next_state = self._phase_state(clock, data)
        if next_state == self._state:
            return None
        transition = self._TRANSITIONS[(self._state << 2) | next_state]
        self._state = next_state
        if transition == 0:
            self._accumulator = 0
            return None
        self._accumulator += transition
        if self._state != self._IDLE_STATE:
            return None
        direction = 1 if self._accumulator >= 4 else -1 if self._accumulator <= -4 else 0
        self._accumulator = 0
        return direction or None

    @staticmethod
    def _phase_state(clock: bool, data: bool) -> int:
        return (0b10 if clock else 0) | (0b01 if data else 0)
