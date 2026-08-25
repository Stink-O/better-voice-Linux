import CoreGraphics
import Foundation

public struct CircleGesture: Equatable {
    public let center: CGPoint
    public let radius: CGFloat

    public init(center: CGPoint, radius: CGFloat) {
        self.center = center
        self.radius = radius
    }
}

/// Detects one closed, roughly circular mouse stroke at a time.
///
/// The detector intentionally uses a small rolling path and a few geometric
/// checks. It is forgiving enough for a hand-drawn circle and rejects ordinary
/// pointer movement without needing a gesture framework.
public struct CircleGestureDetector {
    private struct Sample {
        let point: CGPoint
        let time: TimeInterval
    }

    private var samples: [Sample] = []
    private var cooldownUntil: TimeInterval = 0
    private var waitingForExit: CircleGesture?
    private let window: TimeInterval = 6

    public init() {}

    public mutating func reset() {
        samples.removeAll(keepingCapacity: true)
        cooldownUntil = 0
        waitingForExit = nil
    }

    public mutating func add(point: CGPoint, at time: TimeInterval) -> CircleGesture? {
        if let gesture = waitingForExit {
            guard hypot(point.x - gesture.center.x, point.y - gesture.center.y) > gesture.radius * 1.5 else {
                return nil
            }
            waitingForExit = nil
            samples.removeAll(keepingCapacity: true)
        }

        guard time >= cooldownUntil else { return nil }

        if let previous = samples.last, time - previous.time > 0.45 {
            samples.removeAll(keepingCapacity: true)
        }

        samples.append(Sample(point: point, time: time))
        let cutoff = time - window
        samples.removeAll { $0.time < cutoff }

        guard time >= cooldownUntil, samples.count >= 18 else { return nil }
        guard let gesture = recognizedGesture() else { return nil }

        samples.removeAll(keepingCapacity: true)
        cooldownUntil = time + 0.65
        waitingForExit = gesture
        return gesture
    }

    private func recognizedGesture() -> CircleGesture? {
        guard let last = samples.last?.point else { return nil }
        for start in stride(from: samples.count - 18, through: 0, by: -1) {
            let first = samples[start].point
            guard hypot(first.x - last.x, first.y - last.y) < 160 else { continue }
            if let gesture = recognizedGesture(in: Array(samples[start...])) {
                return gesture
            }
        }
        return nil
    }

    private func recognizedGesture(in samples: [Sample]) -> CircleGesture? {
        guard let first = samples.first?.point, let last = samples.last?.point else { return nil }

        let points = samples.map(\.point)
        let minX = points.map(\.x).min() ?? 0
        let maxX = points.map(\.x).max() ?? 0
        let minY = points.map(\.y).min() ?? 0
        let maxY = points.map(\.y).max() ?? 0
        let width = maxX - minX
        let height = maxY - minY
        guard width >= 28, height >= 28 else { return nil }
        guard width / height > 0.45, width / height < 2.2 else { return nil }

        let center = CGPoint(x: (minX + maxX) / 2, y: (minY + maxY) / 2)
        let distances = points.map { hypot($0.x - center.x, $0.y - center.y) }
        let radius = distances.reduce(0, +) / CGFloat(distances.count)
        guard radius >= 18 else { return nil }

        let variance = distances.reduce(0) { $0 + pow($1 - radius, 2) } / CGFloat(distances.count)
        guard sqrt(variance) / radius < 0.32 else { return nil }

        let closure = hypot(first.x - last.x, first.y - last.y)
        guard closure < max(20, radius * 0.65) else { return nil }

        var angleTravel: CGFloat = 0
        for pair in zip(distances.indices, distances.indices.dropFirst()) {
            let a = points[pair.0]
            let b = points[pair.1]
            let current = atan2(a.y - center.y, a.x - center.x)
            let next = atan2(b.y - center.y, b.x - center.x)
            var delta = next - current
            while delta > .pi { delta -= 2 * .pi }
            while delta < -.pi { delta += 2 * .pi }
            angleTravel += abs(delta)
        }
        guard angleTravel > 5.93, angleTravel < 8.8 else { return nil }

        let pathLength = zip(points, points.dropFirst()).reduce(CGFloat.zero) {
            $0 + hypot($1.1.x - $1.0.x, $1.1.y - $1.0.y)
        }
        let circumference = 2 * .pi * radius
        guard pathLength / circumference > 0.65, pathLength / circumference < 1.9 else { return nil }

        return CircleGesture(center: center, radius: radius)
    }
}
