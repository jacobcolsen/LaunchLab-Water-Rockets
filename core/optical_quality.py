"""Coverage/solve-quality summary for a flight, computed from
TriangulatedPoints (core.optical_triangulation), TrajectoryPoints
(core.optical_trajectory), and each contributing station's clock sync
(TrackingStation, Phase 5). Every number here is a straightforward
summary of quantities earlier phases already computed - no new math.
"""
from .optical_models import TrackingQualityMetrics, TrackingStation


def compute_quality_metrics_for_flight(flight):
    """(Re)computes the TrackingQualityMetrics row for a flight. Deletes
    any existing row and returns None if there's nothing solved yet -
    nothing meaningful to report before a TriangulatedPoint exists.
    Otherwise persists via update_or_create (flight is a OneToOneField,
    so re-running always updates the same row)."""
    triangulated_points = list(flight.triangulated_points.all())

    if not triangulated_points:
        TrackingQualityMetrics.objects.filter(flight=flight).delete()
        return None

    total = len(triangulated_points)
    three_plus = sum(1 for p in triangulated_points if p.stations_used_count >= 3)
    two_only = sum(1 for p in triangulated_points if p.stations_used_count == 2)
    pct_three_station = 100.0 * three_plus / total
    pct_two_station = 100.0 * two_only / total

    trajectory_points = list(flight.trajectory_points.all())
    pct_interpolated = (
        100.0 * sum(1 for p in trajectory_points if p.is_gap_filled) / len(trajectory_points)
        if trajectory_points
        else None
    )

    residuals = [p.residual_error_m for p in triangulated_points if p.residual_error_m is not None]
    mean_residual_m = sum(residuals) / len(residuals) if residuals else None
    max_residual_m = max(residuals) if residuals else None

    num_outliers_rejected = sum(len(p.rejected_stations) for p in triangulated_points)

    contributing_station_ids = {
        station_id for p in triangulated_points for station_id in p.stations_used
    }
    round_trips = [
        s.clock_round_trip_ms
        for s in TrackingStation.objects.filter(id__in=contributing_station_ids)
        if s.clock_round_trip_ms is not None
    ]
    sync_quality_score = sum(round_trips) / len(round_trips) if round_trips else None

    metrics, _ = TrackingQualityMetrics.objects.update_or_create(
        flight=flight,
        defaults={
            "pct_three_station": pct_three_station,
            "pct_two_station": pct_two_station,
            "pct_interpolated": pct_interpolated,
            "mean_residual_m": mean_residual_m,
            "max_residual_m": max_residual_m,
            "num_outliers_rejected": num_outliers_rejected,
            "sync_quality_score": sync_quality_score,
        },
    )
    return metrics
