"""Detects one closed, roughly circular pointer stroke at a time.

Direct port of ``Sources/BetterVoiceCore/CircleGestureDetector.swift``. The
thresholds are kept identical so the Linux build feels the same as the macOS
build: forgiving for a hand-drawn circle, quiet during ordinary pointer motion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise

Point = tuple[float, float]


@dataclass(frozen=True)
class CircleGesture:
    center: Point
    radius: float


@dataclass
class _Sample:
    point: Point
    time: float


@dataclass
class CircleGestureDetector:
    _samples: list[_Sample] = field(default_factory=list)
    _cooldown_until: float = 0.0
    _waiting_for_exit: CircleGesture | None = None
    window: float = 6.0

    def reset(self) -> None:
        self._samples.clear()
        self._cooldown_until = 0.0
        self._waiting_for_exit = None

    def add(self, point: Point, at: float) -> CircleGesture | None:
        gesture = self._waiting_for_exit
        if gesture is not None:
            if math.hypot(point[0] - gesture.center[0], point[1] - gesture.center[1]) <= gesture.radius * 1.5:
                return None
            self._waiting_for_exit = None
            self._samples.clear()

        if at < self._cooldown_until:
            return None

        if self._samples and at - self._samples[-1].time > 0.45:
            self._samples.clear()

        self._samples.append(_Sample(point, at))
        cutoff = at - self.window
        self._samples = [sample for sample in self._samples if sample.time >= cutoff]

        if at < self._cooldown_until or len(self._samples) < 18:
            return None
        gesture = self._recognized_gesture()
        if gesture is None:
            return None

        self._samples.clear()
        self._cooldown_until = at + 0.65
        self._waiting_for_exit = gesture
        return gesture

    def _recognized_gesture(self) -> CircleGesture | None:
        if not self._samples:
            return None
        last = self._samples[-1].point
        for start in range(len(self._samples) - 18, -1, -1):
            first = self._samples[start].point
            if math.hypot(first[0] - last[0], first[1] - last[1]) >= 160:
                continue
            gesture = self._recognized_gesture_in(self._samples[start:])
            if gesture is not None:
                return gesture
        return None

    @staticmethod
    def _recognized_gesture_in(samples: list[_Sample]) -> CircleGesture | None:
        if not samples:
            return None
        first = samples[0].point
        last = samples[-1].point

        points = [sample.point for sample in samples]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        width = max_x - min_x
        height = max_y - min_y
        if width < 28 or height < 28:
            return None
        if height == 0:
            return None
        aspect = width / height
        if not 0.45 < aspect < 2.2:
            return None

        center = ((min_x + max_x) / 2, (min_y + max_y) / 2)
        distances = [math.hypot(point[0] - center[0], point[1] - center[1]) for point in points]
        radius = sum(distances) / len(distances)
        if radius < 18:
            return None

        variance = sum((distance - radius) ** 2 for distance in distances) / len(distances)
        if math.sqrt(variance) / radius >= 0.42:
            return None

        closure = math.hypot(first[0] - last[0], first[1] - last[1])
        if closure >= max(20.0, radius * 0.65):
            return None

        angle_travel = 0.0
        for current_point, next_point in pairwise(points):
            current = math.atan2(current_point[1] - center[1], current_point[0] - center[0])
            following = math.atan2(next_point[1] - center[1], next_point[0] - center[0])
            delta = following - current
            while delta > math.pi:
                delta -= 2 * math.pi
            while delta < -math.pi:
                delta += 2 * math.pi
            angle_travel += abs(delta)
        if not 4.5 < angle_travel < 8.8:
            return None

        path_length = sum(
            math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in pairwise(points)
        )
        circumference = 2 * math.pi * radius
        ratio = path_length / circumference
        if not 0.65 < ratio < 1.9:
            return None

        return CircleGesture(center=center, radius=radius)
