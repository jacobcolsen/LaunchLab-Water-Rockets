import math

from .altitude import _single_station_estimate
from .models import Launch, Session
from .triangulation import GRID_STEP_MS, station_ground_position, triangulate


def build_debrief(launch: Launch) -> dict:
    """On-demand, richer companion to compute_result_for_launch - recomputed
    fresh from the raw Samples each time (cheap at this scale), not
    persisted, so Result stays lean and this can evolve independently."""
    samples = [s for s in launch.samples.select_related("station").all() if s.data]

    base = {
        "launch": {
            "id": launch.id,
            "number": launch.number,
            "name": launch.name,
            "status": launch.status,
        },
        "method": None,
        "best_altitude_ft": None,
        "station_breakdown": None,
        "stats": None,
        "trajectory_3d": None,
        "trajectory_2d": None,
        "stations": None,
        "limitations": [],
    }

    if not samples:
        base["limitations"] = ["No station data was uploaded for this launch."]
        return base

    if len(samples) == 1:
        sample = samples[0]
        station = sample.station
        sorted_data = sorted(sample.data, key=lambda s: s["t"])
        peak = _single_station_estimate(station, sorted_data)
        t0 = sorted_data[0]["t"]
        trajectory_2d = [
            {
                "t": (s["t"] - t0) / 1000,
                "height": max(0.0, station.distance_ft * math.tan(math.radians(s["elevation"]))),
            }
            for s in sorted_data
        ]
        flight_duration_s = (sorted_data[-1]["t"] - t0) / 1000
        base.update(
            {
                "method": "single_station",
                "best_altitude_ft": peak,
                "station_breakdown": {station.label: peak},
                "stats": {"flight_duration_s": flight_duration_s},
                "trajectory_2d": trajectory_2d,
                "limitations": [
                    "Only one station recorded data for this launch. This estimate "
                    "assumes the rocket stayed directly overhead the whole flight and "
                    "can't detect sideways drift, a real 3D path, or a landing location "
                    "- register 2+ stations for that."
                ],
            }
        )
        return base

    stations_samples = []
    single_estimates = {}
    for sample in samples:
        sorted_data = sorted(sample.data, key=lambda s: s["t"])
        if len(sorted_data) < 2:
            continue
        stations_samples.append((sample.station, sorted_data))
        single_estimates[sample.station.id] = _single_station_estimate(sample.station, sorted_data)

    if len(stations_samples) < 2:
        base["limitations"] = ["Not enough overlapping station data to reconstruct a trajectory."]
        return base

    triangulated = triangulate(stations_samples)
    if triangulated is None:
        base["limitations"] = [
            "Stations didn't have any overlapping recording window - nothing to reconstruct."
        ]
        return base

    best_altitude_ft, used_ids, trajectory = triangulated
    stations = [s for s, _ in stations_samples]

    breakdown = {
        station.label: {
            "single_station_estimate_ft": single_estimates[station.id],
            "used": station.id in used_ids,
        }
        for station in stations
    }

    t0 = trajectory[0][0]
    apogee = max(trajectory, key=lambda p: p[3])
    time_to_apogee_s = (apogee[0] - t0) / 1000
    flight_duration_s = (trajectory[-1][0] - t0) / 1000
    dt = GRID_STEP_MS / 1000

    ascent_rates = []
    descent_rates = []
    for prev, cur in zip(trajectory, trajectory[1:]):
        rate = (cur[3] - prev[3]) / dt
        if cur[0] <= apogee[0]:
            ascent_rates.append(rate)
        else:
            descent_rates.append(rate)
    ascent_rate_max_ft_s = max(ascent_rates) if ascent_rates else 0.0
    descent_rate_max_ft_s = abs(min(descent_rates)) if descent_rates else 0.0

    drift_at_apogee_ft = math.hypot(apogee[1], apogee[2])

    landing_point = trajectory[-1]
    landing_distance_ft = math.hypot(landing_point[1], landing_point[2])
    landing_bearing_deg = math.degrees(math.atan2(landing_point[1], landing_point[2])) % 360

    limitations = []
    rejected = [s.label for s in stations if s.id not in used_ids]
    if rejected:
        limitations.append(
            f"{', '.join(rejected)} disagreed enough with the others to be "
            "excluded from this estimate."
        )

    base.update(
        {
            "method": "multi_station",
            "best_altitude_ft": best_altitude_ft,
            "station_breakdown": breakdown,
            "stats": {
                "time_to_apogee_s": time_to_apogee_s,
                "flight_duration_s": flight_duration_s,
                "ascent_rate_max_ft_s": ascent_rate_max_ft_s,
                "descent_rate_max_ft_s": descent_rate_max_ft_s,
                "drift_at_apogee_ft": drift_at_apogee_ft,
                "landing_estimate": {
                    "distance_ft": landing_distance_ft,
                    "bearing_deg": landing_bearing_deg,
                },
            },
            "trajectory_3d": [{"t": (t - t0) / 1000, "x": x, "y": y, "z": z} for t, x, y, z in trajectory],
            "stations": [
                {
                    "label": station.label,
                    "x": float(station_ground_position(station)[0]),
                    "y": float(station_ground_position(station)[1]),
                    "used": station.id in used_ids,
                }
                for station in stations
            ],
            "limitations": limitations,
        }
    )
    return base


def build_session_comparison(session: Session) -> dict:
    """Aggregates build_debrief() across every launch in a session that has
    data - reuses its math entirely rather than recomputing anything, so
    there's exactly one place that understands how to turn Samples into
    stats/trajectories."""
    rows = []
    for launch in session.launches.order_by("number").all():
        debrief = build_debrief(launch)
        if debrief["method"] is None:
            continue

        stats = debrief["stats"] or {}
        landing = stats.get("landing_estimate") or {}

        if debrief["method"] == "multi_station":
            height_series = [{"t": p["t"], "height": p["z"]} for p in debrief["trajectory_3d"]]
        else:
            height_series = debrief["trajectory_2d"]

        rows.append(
            {
                "id": debrief["launch"]["id"],
                "number": debrief["launch"]["number"],
                "name": debrief["launch"]["name"],
                "method": debrief["method"],
                "best_altitude_ft": debrief["best_altitude_ft"],
                "time_to_apogee_s": stats.get("time_to_apogee_s"),
                "flight_duration_s": stats.get("flight_duration_s"),
                "ascent_rate_max_ft_s": stats.get("ascent_rate_max_ft_s"),
                "descent_rate_max_ft_s": stats.get("descent_rate_max_ft_s"),
                "drift_at_apogee_ft": stats.get("drift_at_apogee_ft"),
                "landing_distance_ft": landing.get("distance_ft"),
                "landing_bearing_deg": landing.get("bearing_deg"),
                "height_series": height_series,
            }
        )

    return {"launches": rows}
