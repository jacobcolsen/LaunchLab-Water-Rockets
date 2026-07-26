import math

from .models import Launch, Result


def compute_result_for_launch(launch: Launch):
    samples = list(launch.samples.all())
    if len(samples) != 1:
        return None

    sample = samples[0]
    if not sample.data:
        return None

    peak_elevation = max(s["elevation"] for s in sample.data)
    station = sample.station
    height = max(0.0, station.distance_ft * math.tan(math.radians(peak_elevation)))

    result, _ = Result.objects.update_or_create(
        launch=launch,
        defaults={
            "best_altitude_ft": height,
            "method": Result.METHOD_SINGLE,
            "station_breakdown": {station.label: height},
        },
    )
    return result
