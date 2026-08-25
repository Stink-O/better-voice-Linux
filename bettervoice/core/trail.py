"""Links only nearby samples so pauses and pointer jumps leave separate strokes.

Port of ``Sources/BetterVoiceCore/TrailSegments.swift``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

Point = tuple[float, float]


@dataclass(frozen=True)
class TrailSegment:
    start: int
    end: int


def trail_segments(
    points: Sequence[Point],
    times: Sequence[float],
    maximum_gap: float = 0.18,
    maximum_distance: float = 160.0,
) -> list[TrailSegment]:
    if len(points) != len(times) or len(points) <= 1:
        return []

    segments: list[TrailSegment] = []
    for index in range(1, len(points)):
        gap = times[index] - times[index - 1]
        distance = math.hypot(
            points[index][0] - points[index - 1][0],
            points[index][1] - points[index - 1][1],
        )
        if 0 <= gap <= maximum_gap and distance <= maximum_distance:
            segments.append(TrailSegment(index - 1, index))
    return segments
