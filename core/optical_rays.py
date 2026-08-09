"""Turns a station's per-frame bearing observations into 3D rays (origin
+ bearing direction) aligned onto a shared time grid across stations -
the direct input Phase 7's triangulation will consume. Mirrors
core/triangulation.py's shape for the clinometer: interpolate onto a
common grid, never extrapolate past what a station actually recorded.

An observation's bearing comes from one of two sources: a tapped pixel
(manual/assisted/automatic/simulated - resolved via pixel_to_bearing_vector
against the station's locked calibration) or a live device-orientation
sample (orientation - resolved via camera_forward_vector directly, since
the sample already carries its own facing/pitch, bypassing pixel
geometry and the locked calibration's stored orientation entirely).

No 3D point-solving or outlier rejection happens here - purely ray
generation. See core/triangulation.py's `triangulate()` for the pattern
a future multi-station solver here would follow.
"""
import numpy as np

from .optical_camera import camera_forward_vector, pixel_to_bearing_vector
from .optical_models import PixelObservation

GRID_STEP_MS = 50


def station_ground_position(station):
    """(east, north, up) ENU position of a station, meters, relative to
    the pad at the origin."""
    return np.array([station.surveyed_x_m, station.surveyed_y_m, station.surveyed_z_m])


def _observation_bearing(obs, calibration):
    if obs.facing_deg is not None and obs.pitch_deg is not None:
        return camera_forward_vector(obs.facing_deg, obs.pitch_deg)
    return pixel_to_bearing_vector(obs.pixel_x, obs.pixel_y, calibration)


def station_bearing_series(station, flight):
    """Time-sorted list of (synchronized_timestamp_ms, bearing unit
    vector) for this station's valid, current observations in the given
    flight that have a synchronized_timestamp_ms - unsynced observations
    are excluded, since they can't be placed on a shared time grid yet."""
    calibration = station.calibrations.filter(is_active=True).first()
    if calibration is None:
        return []

    observations = (
        PixelObservation.objects.filter(
            frame__station=station,
            frame__flight=flight,
            valid=True,
            is_current=True,
            frame__synchronized_timestamp_ms__isnull=False,
        )
        .select_related("frame")
        .order_by("frame__synchronized_timestamp_ms")
    )

    return [
        (obs.frame.synchronized_timestamp_ms, np.array(_observation_bearing(obs, calibration)))
        for obs in observations
    ]


def _interp_bearing_series(series, grid_times):
    """series: time-sorted list of (t, bearing unit-vector np.array).
    Returns a list, parallel to grid_times, of interpolated unit vectors
    (normalize-after-linear-interpolate - "nlerp") or None where a grid
    time falls outside this station's own recorded range (never
    extrapolated), matching core.triangulation._interp_series exactly."""
    ts = [t for t, _ in series]
    if len(ts) < 2:
        return [None] * len(grid_times)

    bearings = [b for _, b in series]

    results = []
    j = 0
    for t in grid_times:
        if t < ts[0] or t > ts[-1]:
            results.append(None)
            continue
        while j < len(ts) - 2 and ts[j + 1] < t:
            j += 1
        t0, t1 = ts[j], ts[j + 1]
        b0, b1 = bearings[j], bearings[j + 1]
        frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        blended = b0 + (b1 - b0) * frac
        norm = np.linalg.norm(blended)
        results.append(blended / norm if norm else blended)
    return results


def stations_with_bearing_data(flight):
    """Stations (from this flight's session) with an active calibration, a
    known ENU position, and 2+ usable (valid, current, synchronized)
    bearing observations for this specific flight - the actual per-flight
    coverage, not just how many stations are registered in the session.
    Used both to decide whether a flight qualifies for real triangulation
    (generate_rays_for_flight, below) and to tell the UI which comparison
    mode a given flight actually belongs in (core/optical_serializers.py's
    tracking_station_count)."""
    stations = [
        s
        for s in flight.session.stations.all()
        if s.calibrations.filter(is_active=True).exists()
        and None not in (s.surveyed_x_m, s.surveyed_y_m, s.surveyed_z_m)
    ]
    return [s for s in stations if len(station_bearing_series(s, flight)) >= 2]


def generate_rays_for_flight(flight):
    """Returns a list of {"synchronized_timestamp_ms": t, "rays": [(station,
    origin, direction), ...]} entries spanning the overlapping time range
    of every qualifying station (2+ synchronized pixel taps, an active
    calibration, and a known ENU position). Individual grid timesteps may
    still have fewer rays than the full station count - a station's own
    internal gaps (skipped frames) are preserved, not filled in. Whether
    a given timestep has "enough" rays to solve is Phase 7's decision,
    not this function's - it returns whatever coverage actually exists."""
    qualifying_stations = stations_with_bearing_data(flight)
    if len(qualifying_stations) < 2:
        return []

    series_by_station = {
        station.id: (station, station_bearing_series(station, flight)) for station in qualifying_stations
    }

    starts = [series[0][0] for _, series in series_by_station.values()]
    ends = [series[-1][0] for _, series in series_by_station.values()]
    grid_start = max(starts)
    grid_end = min(ends)
    if grid_end <= grid_start:
        return []

    grid_times = list(range(int(grid_start), int(grid_end) + 1, GRID_STEP_MS))
    if len(grid_times) < 2:
        return []

    interpolated = {
        station_id: (station, _interp_bearing_series(series, grid_times))
        for station_id, (station, series) in series_by_station.items()
    }
    origins = {
        station_id: station_ground_position(station) for station_id, (station, _) in interpolated.items()
    }

    entries = []
    for t_idx, t in enumerate(grid_times):
        rays = []
        for station_id, (station, bearings) in interpolated.items():
            bearing = bearings[t_idx]
            if bearing is None:
                continue
            rays.append((station, origins[station_id], bearing))
        entries.append({"synchronized_timestamp_ms": t, "rays": rays})

    return entries
