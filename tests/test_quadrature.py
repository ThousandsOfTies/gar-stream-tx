from __future__ import annotations

import unittest
from pathlib import Path

from quadrature import QuadratureDecoder


class QuadratureDecoderTest(unittest.TestCase):
    def test_physical_reader_requests_pull_up_for_all_encoder_inputs(self) -> None:
        reader = (Path(__file__).resolve().parents[1] / "ky040.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('gpio.bias = "pull_up"', reader)
        self.assertIn("reorder_window_ns", reader)

    def test_each_complete_direction_emits_exactly_one_step(self) -> None:
        clockwise = QuadratureDecoder(True, True)
        self.assertEqual(
            [None, None, None, 1],
            [
                clockwise.update(False, True),
                clockwise.update(False, False),
                clockwise.update(True, False),
                clockwise.update(True, True),
            ],
        )
        counter_clockwise = QuadratureDecoder(True, True)
        self.assertEqual(
            [None, None, None, -1],
            [
                counter_clockwise.update(True, False),
                counter_clockwise.update(False, False),
                counter_clockwise.update(False, True),
                counter_clockwise.update(True, True),
            ],
        )

    def test_contact_bounce_does_not_advance_the_cursor(self) -> None:
        decoder = QuadratureDecoder(True, True)
        updates = [
            decoder.update(False, True),
            decoder.update(True, True),
            decoder.update(False, True),
            decoder.update(False, False),
            decoder.update(True, False),
            decoder.update(True, True),
        ]
        self.assertEqual([None, None, None, None, None, 1], updates)

    def test_invalid_phase_jump_does_not_emit_a_step(self) -> None:
        decoder = QuadratureDecoder(True, True)
        self.assertIsNone(decoder.update(False, False))
        self.assertIsNone(decoder.update(True, True))

    def test_individual_gpio_events_keep_all_four_transitions(self) -> None:
        decoder = QuadratureDecoder(True, True)
        self.assertEqual(
            [None, None, None, 1],
            [
                decoder.update_phase("clock", False),
                decoder.update_phase("data", False),
                decoder.update_phase("clock", True),
                decoder.update_phase("data", True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
