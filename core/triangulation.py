import math

import numpy as np

GRID_STEP_MS = 50


def station_ground_position(station):
    """(east, north, 0) ground position of a station relative to the pad
    at the origin. bearing_degrees was captured pointing AT the pad, so
    the station itself sits at the reciprocal bearing."""
    reverse_bearing = math.radians((station.bearing_degrees + 180) % 360)
    x = station.distance_ft * math.sin(reverse_bearing)
    y = station.distance_ft * math.cos(reverse_bearing)
    return np.array([x, y, 0.0])


def ray_direction(elevation_deg, azimuth_deg):
    elev = math.radians(elevation_deg)
    az = math.radians(azimuth_deg)
    d = np.array(
        [
            math.cos(elev) * math.sin(az),
            math.cos(elev) * math.cos(az),
            math.sin(elev),
        ]
    )
    norm = np.linalg.norm(d)
    return d / norm if norm else d


def _signed_angle_diff(a, b):
    """Shortest signed difference b - a, wrapped to (-180, 180]."""
    return (b - a + 180) % 360 - 180


def _interp_series(samples, grid_times):
    """samples: time-sorted list of {"t", "elevation", "azimuth"}.
    Returns a list, parallel to grid_times, of (elevation, azimuth) tuples
    or None where a grid time falls outside this station's own recorded
    range (never extrapolated)."""
    ts = [s["t"] for s in samples]
    if len(ts) < 2:
        return [None] * len(grid_times)

    elevations = [s["elevation"] for s in samples]
    azimuths = [s["azimuth"] for s in samples]

    results = []
    j = 0
    for t in grid_times:
        if t < ts[0] or t > ts[-1]:
            results.append(None)
            continue
        while j < len(ts) - 2 and ts[j + 1] < t:
            j += 1
        t0, t1 = ts[j], ts[j + 1]
        e0, e1 = elevations[j], elevations[j + 1]
        a0, a1 = azimuths[j], azimuths[j + 1]
        frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        elevation = e0 + (e1 - e0) * frac
        azimuth = (a0 + _signed_angle_diff(a0, a1) * frac) % 360
        results.append((elevation, azimuth))
    return results


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


def triangulate(stations_samples):
    """stations_samples: list of (Station, time-sorted sample list) pairs,
    each with 2+ samples. Returns (best_altitude_ft, {used_station_id, ...})
    or None if there's no usable time overlap across the stations."""
    starts = [samples[0]["t"] for _, samples in stations_samples]
    ends = [samples[-1]["t"] for _, samples in stations_samples]
    grid_start = max(starts)
    grid_end = min(ends)
    if grid_end <= grid_start:
        return None

    grid_times = list(range(int(grid_start), int(grid_end) + 1, GRID_STEP_MS))
    if len(grid_times) < 2:
        return None

    stations = [s for s, _ in stations_samples]
    origins = [station_ground_position(s) for s in stations]
    interpolated = [_interp_series(samples, grid_times) for _, samples in stations_samples]

    def solve_with(indices):
        heights = []
        per_station_residuals = {i: [] for i in indices}
        for t_idx in range(len(grid_times)):
            rays = []
            active = []
            for i in indices:
                sample = interpolated[i][t_idx]
                if sample is None:
                    continue
                elevation, azimuth = sample
                direction = ray_direction(elevation, azimuth)
                rays.append((origins[i], direction))
                active.append(i)
            if len(rays) < 2:
                continue
            point = _solve_point(rays)
            heights.append(point[2])
            for i, (origin, direction) in zip(active, rays):
                per_station_residuals[i].append(_residual(point, origin, direction))
        return heights, per_station_residuals

    indices = list(range(len(stations)))
    heights, per_station_residuals = solve_with(indices)

    while len(indices) > 2:
        mean_residuals = {
            i: sum(per_station_residuals[i]) / len(per_station_residuals[i])
            for i in indices
            if per_station_residuals[i]
        }
        if len(mean_residuals) < 2:
            break
        # A single bad ray pulls the shared least-squares solution away from
        # the good rays' true intersection too, inflating every station's
        # residual - so the worst offender has to be judged against the
        # *next-worst* station, not a median that's already contaminated by
        # the same outlier it's supposed to help catch.
        ranked = sorted(mean_residuals.items(), key=lambda kv: kv[1], reverse=True)
        worst_i, worst_val = ranked[0]
        second_val = ranked[1][1]
        is_outlier = worst_val > 5.0 and (worst_val > 1.8 * second_val or worst_val - second_val > 15.0)
        if not is_outlier:
            break
        indices.remove(worst_i)
        heights, per_station_residuals = solve_with(indices)

    if not heights:
        return None

    used_ids = {stations[i].id for i in indices}
    return float(max(heights)), used_ids
