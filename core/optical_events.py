"""Automatic flight-event detection from a flight's assembled
TrajectoryPoints (core.optical_trajectory) - deterministic position-based
signal thresholds, no ML.

Being honest about what pure 3D position-over-time can actually tell us:
- first_motion and launch are the same detectable instant here - this
  system can't separately resolve ignition vs. liftoff for a water
  rocket from position data alone.
- burnout has a real, deterministic proxy: peak ascent speed (a rocket
  accelerates while thrusting, decelerates once it stops).
- rail_departure is never auto-detected - it needs the rail's
  length/orientation, which nothing in this system tracks. Left out
  entirely rather than guessed at.
- confidence stays null on every event, same reasoning as
  TriangulatedPoint.confidence (Phase 7): a real confidence number needs
  empirical validation this project doesn't have.
"""
import math

from .optical_models import FlightEvent

DEFAULT_FIRST_MOTION_THRESHOLD_M = 0.3
DEFAULT_LANDING_HEIGHT_THRESHOLD_M = 1.0


def _velocity_series(points):
    """(t_mid_ms, speed_m_s) for consecutive pairs, from filtered position -
    same approach as core.optical_debrief."""
    series = []
    for prev, curr in zip(points, points[1:]):
        dt = (curr.timestamp_ms - prev.timestamp_ms) / 1000
        if dt <= 0:
            continue
        vx = (curr.filtered_x_m - prev.filtered_x_m) / dt
        vy = (curr.filtered_y_m - prev.filtered_y_m) / dt
        vz = (curr.filtered_z_m - prev.filtered_z_m) / dt
        speed = math.sqrt(vx * vx + vy * vy + vz * vz)
        series.append(((prev.timestamp_ms + curr.timestamp_ms) / 2, speed))
    return series


def detect_flight_events(
    flight,
    first_motion_threshold_m=DEFAULT_FIRST_MOTION_THRESHOLD_M,
    landing_height_threshold_m=DEFAULT_LANDING_HEIGHT_THRESHOLD_M,
):
    """(Re)detects FlightEvents for a flight from its current
    TrajectoryPoints. Deletes this flight's existing FlightEvents first -
    same recompute-don't-preserve rule as every other derived table in
    this subsystem. Returns the list of created FlightEvent instances."""
    points = list(flight.trajectory_points.order_by("timestamp_ms"))

    FlightEvent.objects.filter(flight=flight).delete()

    if len(points) < 2:
        return []

    start = points[0]
    first_motion_point = None
    for p in points:
        displacement = math.sqrt(
            (p.filtered_x_m - start.filtered_x_m) ** 2
            + (p.filtered_y_m - start.filtered_y_m) ** 2
            + (p.filtered_z_m - start.filtered_z_m) ** 2
        )
        if displacement > first_motion_threshold_m:
            first_motion_point = p
            break

    if first_motion_point is None:
        # No detectable motion - conservative: report nothing rather than
        # guess at events for what's likely degenerate/no-flight data.
        return []

    created = []

    created.append(
        FlightEvent.objects.create(
            session=flight.session,
            flight=flight,
            event_type=FlightEvent.EVENT_FIRST_MOTION,
            timestamp_ms=first_motion_point.timestamp_ms,
            detection_method=f"first point past a {first_motion_threshold_m}m displacement threshold from the start",
        )
    )
    created.append(
        FlightEvent.objects.create(
            session=flight.session,
            flight=flight,
            event_type=FlightEvent.EVENT_LAUNCH,
            timestamp_ms=first_motion_point.timestamp_ms,
            detection_method="coincides with first_motion",
            notes=(
                "This tracking method can't separately resolve ignition/liftoff for a "
                "water rocket from position data alone."
            ),
        )
    )

    apogee_point = max(points, key=lambda p: p.filtered_z_m)
    created.append(
        FlightEvent.objects.create(
            session=flight.session,
            flight=flight,
            event_type=FlightEvent.EVENT_APOGEE,
            timestamp_ms=apogee_point.timestamp_ms,
            detection_method="maximum filtered height",
        )
    )

    velocities = _velocity_series(points)
    ascent_velocities = [(t, speed) for t, speed in velocities if t <= apogee_point.timestamp_ms]
    if ascent_velocities:
        burnout_t, _ = max(ascent_velocities, key=lambda v: v[1])
        created.append(
            FlightEvent.objects.create(
                session=flight.session,
                flight=flight,
                event_type=FlightEvent.EVENT_BURNOUT,
                timestamp_ms=int(burnout_t),
                detection_method="peak ascent speed (proxy for end of powered flight)",
            )
        )

    landing_point = points[-1]
    confirmed = landing_point.filtered_z_m <= landing_height_threshold_m
    created.append(
        FlightEvent.objects.create(
            session=flight.session,
            flight=flight,
            event_type=FlightEvent.EVENT_LANDING,
            timestamp_ms=landing_point.timestamp_ms,
            detection_method=(
                "height dropped back near the pad"
                if confirmed
                else "last point multi-station coverage could still solve"
            ),
            notes="" if confirmed else "Coverage likely ended before actual touchdown.",
        )
    )

    return created
