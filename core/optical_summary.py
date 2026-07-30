"""Combines what earlier phases already computed into one flight summary
- the same "combine everything for one view" role core/debrief.py's
build_debrief() plays for the clinometer's own debrief modal. Nothing
new is computed here.
"""
from .optical_debrief import compute_derived_flight_data
from .optical_serializers import (
    FlightEventSerializer,
    TrackingQualityMetricsSerializer,
    TrajectoryPointSerializer,
)


def build_flight_summary(flight):
    stats = compute_derived_flight_data(flight)
    events = FlightEventSerializer(flight.flight_events.order_by("timestamp_ms"), many=True).data
    quality = (
        TrackingQualityMetricsSerializer(flight.quality_metrics).data
        if hasattr(flight, "quality_metrics")
        else None
    )
    trajectory = TrajectoryPointSerializer(
        flight.trajectory_points.order_by("timestamp_ms"), many=True
    ).data

    return {
        "flight_number": flight.number,
        "stats": stats,
        "events": events,
        "quality": quality,
        "trajectory": trajectory,
    }
