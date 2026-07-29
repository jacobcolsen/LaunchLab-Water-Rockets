import math

from django.test import TestCase

from .optical_mock import (
    NUM_DROPPED_FRAMES_PER_STATION,
    STATION_CLOCK_OFFSETS_MS,
    camera_basis,
    flight_duration_s,
    generate_mock_tracking_session,
    project_to_pixel,
    rocket_trajectory,
)
from .optical_models import FrameObservation, PixelObservation, StationCalibration


def _calibration(facing_deg=0.0, pitch_deg=0.0, roll_deg=0.0, width=1920, height=1080, fov_h=60.0, fov_v=34.0):
    """An unsaved StationCalibration - plenty for pure geometry tests, no DB needed."""
    return StationCalibration(
        image_width_px=width,
        image_height_px=height,
        fov_horizontal_deg=fov_h,
        fov_vertical_deg=fov_v,
        facing_deg=facing_deg,
        pitch_deg=pitch_deg,
        roll_deg=roll_deg,
    )


class CameraProjectionTests(TestCase):
    def test_point_on_boresight_projects_to_center_pixel(self):
        calibration = _calibration(facing_deg=0.0, pitch_deg=0.0)
        # Facing north (0 deg) with no pitch - straight ahead is (0, 10, 0).
        pixel = project_to_pixel((0.0, 10.0, 0.0), (0.0, 0.0, 0.0), calibration)
        self.assertIsNotNone(pixel)
        pixel_x, pixel_y = pixel
        self.assertAlmostEqual(pixel_x, calibration.image_width_px / 2, places=6)
        self.assertAlmostEqual(pixel_y, calibration.image_height_px / 2, places=6)

    def test_point_on_boresight_with_pitch_and_facing_projects_to_center(self):
        calibration = _calibration(facing_deg=135.0, pitch_deg=20.0)
        right, up, forward = camera_basis(135.0, 20.0)
        far_point = tuple(forward[i] * 15.0 for i in range(3))
        pixel_x, pixel_y = project_to_pixel(far_point, (0.0, 0.0, 0.0), calibration)
        self.assertAlmostEqual(pixel_x, calibration.image_width_px / 2, places=6)
        self.assertAlmostEqual(pixel_y, calibration.image_height_px / 2, places=6)

    def test_point_behind_camera_returns_none(self):
        calibration = _calibration(facing_deg=0.0, pitch_deg=0.0)
        pixel = project_to_pixel((0.0, -10.0, 0.0), (0.0, 0.0, 0.0), calibration)
        self.assertIsNone(pixel)

    def test_camera_basis_is_orthonormal(self):
        right, up, forward = camera_basis(47.0, -12.0, roll_deg=8.0)
        for v in (right, up, forward):
            length = math.sqrt(sum(c * c for c in v))
            self.assertAlmostEqual(length, 1.0, places=9)
        self.assertAlmostEqual(sum(right[i] * up[i] for i in range(3)), 0.0, places=9)
        self.assertAlmostEqual(sum(right[i] * forward[i] for i in range(3)), 0.0, places=9)
        self.assertAlmostEqual(sum(up[i] * forward[i] for i in range(3)), 0.0, places=9)


class RocketTrajectoryTests(TestCase):
    def test_starts_at_launch_pad(self):
        x, y, z = rocket_trajectory(0.0)
        self.assertEqual((x, y, z), (0.0, 0.0, 0.0))

    def test_reaches_configured_apogee_height(self):
        apogee_height_m = 45.0
        v0 = math.sqrt(2 * 9.81 * apogee_height_m)
        t_apogee = v0 / 9.81
        _, _, z = rocket_trajectory(t_apogee, apogee_height_m=apogee_height_m)
        self.assertAlmostEqual(z, apogee_height_m, places=3)

    def test_lands_back_at_ground_level(self):
        duration = flight_duration_s()
        _, _, z = rocket_trajectory(duration)
        self.assertAlmostEqual(z, 0.0, places=3)


class MockTrackingSessionGeneratorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.session = generate_mock_tracking_session()

    def test_creates_three_stations_each_with_an_active_calibration(self):
        stations = list(self.session.stations.all())
        self.assertEqual(len(stations), 3)
        for station in stations:
            self.assertTrue(station.calibrations.filter(is_active=True).exists())

    def test_every_station_has_the_same_frame_count(self):
        counts = {
            station.label: FrameObservation.objects.filter(station=station).count()
            for station in self.session.stations.all()
        }
        self.assertEqual(len(set(counts.values())), 1, counts)
        self.assertGreater(next(iter(counts.values())), 0)

    def test_each_station_is_missing_exactly_the_configured_number_of_observations(self):
        for station in self.session.stations.all():
            frame_count = FrameObservation.objects.filter(station=station).count()
            pixel_count = PixelObservation.objects.filter(frame__station=station).count()
            self.assertEqual(frame_count - pixel_count, NUM_DROPPED_FRAMES_PER_STATION)

    def test_exactly_one_observation_is_badly_corrupted(self):
        corrupted = PixelObservation.objects.filter(confidence__lt=0.5)
        self.assertEqual(corrupted.count(), 1)

    def test_station_clock_offsets_are_applied_at_frame_zero(self):
        for station, expected_offset_ms in zip(self.session.stations.all(), STATION_CLOCK_OFFSETS_MS):
            frame = FrameObservation.objects.get(station=station, frame_index=0)
            self.assertEqual(frame.local_timestamp_ms, expected_offset_ms)

    def test_session_is_flagged_simulated(self):
        self.assertTrue(self.session.is_simulated)
