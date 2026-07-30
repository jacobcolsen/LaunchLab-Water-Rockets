from django.contrib import admin

from .optical_models import (
    FlightEvent,
    FrameObservation,
    PixelObservation,
    StationCalibration,
    SynchronizedObservationSet,
    TrackingQualityMetrics,
    TrackingSession,
    TrackingStation,
    TrajectoryPoint,
    TriangulatedPoint,
)


class StationCalibrationInline(admin.TabularInline):
    model = StationCalibration
    extra = 0
    fields = (
        "image_width_px",
        "image_height_px",
        "fov_horizontal_deg",
        "fov_vertical_deg",
        "facing_deg",
        "pitch_deg",
        "roll_deg",
        "orientation_source",
        "is_active",
    )


class TrackingStationInline(admin.TabularInline):
    model = TrackingStation
    extra = 0
    fields = ("station_index", "label", "position_source", "device_token")
    readonly_fields = ("device_token",)


@admin.register(TrackingSession)
class TrackingSessionAdmin(admin.ModelAdmin):
    list_display = ("name", "is_simulated", "created_at")
    list_filter = ("is_simulated",)
    inlines = [TrackingStationInline]


@admin.register(TrackingStation)
class TrackingStationAdmin(admin.ModelAdmin):
    list_display = ("label", "session", "station_index", "position_source")
    list_filter = ("session",)
    inlines = [StationCalibrationInline]


@admin.register(StationCalibration)
class StationCalibrationAdmin(admin.ModelAdmin):
    list_display = ("station", "facing_deg", "pitch_deg", "is_active", "created_at")
    list_filter = ("station__session", "is_active")


class PixelObservationInline(admin.TabularInline):
    model = PixelObservation
    extra = 0
    fields = ("pixel_x", "pixel_y", "confidence", "observation_source", "valid", "is_current")


@admin.register(FrameObservation)
class FrameObservationAdmin(admin.ModelAdmin):
    list_display = ("station", "frame_index", "local_timestamp_ms", "synchronized_timestamp_ms")
    list_filter = ("station__session", "station")
    inlines = [PixelObservationInline]


@admin.register(PixelObservation)
class PixelObservationAdmin(admin.ModelAdmin):
    list_display = ("frame", "pixel_x", "pixel_y", "observation_source", "valid", "is_current")
    list_filter = ("observation_source", "valid", "is_current")


@admin.register(SynchronizedObservationSet)
class SynchronizedObservationSetAdmin(admin.ModelAdmin):
    list_display = ("session", "synchronized_timestamp_ms")
    list_filter = ("session",)


@admin.register(TriangulatedPoint)
class TriangulatedPointAdmin(admin.ModelAdmin):
    list_display = ("session", "synchronized_timestamp_ms", "x_m", "y_m", "z_m", "stations_used_count")
    list_filter = ("session",)


@admin.register(TrajectoryPoint)
class TrajectoryPointAdmin(admin.ModelAdmin):
    list_display = (
        "session",
        "timestamp_ms",
        "filtered_x_m",
        "filtered_y_m",
        "filtered_z_m",
        "is_interpolated",
    )
    list_filter = ("session", "is_interpolated", "is_gap_filled")


@admin.register(FlightEvent)
class FlightEventAdmin(admin.ModelAdmin):
    list_display = ("session", "event_type", "timestamp_ms", "confidence", "is_manual_override")
    list_filter = ("session", "event_type")


@admin.register(TrackingQualityMetrics)
class TrackingQualityMetricsAdmin(admin.ModelAdmin):
    list_display = ("flight", "pct_three_station", "mean_residual_m", "num_outliers_rejected")
