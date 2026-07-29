"""Solves a 3D point from each grid timestep's rays (core.optical_rays)
and persists the result to TriangulatedPoint. Mirrors the clinometer's
core/triangulation.py `triangulate()` closely: least-squares closest-
point-to-multiple-lines, then iteratively drop the worst-performing
station if it's a clear outlier.

Kept as its own module (not importing the clinometer's private
_solve_point/_residual) to keep the optical subsystem self-contained,
matching every prior phase's separation from the clinometer code - the
vector math itself is tiny and unit-agnostic, so duplicating it here
costs little.

Units: meters throughout (see core/optical_models.py's ENU convention).
The clinometer's outlier thresholds (5ft / 15ft) don't transfer as
numbers to a meters-based system - see the configurable defaults below,
converted rather than copied verbatim.

Confirmed via testing: the worst-vs-second-worst rejection check needs a
stable "healthy majority" to compare against. With exactly 3 rays (the
minimum), a single bad one pulls the shared least-squares fit toward it
enough that the two *good* stations' residuals inflate too, often close
to the bad one's - so a single outlier among only 3 stations may not be
reliably distinguished and rejected, even though the resulting point
still stays roughly correct (bounded by 1-of-3 votes, not wildly wrong).
With 4+ stations (3+ good, 1 bad) the good majority anchors the fit and
the bad ray's residual clearly stands out. This is a real, observed
property of the algorithm, not a bug - more stations make rejection more
reliable, not just more redundant.
"""
import math

import numpy as np

from .optical_models import TriangulatedPoint
from .optical_rays import generate_rays_for_flight

DEFAULT_OUTLIER_MIN_RESIDUAL_M = 1.5
DEFAULT_OUTLIER_RATIO_THRESHOLD = 1.8
DEFAULT_OUTLIER_ABSOLUTE_GAP_M = 4.5


def _solve_point(rays):
    """rays: list of (origin: np.array(3), direction: np.array(3), unit).
    Closest point to a set of 3D lines, least squares."""
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for origin, direction in rays:
        proj = np.eye(3) - np.outer(direction, direction)
        A += proj
        b += proj @ origin
    point, *_ = np.linalg.lstsq(A, b, rcond=None)
    return point


def _residual(point, origin, direction):
    proj = np.eye(3) - np.outer(direction, direction)
    return float(np.linalg.norm(proj @ (point - origin)))


def _geometry_quality(directions):
    """Mean pairwise angle (degrees) between surviving ray directions -
    a simple, deterministic proxy for how well-separated the viewing
    angles are. Higher is better-conditioned; None for fewer than 2."""
    angles = []
    for i in range(len(directions)):
        for j in range(i + 1, len(directions)):
            cos_angle = float(np.clip(np.dot(directions[i], directions[j]), -1.0, 1.0))
            angles.append(math.degrees(math.acos(cos_angle)))
    return sum(angles) / len(angles) if angles else None


def triangulate_flight(
    flight,
    outlier_min_residual_m=DEFAULT_OUTLIER_MIN_RESIDUAL_M,
    outlier_ratio_threshold=DEFAULT_OUTLIER_RATIO_THRESHOLD,
    outlier_absolute_gap_m=DEFAULT_OUTLIER_ABSOLUTE_GAP_M,
):
    """(Re)computes TriangulatedPoints for a flight from its current rays.
    Deletes this flight's existing TriangulatedPoints first - they're a
    derived, recomputable artifact (never hand-edited), so regenerating
    them whenever observations change is correct, not lossy. Returns the
    list of created TriangulatedPoint instances."""
    entries = generate_rays_for_flight(flight)

    TriangulatedPoint.objects.filter(flight=flight).delete()

    created = []
    for entry in entries:
        rays = entry["rays"]
        if len(rays) < 2:
            continue

        indices = list(range(len(rays)))

        def solve_with(idxs):
            active = [(rays[i][1], rays[i][2]) for i in idxs]
            point = _solve_point(active)
            residuals = {i: _residual(point, rays[i][1], rays[i][2]) for i in idxs}
            return point, residuals

        point, residuals = solve_with(indices)
        rejected = []

        while len(indices) > 2:
            ranked = sorted(((i, residuals[i]) for i in indices), key=lambda kv: kv[1], reverse=True)
            worst_i, worst_val = ranked[0]
            second_val = ranked[1][1]
            is_outlier = worst_val > outlier_min_residual_m and (
                worst_val > outlier_ratio_threshold * second_val
                or worst_val - second_val > outlier_absolute_gap_m
            )
            if not is_outlier:
                break
            indices.remove(worst_i)
            rejected.append(worst_i)
            point, residuals = solve_with(indices)

        stations_used = [rays[i][0].id for i in indices]
        rejected_stations = [rays[i][0].id for i in rejected]
        residual_values = [residuals[i] for i in indices]
        directions = [rays[i][2] for i in indices]

        created.append(
            TriangulatedPoint.objects.create(
                session=flight.session,
                flight=flight,
                synchronized_timestamp_ms=entry["synchronized_timestamp_ms"],
                x_m=float(point[0]),
                y_m=float(point[1]),
                z_m=float(point[2]),
                stations_used=stations_used,
                stations_used_count=len(stations_used),
                rejected_stations=rejected_stations,
                residual_error_m=float(max(residual_values)) if residual_values else None,
                geometry_quality=_geometry_quality(directions),
            )
        )

    return created
