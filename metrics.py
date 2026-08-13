"""Atomic, machine-readable GarStream application telemetry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Mapping


def measured_fps(frame_count: int, first_frame_ns: int, last_frame_ns: int) -> float:
    """Return observed FPS from the intervals between timestamped frames."""
    if frame_count < 2 or last_frame_ns <= first_frame_ns:
        return 0.0
    return (frame_count - 1) / ((last_frame_ns - first_frame_ns) / 1_000_000_000)


class MetricsWriter:
    """Write one complete JSON observation or leave telemetry disabled.

    The path is deliberately supplied at runtime.  Artifacts therefore never
    contain a host path, bridge URL, or peer address.
    """

    def __init__(self, path: str | None = None) -> None:
        configured_path = path or os.environ.get("GAR_STREAM_METRICS_PATH")
        self.path = Path(configured_path) if configured_path else None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, payload: Mapping[str, object]) -> None:
        if self.path is None:
            return
        encoded = (
            json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        )
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as stream:
                stream.write(encoded)
                temporary = Path(stream.name)
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.path)
        except OSError:
            # Observation must never stop the camera/receiver data path.
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
