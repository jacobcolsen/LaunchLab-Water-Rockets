from rest_framework import serializers

from .optical_models import (
    FlightEvent,
    FrameObservation,
    PixelObservation,
    StationCalibration,
    TrackingQualityMetrics,
    TrackingSession,
    TrackingStation,
    TrajectoryPoint,
    TriangulatedPoint,
)

SCHEMA_VERSION = "1.0"


class StationCalibrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationCalibration
        fields = [
            "id",
            "image_width_px",
            "image_height_px",
            "fov_horizontal_deg",
            "fov_vertical_deg",
            "facing_deg",
            "pitch_deg",
            "roll_deg",
            "orientation_source",
            "calibration_target_pixel_x",
            "calibration_target_pixel_y",
            "is_active",
            "created_at",
        ]


class TrackingStationSerializer(serializers.ModelSerializer):
    # device_token deliberately excluded - this is the public shape used by
    # the control page's station roster, which every joined phone can read.
    # Only the station's own create/position-update response (see
    # TrackingStationCreateResponseSerializer below) includes its token,
    # mirroring StationSerializer/StationCreateResponseSerializer's split
    # in core/serializers.py.
    calibrations = StationCalibrationSerializer(many=True, read_only=True)

    class Meta:
        model = TrackingStation
        fields = [
            "id",
            "label",
            "station_index",
            "position_source",
            "gps_latitude",
            "gps_longitude",
            "gps_altitude_m",
            "surveyed_x_m",
            "surveyed_y_m",
            "surveyed_z_m",
            "measured_height_m",
            "created_at",
            "calibrations",
        ]


POSITION_REQUIRED_FIELDS = {
    TrackingStation.SOURCE_GPS: ["gps_latitude", "gps_longitude"],
    TrackingStation.SOURCE_MANUAL: ["gps_latitude", "gps_longitude"],
    TrackingStation.SOURCE_SURVEYED: ["surveyed_x_m", "surveyed_y_m", "surveyed_z_m"],
    TrackingStation.SOURCE_MOCK: [],
}


class TrackingStationCreateResponseSerializer(TrackingStationSerializer):
    # station_index is always server-assigned (sequential, no hard-coded
    # station count) - never accepted from the client.
    class Meta(TrackingStationSerializer.Meta):
        fields = TrackingStationSerializer.Meta.fields + ["device_token"]
        read_only_fields = ["station_index"]

    def validate(self, data):
        position_source = data.get(
            "position_source",
            self.instance.position_source if self.instance else TrackingStation.SOURCE_MOCK,
        )
        required = POSITION_REQUIRED_FIELDS.get(position_source)
        if required is None:
            raise serializers.ValidationError(f"Unknown position_source '{position_source}'.")
        missing = [f for f in required if data.get(f) is None]
        if missing:
            raise serializers.ValidationError(
                f"Missing required field(s) for position_source '{position_source}': "
                f"{', '.join(missing)}."
            )
        return data


class PixelObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PixelObservation
        fields = [
            "id",
            "pixel_x",
            "pixel_y",
            "confidence",
            "observation_source",
            "valid",
            "rejection_reason",
            "is_current",
            "created_at",
        ]


class FrameObservationSerializer(serializers.ModelSerializer):
    pixel_observations = PixelObservationSerializer(many=True, read_only=True)

    class Meta:
        model = FrameObservation
        fields = [
            "id",
            "station",
            "frame_index",
            "local_timestamp_ms",
            "synchronized_timestamp_ms",
            "image_width_px",
            "image_height_px",
            "pixel_observations",
        ]


class TriangulatedPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = TriangulatedPoint
        fields = [
            "id",
            "synchronized_timestamp_ms",
            "x_m",
            "y_m",
            "z_m",
            "latitude",
            "longitude",
            "altitude_m",
            "stations_used",
            "stations_used_count",
            "rejected_stations",
            "residual_error_m",
            "geometry_quality",
            "confidence",
            "created_at",
        ]


class TrajectoryPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrajectoryPoint
        fields = [
            "id",
            "timestamp_ms",
            "source_point",
            "raw_x_m",
            "raw_y_m",
            "raw_z_m",
            "filtered_x_m",
            "filtered_y_m",
            "filtered_z_m",
            "is_interpolated",
            "is_gap_filled",
            "created_at",
        ]


class FlightEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = FlightEvent
        fields = [
            "id",
            "event_type",
            "timestamp_ms",
            "confidence",
            "detection_method",
            "is_manual_override",
            "notes",
            "created_at",
        ]


class TrackingQualityMetricsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingQualityMetrics
        fields = [
            "pct_three_station",
            "pct_two_station",
            "pct_interpolated",
            "mean_residual_m",
            "max_residual_m",
            "num_outliers_rejected",
            "sync_quality_score",
            "computed_at",
        ]


class TrackingSessionSerializer(serializers.ModelSerializer):
    stations = TrackingStationSerializer(many=True, read_only=True)

    class Meta:
        model = TrackingSession
        fields = ["id", "name", "created_at", "is_simulated", "notes", "stations"]


def export_tracking_session(session: TrackingSession) -> dict:
    """Full versioned dump of a tracking session and everything under it -
    the "versioned JSON schema" for this subsystem. Frame/pixel data nests
    under its station rather than a flat dump, since that's the natural
    per-station shape every later phase will want to consume."""
    session_data = TrackingSessionSerializer(session).data

    frames_by_station = {}
    for frame in (
        FrameObservation.objects.filter(session=session)
        .prefetch_related("pixel_observations")
        .order_by("station_id", "frame_index")
    ):
        frames_by_station.setdefault(frame.station_id, []).append(frame)

    for station_data in session_data["stations"]:
        station_data["frame_observations"] = FrameObservationSerializer(
            frames_by_station.get(station_data["id"], []), many=True
        ).data

    return {
        "schema_version": SCHEMA_VERSION,
        "session": session_data,
        "triangulated_points": TriangulatedPointSerializer(
            session.triangulated_points.all(), many=True
        ).data,
        "trajectory_points": TrajectoryPointSerializer(
            session.trajectory_points.all(), many=True
        ).data,
        "flight_events": FlightEventSerializer(session.flight_events.all(), many=True).data,
        "quality_metrics": (
            TrackingQualityMetricsSerializer(session.quality_metrics).data
            if hasattr(session, "quality_metrics")
            else None
        ),
    }
