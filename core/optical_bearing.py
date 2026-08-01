"""Direction-only view for a flight, for when too few cameras were tracking
to triangulate a real 3D position (see core.optical_rays.generate_rays_for_flight,
which already requires 2+ qualifying stations and returns [] otherwise - a
single bearing ray has no unique 3D point, so there's nothing to "fill in"
there). This module answers a smaller, honest question instead: which way
was each camera actually pointed, over time - reusing the same per-station
bearing series (core.optical_rays.station_bearing_series) the real
triangulation path already computes, just converted to degrees a person can
read instead of a raw unit vector.
"""
import math

from .optical_rays import station_bearing_series


def _vector_to_az_el(direction):
    """direction: (east, north, up) unit vector. Returns (azimuth_deg,
    elevation_deg) - azimuth measured clockwise from north (matching
    optical_mock.py's aiming trig), elevation measured up from the
    horizon."""
    east, north, up = direction
    azimuth_deg = math.degrees(math.atan2(east, north)) % 360
    elevation_deg = math.degrees(math.asin(max(-1.0, min(1.0, up))))
    return azimuth_deg, elevation_deg


def compute_bearing_series_for_flight(flight):
    """Per-station time series of (azimuth, elevation) - one entry per
    station that has any usable pixel taps for this flight, regardless of
    how many total stations exist (works the same whether this is the
    flight's only data or a supplement alongside a real 3D solve)."""
    stations_out = []
    for station in flight.session.stations.all():
        series = station_bearing_series(station, flight)
        if not series:
            continue
        points = []
        for timestamp_ms, direction in series:
            azimuth_deg, elevation_deg = _vector_to_az_el(direction)
            points.append(
                {
                    "timestamp_ms": timestamp_ms,
                    "azimuth_deg": azimuth_deg,
                    "elevation_deg": elevation_deg,
                }
            )
        stations_out.append(
            {
                "station_id": station.id,
                "label": station.label,
                "series": points,
            }
        )

    return {"flight_number": flight.number, "stations": stations_out}
