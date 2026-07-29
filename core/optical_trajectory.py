"""Assembles a flight's sparse TriangulatedPoints (core.optical_triangulation)
into one continuous TrajectoryPoint sequence: small interior gaps get
linearly interpolated, large ones are left as honest holes (never
fabricated), and a light smoothing pass produces filtered_x/y/z_m
alongside the untouched raw_x/y/z_m - keeping measured and filtered data
clearly separate, per the project's own constraint.

Smoothing: a simple, configurable centered moving average - not
Savitzky-Golay/spline/Kalman (mentioned as options in the original
constraints), since none of those exist in this codebase today and this
system has no scipy dependency. A deliberate scope reduction, easy to
swap out later without touching anything upstream.

is_interpolated vs is_gap_filled: identical in this phase's output.
There's no resampling onto a *different* time grid yet that would make
them diverge - this phase only fills gaps on the same grid
core.optical_rays already established. A future phase resampling the
assembled curve itself is where the distinction would start to matter.
"""
from .optical_models import TrajectoryPoint, TriangulatedPoint
from .optical_rays import GRID_STEP_MS

DEFAULT_SMOOTHING_WINDOW_POINTS = 5
DEFAULT_MAX_GAP_FILL_MS = 1000


def _moving_average(values, window_points):
    """Centered moving average with a naturally shrinking window at both
    ends (no padding, so edges are never smoothed using fabricated
    values)."""
    if window_points <= 1:
        return list(values)

    half = window_points // 2
    n = len(values)
    result = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        result.append(sum(values[lo:hi]) / (hi - lo))
    return result


def assemble_trajectory_for_flight(
    flight,
    smoothing_window_points=DEFAULT_SMOOTHING_WINDOW_POINTS,
    max_gap_fill_ms=DEFAULT_MAX_GAP_FILL_MS,
):
    """(Re)assembles TrajectoryPoints for a flight from its current
    TriangulatedPoints. Deletes this flight's existing TrajectoryPoints
    first - same recompute-don't-preserve rule as TriangulatedPoint
    itself; the raw pixel taps underneath are the actual source of
    truth. Returns the list of created TrajectoryPoint instances."""
    points = list(TriangulatedPoint.objects.filter(flight=flight).order_by("synchronized_timestamp_ms"))

    TrajectoryPoint.objects.filter(flight=flight).delete()

    if len(points) < 2:
        return []

    assembled = [
        {
            "t": points[0].synchronized_timestamp_ms,
            "x": points[0].x_m,
            "y": points[0].y_m,
            "z": points[0].z_m,
            "source": points[0],
            "gap_filled": False,
        }
    ]

    for prev, curr in zip(points, points[1:]):
        gap_ms = curr.synchronized_timestamp_ms - prev.synchronized_timestamp_ms
        if GRID_STEP_MS < gap_ms <= max_gap_fill_ms:
            steps = gap_ms // GRID_STEP_MS
            for step in range(1, int(steps)):
                frac = step / steps
                assembled.append(
                    {
                        "t": prev.synchronized_timestamp_ms + step * GRID_STEP_MS,
                        "x": prev.x_m + (curr.x_m - prev.x_m) * frac,
                        "y": prev.y_m + (curr.y_m - prev.y_m) * frac,
                        "z": prev.z_m + (curr.z_m - prev.z_m) * frac,
                        "source": None,
                        "gap_filled": True,
                    }
                )
        # gap_ms > max_gap_fill_ms: an honest hole - no rows fabricated.

        assembled.append(
            {
                "t": curr.synchronized_timestamp_ms,
                "x": curr.x_m,
                "y": curr.y_m,
                "z": curr.z_m,
                "source": curr,
                "gap_filled": False,
            }
        )

    filtered_x = _moving_average([a["x"] for a in assembled], smoothing_window_points)
    filtered_y = _moving_average([a["y"] for a in assembled], smoothing_window_points)
    filtered_z = _moving_average([a["z"] for a in assembled], smoothing_window_points)

    created = [
        TrajectoryPoint.objects.create(
            session=flight.session,
            flight=flight,
            timestamp_ms=a["t"],
            source_point=a["source"],
            raw_x_m=a["x"],
            raw_y_m=a["y"],
            raw_z_m=a["z"],
            filtered_x_m=fx,
            filtered_y_m=fy,
            filtered_z_m=fz,
            is_interpolated=a["gap_filled"],
            is_gap_filled=a["gap_filled"],
        )
        for a, fx, fy, fz in zip(assembled, filtered_x, filtered_y, filtered_z)
    ]
    return created
