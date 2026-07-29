"""Derived flight statistics computed on demand from a flight's assembled
TrajectoryPoints (core.optical_trajectory) - the optical analog of
core/debrief.py's build_debrief() for the clinometer. Recomputed fresh
each call rather than persisted, same philosophy as that module: cheap
at this scale, and there's no new row to keep in sync with upstream
changes.

Units: meters/seconds/degrees throughout (see core/optical_models.py's
ENU convention) - the clinometer's equivalent numbers are in feet.
"""
import math

LANDING_HEIGHT_CAUTION_M = 2.0


def compute_derived_flight_data(flight):
    points = list(flight.trajectory_points.order_by("timestamp_ms"))

    if len(points) < 2:
        return {
            "flight_duration_s": None,
            "time_to_apogee_s": None,
            "apogee_height_m": None,
            "ascent_rate_max_m_s": None,
            "descent_rate_max_m_s": None,
            "max_speed_m_s": None,
            "drift_at_apogee_m": None,
            "landing_estimate": None,
            "limitations": ["Not enough trajectory data to compute derived flight metrics."],
        }

    t0 = points[0].timestamp_ms
    flight_duration_s = (points[-1].timestamp_ms - t0) / 1000

    # Velocity via finite differences of the *filtered* position - the
    # smoothed curve, not raw, since differentiating noisy raw positions
    # would amplify that noise.
    velocities = []  # (t_mid_ms, vx, vy, vz)
    for prev, curr in zip(points, points[1:]):
        dt = (curr.timestamp_ms - prev.timestamp_ms) / 1000
        if dt <= 0:
            continue
        velocities.append(
            (
                (prev.timestamp_ms + curr.timestamp_ms) / 2,
                (curr.filtered_x_m - prev.filtered_x_m) / dt,
                (curr.filtered_y_m - prev.filtered_y_m) / dt,
                (curr.filtered_z_m - prev.filtered_z_m) / dt,
            )
        )

    apogee = max(points, key=lambda p: p.filtered_z_m)
    time_to_apogee_s = (apogee.timestamp_ms - t0) / 1000
    drift_at_apogee_m = math.hypot(apogee.filtered_x_m, apogee.filtered_y_m)

    ascent_rates = [vz for t_mid, vx, vy, vz in velocities if t_mid <= apogee.timestamp_ms]
    descent_rates = [vz for t_mid, vx, vy, vz in velocities if t_mid > apogee.timestamp_ms]
    ascent_rate_max_m_s = max(ascent_rates) if ascent_rates else 0.0
    descent_rate_max_m_s = abs(min(descent_rates)) if descent_rates else 0.0

    max_speed_m_s = (
        max(math.sqrt(vx * vx + vy * vy + vz * vz) for _, vx, vy, vz in velocities)
        if velocities
        else 0.0
    )

    landing_point = points[-1]
    landing_distance_m = math.hypot(landing_point.filtered_x_m, landing_point.filtered_y_m)
    landing_bearing_deg = math.degrees(
        math.atan2(landing_point.filtered_x_m, landing_point.filtered_y_m)
    ) % 360

    limitations = []
    if landing_point.filtered_z_m > LANDING_HEIGHT_CAUTION_M:
        limitations.append(
            f"Multi-station coverage ended while the rocket was still "
            f"~{landing_point.filtered_z_m:.1f}m up - this is the last point we could "
            "solve, not a confirmed landing."
        )
    if any(p.is_gap_filled for p in points):
        gap_count = sum(1 for p in points if p.is_gap_filled)
        limitations.append(
            f"Trajectory includes {gap_count} interpolated point(s) filling brief "
            "gaps in station coverage."
        )

    return {
        "flight_duration_s": flight_duration_s,
        "time_to_apogee_s": time_to_apogee_s,
        "apogee_height_m": apogee.filtered_z_m,
        "ascent_rate_max_m_s": ascent_rate_max_m_s,
        "descent_rate_max_m_s": descent_rate_max_m_s,
        "max_speed_m_s": max_speed_m_s,
        "drift_at_apogee_m": drift_at_apogee_m,
        "landing_estimate": {
            "distance_m": landing_distance_m,
            "bearing_deg": landing_bearing_deg,
        },
        "limitations": limitations,
    }
