from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from metrics import MetricsWriter, measured_fps


class MetricsWriterTest(unittest.TestCase):
    def test_measured_fps_counts_intervals_not_frames(self) -> None:
        self.assertEqual(0.0, measured_fps(1, 1, 1))
        self.assertEqual(1.0, measured_fps(2, 1, 1_000_000_001))

    def test_writes_a_complete_atomic_json_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "nested" / "metrics.json"
            writer = MetricsWriter(str(path))
            writer.write({"schema_version": 1, "frames": {"sent_count": 2}})
            self.assertEqual(
                {"schema_version": 1, "frames": {"sent_count": 2}},
                json.loads(path.read_text()),
            )
            self.assertEqual([], list(path.parent.glob(".metrics.json.*")))


if __name__ == "__main__":
    unittest.main()
