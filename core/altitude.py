import math

from .models import Launch, Result
from .triangulation import triangulate


def _single_station_estimate(station, sample_data):
    peak_elevation = max(s["elevation"] for s in sample_data)
    return max(0.0, station.distance_ft * math.tan(math.radians(peak_elevation)))


def compute_result_for_launch(launch: Launch):
    samples = [s for s in launch.samples.select_related("station").all() if s.data]
    if len(samples) == 0:
        return None

    if len(samples) == 1:
        sample = samples[0]
        station = sample.station
        height = _single_station_estimate(station, sample.data)
        result, _ = Result.objects.update_or_create(
            launch=launch,
            defaults={
                "best_altitude_ft": height,
                "method": Result.METHOD_SINGLE,
                "station_breakdown": {station.label: height},
            },
        )
        return result

    stations_samples = []
    single_estimates = {}
    for sample in samples:
        sorted_data = sorted(sample.data, key=lambda s: s["t"])
        if len(sorted_data) < 2:
            continue
        stations_samples.append((sample.station, sorted_data))
        single_estimates[sample.station.id] = _single_station_estimate(sample.station, sorted_data)

    if len(stations_samples) < 2:
        return None

    triangulated = triangulate(stations_samples)
    if triangulated is None:
        return None
    best_altitude_ft, used_ids, _trajectory = triangulated

    breakdown = {
        station.label: {
            "single_station_estimate_ft": single_estimates[station.id],
            "used": station.id in used_ids,
        }
        for station, _ in stations_samples
    }

    result, _ = Result.objects.update_or_create(
        launch=launch,
        defaults={
            "best_altitude_ft": best_altitude_ft,
            "method": Result.METHOD_MULTI,
            "station_breakdown": breakdown,
        },
    )
    return result
