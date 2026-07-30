"""Tools for actually measuring this pipeline's accuracy, rather than
just asserting the code is structurally correct. Every earlier phase has
been careful never to claim unvalidated accuracy (TriangulatedPoint and
FlightEvent confidence stay null, calibration is labeled "estimated,"
landing estimates flag when they're not a confirmed touchdown) - this
module is what finally lets that be checked against a real number.

Important honesty boundary: `validate_pipeline_against_mock_flight`
validates the pipeline's math against *this project's own simulator*
(core.optical_mock), not against a real rocket. That's genuinely useful
(it catches regressions and quantifies the theoretical best case), but
it is not the same as validating real-world accuracy - camera FOV is
still just "estimated," compression/motion blur/lighting aren't
simulated, and no real GPS-surveyed field test has ever been run. Its
`caveat` field says this plainly rather than letting "validation" imply
more than it delivers.
"""
import math

from .optical_mock import generate_mock_tracking_session, rocket_trajectory
from .optical_trajectory import assemble_trajectory_for_flight
from .optical_triangulation import triangulate_flight

SYNTHETIC_VALIDATION_CAVEAT = (
    "This report compares the pipeline's output against core.optical_mock's own "
    "simulated ground truth - an idealized camera model with no compression, motion "
    "blur, or lighting effects, and perfect clock sync. It quantifies the pipeline's "
    "theoretical best case and catches regressions, but is not a substitute for "
    "validating against a real, independently-measured flight."
)


def compute_position_error(computed, known):
    """computed, known: (x_m, y_m, z_m) tuples. Plain distance math - no
    new geometry, just the comparison."""
    dx = computed[0] - known[0]
    dy = computed[1] - known[1]
    dz = computed[2] - known[2]
    return {
        "horizontal_error_m": math.hypot(dx, dy),
        "vertical_error_m": abs(dz),
        "total_error_m": math.sqrt(dx * dx + dy * dy + dz * dz),
    }


def validate_flight_against_known_point(flight, known_x_m, known_y_m, known_z_m, at_timestamp_ms=None):
    """Finds the TrajectoryPoint closest to at_timestamp_ms (defaulting
    to the flight's apogee instant - the most natural point to
    independently re-measure, e.g. with a rangefinder or a known marker
    height) and reports its error against the supplied known point."""
    points = list(flight.trajectory_points.order_by("timestamp_ms"))
    if not points:
        return {"detail": "This flight has no trajectory data to validate against yet."}

    if at_timestamp_ms is None:
        target_point = max(points, key=lambda p: p.filtered_z_m)
    else:
        target_point = min(points, key=lambda p: abs(p.timestamp_ms - at_timestamp_ms))

    computed = (target_point.filtered_x_m, target_point.filtered_y_m, target_point.filtered_z_m)
    known = (known_x_m, known_y_m, known_z_m)
    error = compute_position_error(computed, known)

    return {
        "compared_timestamp_ms": target_point.timestamp_ms,
        "computed_position_m": {"x": computed[0], "y": computed[1], "z": computed[2]},
        "known_position_m": {"x": known[0], "y": known[1], "z": known[2]},
        **error,
    }


def validate_pipeline_against_mock_flight(session_name="Validation Mock Flight"):
    """Runs the real Phase 7-8 pipeline (triangulate_flight,
    assemble_trajectory_for_flight) against a fresh mock flight, and at
    every resulting TrajectoryPoint compares the filtered position
    against rocket_trajectory()'s true simulated position at that exact
    timestamp. Returns mean/max error plus a per-point breakdown."""
    session = generate_mock_tracking_session(name=session_name)
    flight = session.flights.first()
    triangulate_flight(flight)
    assemble_trajectory_for_flight(flight)

    per_point_errors = []
    for point in flight.trajectory_points.order_by("timestamp_ms"):
        true_position = rocket_trajectory(point.timestamp_ms / 1000)
        computed = (point.filtered_x_m, point.filtered_y_m, point.filtered_z_m)
        error = compute_position_error(computed, true_position)
        per_point_errors.append({"timestamp_ms": point.timestamp_ms, **error})

    total_errors = [e["total_error_m"] for e in per_point_errors]
    mean_error_m = sum(total_errors) / len(total_errors) if total_errors else None
    max_error_m = max(total_errors) if total_errors else None

    return {
        "mean_error_m": mean_error_m,
        "max_error_m": max_error_m,
        "per_point_errors": per_point_errors,
        "caveat": SYNTHETIC_VALIDATION_CAVEAT,
    }
