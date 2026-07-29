"""Optical multi-camera rocket tracking - data model only (Phase 1).

Kept separate from the existing clinometer models in `models.py`: this is
a parallel, additive subsystem with its own units and coordinate
conventions. It's imported into `models.py` (see the bottom of that file)
purely so Django's app registry/migrations pick it up - nothing else
depends on that wiring.

Coordinate system: local East-North-Up (ENU), origin at the launch pad,
units in **meters** (unlike the clinometer system, which uses feet and a
distance+bearing convention - this subsystem explicitly requires SI units
internally).

Timestamps: `*_timestamp_ms` fields are millisecond Unix-epoch integers
(matching JS `Date.now()` for direct compatibility with a future browser
client). `local_timestamp_ms` is always the originating device's own
clock; `synchronized_timestamp_ms` is the cross-station-corrected time and
stays null until the Phase 5 synchronization step runs.

Confidence: every `confidence` field is a float on a 0.0-1.0 scale.

observation_source: how a PixelObservation's pixel coordinates were
produced - "manual" (user tapped the screen), "assisted" (tap + software
refinement), "automatic" (unassisted tracking), or "simulated" (synthetic
mock data).
"""
import secrets

from django.db import models


def generate_optical_device_token():
    return secrets.token_urlsafe(24)


class TrackingSession(models.Model):
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    is_simulated = models.BooleanField(
        default=False, help_text="True for mock/synthetic data, never a real flight."
    )
    notes = models.TextField(blank=True, default="")

    def __str__(self):
        return self.name


class TrackingStation(models.Model):
    SOURCE_GPS = "gps"
    SOURCE_MANUAL = "manual"
    SOURCE_SURVEYED = "surveyed_enu"
    SOURCE_MOCK = "mock"
    POSITION_SOURCE_CHOICES = [
        (SOURCE_GPS, "Phone GPS"),
        (SOURCE_MANUAL, "Manual latitude/longitude"),
        (SOURCE_SURVEYED, "Local surveyed ENU"),
        (SOURCE_MOCK, "Mock coordinates"),
    ]

    session = models.ForeignKey(TrackingSession, related_name="stations", on_delete=models.CASCADE)
    label = models.CharField(max_length=100)
    station_index = models.PositiveIntegerField(
        help_text="Display/ordering index - not a hard station-count limit."
    )
    device_token = models.CharField(max_length=64, unique=True, default=generate_optical_device_token)

    position_source = models.CharField(
        max_length=20, choices=POSITION_SOURCE_CHOICES, default=SOURCE_MOCK
    )
    gps_latitude = models.FloatField(null=True, blank=True)
    gps_longitude = models.FloatField(null=True, blank=True)
    gps_altitude_m = models.FloatField(null=True, blank=True)
    surveyed_x_m = models.FloatField(
        null=True, blank=True, help_text="Local ENU east offset from the launch pad, meters."
    )
    surveyed_y_m = models.FloatField(
        null=True, blank=True, help_text="Local ENU north offset from the launch pad, meters."
    )
    surveyed_z_m = models.FloatField(
        null=True, blank=True, help_text="Local ENU up offset from the launch pad, meters."
    )
    measured_height_m = models.FloatField(
        null=True, blank=True, help_text="Camera height above ground, meters."
    )

    clock_offset_ms = models.FloatField(
        null=True, blank=True, help_text="Add to this station's local_timestamp_ms to get server time."
    )
    clock_round_trip_ms = models.FloatField(
        null=True, blank=True, help_text="Round-trip time of the winning sync measurement - lower is better."
    )
    clock_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "station_index")
        ordering = ["session", "station_index"]

    def __str__(self):
        return f"{self.label} ({self.session.name})"


class StationCalibration(models.Model):
    ORIENTATION_SENSOR = "sensor"
    ORIENTATION_MANUAL = "manual"
    ORIENTATION_GEOMETRY_REFINED = "geometry_refined"
    ORIENTATION_SOURCE_CHOICES = [
        (ORIENTATION_SENSOR, "Device sensor"),
        (ORIENTATION_MANUAL, "Manual entry"),
        (ORIENTATION_GEOMETRY_REFINED, "Geometry-refined from a tap"),
    ]

    station = models.ForeignKey(TrackingStation, related_name="calibrations", on_delete=models.CASCADE)
    image_width_px = models.PositiveIntegerField()
    image_height_px = models.PositiveIntegerField()
    fov_horizontal_deg = models.FloatField()
    fov_vertical_deg = models.FloatField()
    facing_deg = models.FloatField(help_text="Compass bearing the camera boresight points toward.")
    pitch_deg = models.FloatField(help_text="Boresight elevation angle above the horizon; positive is up.")
    roll_deg = models.FloatField(default=0.0)
    orientation_source = models.CharField(
        max_length=20, choices=ORIENTATION_SOURCE_CHOICES, default=ORIENTATION_SENSOR
    )
    calibration_target_pixel_x = models.FloatField(null=True, blank=True)
    calibration_target_pixel_y = models.FloatField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Calibration: {self.station.label} ({self.created_at:%Y-%m-%d %H:%M})"


class TrackingFlight(models.Model):
    """One launch attempt within a TrackingSession's day-long setup. Frame
    numbering resets per flight (see FrameObservation) so stations don't
    need to re-register position/calibration between attempts."""

    session = models.ForeignKey(TrackingSession, related_name="flights", on_delete=models.CASCADE)
    number = models.PositiveIntegerField(blank=True, null=True)
    name = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session", "number")
        ordering = ["session", "number"]

    def save(self, *args, **kwargs):
        if self.number is None:
            self.number = self.session.flights.count() + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.session.name} — Flight {self.number}"


class FrameObservation(models.Model):
    session = models.ForeignKey(
        TrackingSession, related_name="frame_observations", on_delete=models.CASCADE
    )
    station = models.ForeignKey(
        TrackingStation, related_name="frame_observations", on_delete=models.CASCADE
    )
    flight = models.ForeignKey(
        TrackingFlight, related_name="frame_observations", on_delete=models.CASCADE
    )
    frame_index = models.PositiveIntegerField()
    local_timestamp_ms = models.BigIntegerField()
    synchronized_timestamp_ms = models.BigIntegerField(null=True, blank=True)
    image_width_px = models.PositiveIntegerField()
    image_height_px = models.PositiveIntegerField()

    class Meta:
        unique_together = ("station", "flight", "frame_index")
        ordering = ["station", "flight", "frame_index"]

    def __str__(self):
        return f"{self.station.label} flight {self.flight.number} frame {self.frame_index}"


class PixelObservation(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_ASSISTED = "assisted"
    SOURCE_AUTOMATIC = "automatic"
    SOURCE_SIMULATED = "simulated"
    OBSERVATION_SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_ASSISTED, "Assisted"),
        (SOURCE_AUTOMATIC, "Automatic"),
        (SOURCE_SIMULATED, "Simulated"),
    ]

    frame = models.ForeignKey(
        FrameObservation, related_name="pixel_observations", on_delete=models.CASCADE
    )
    pixel_x = models.FloatField()
    pixel_y = models.FloatField()
    confidence = models.FloatField(default=1.0)
    observation_source = models.CharField(max_length=20, choices=OBSERVATION_SOURCE_CHOICES)
    valid = models.BooleanField(default=True)
    rejection_reason = models.CharField(max_length=200, blank=True, default="")
    is_current = models.BooleanField(
        default=True,
        help_text="Only one current observation per frame; corrections add a new row "
        "instead of overwriting, so raw taps are never lost.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Pixel ({self.pixel_x:.0f}, {self.pixel_y:.0f}) @ {self.frame}"


class SynchronizedObservationSet(models.Model):
    session = models.ForeignKey(
        TrackingSession, related_name="synchronized_sets", on_delete=models.CASCADE
    )
    synchronized_timestamp_ms = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "synchronized_timestamp_ms"]

    def __str__(self):
        return f"Sync set @ {self.synchronized_timestamp_ms}"


class TriangulatedPoint(models.Model):
    session = models.ForeignKey(
        TrackingSession, related_name="triangulated_points", on_delete=models.CASCADE
    )
    synchronized_timestamp_ms = models.BigIntegerField()
    x_m = models.FloatField()
    y_m = models.FloatField()
    z_m = models.FloatField()
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    altitude_m = models.FloatField(null=True, blank=True)
    stations_used = models.JSONField(default=list, help_text="TrackingStation ids used in this solve.")
    stations_used_count = models.PositiveIntegerField(default=0)
    rejected_stations = models.JSONField(default=list)
    residual_error_m = models.FloatField(null=True, blank=True)
    geometry_quality = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "synchronized_timestamp_ms"]

    def __str__(self):
        return f"Point @ {self.synchronized_timestamp_ms}: ({self.x_m:.1f}, {self.y_m:.1f}, {self.z_m:.1f})"


class TrajectoryPoint(models.Model):
    session = models.ForeignKey(
        TrackingSession, related_name="trajectory_points", on_delete=models.CASCADE
    )
    timestamp_ms = models.BigIntegerField()
    source_point = models.ForeignKey(
        TriangulatedPoint,
        related_name="trajectory_points",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    raw_x_m = models.FloatField(null=True, blank=True)
    raw_y_m = models.FloatField(null=True, blank=True)
    raw_z_m = models.FloatField(null=True, blank=True)
    filtered_x_m = models.FloatField(null=True, blank=True)
    filtered_y_m = models.FloatField(null=True, blank=True)
    filtered_z_m = models.FloatField(null=True, blank=True)
    is_interpolated = models.BooleanField(default=False)
    is_gap_filled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "timestamp_ms"]

    def __str__(self):
        return f"Trajectory point @ {self.timestamp_ms}"


class FlightEvent(models.Model):
    EVENT_FIRST_MOTION = "first_motion"
    EVENT_LAUNCH = "launch"
    EVENT_RAIL_DEPARTURE = "rail_departure"
    EVENT_BURNOUT = "burnout"
    EVENT_APOGEE = "apogee"
    EVENT_LANDING = "landing"
    EVENT_TYPE_CHOICES = [
        (EVENT_FIRST_MOTION, "First motion"),
        (EVENT_LAUNCH, "Launch"),
        (EVENT_RAIL_DEPARTURE, "Rail departure"),
        (EVENT_BURNOUT, "Burnout"),
        (EVENT_APOGEE, "Apogee"),
        (EVENT_LANDING, "Landing"),
    ]

    session = models.ForeignKey(TrackingSession, related_name="flight_events", on_delete=models.CASCADE)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    timestamp_ms = models.BigIntegerField()
    confidence = models.FloatField(null=True, blank=True)
    detection_method = models.CharField(max_length=100, blank=True, default="")
    is_manual_override = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["session", "timestamp_ms"]

    def __str__(self):
        return f"{self.get_event_type_display()} @ {self.timestamp_ms}"


class TrackingQualityMetrics(models.Model):
    session = models.OneToOneField(
        TrackingSession, related_name="quality_metrics", on_delete=models.CASCADE
    )
    pct_three_station = models.FloatField(null=True, blank=True)
    pct_two_station = models.FloatField(null=True, blank=True)
    pct_interpolated = models.FloatField(null=True, blank=True)
    mean_residual_m = models.FloatField(null=True, blank=True)
    max_residual_m = models.FloatField(null=True, blank=True)
    num_outliers_rejected = models.PositiveIntegerField(default=0)
    sync_quality_score = models.FloatField(null=True, blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Quality metrics: {self.session.name}"
