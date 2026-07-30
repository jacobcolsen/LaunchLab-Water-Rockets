import json
import math
import time

import numpy as np
from django.test import TestCase

from .optical_camera import (
    camera_basis,
    pixel_to_bearing_vector,
    project_to_pixel,
    solve_boresight_from_tap,
)
from .optical_mock import (
    FRAME_RATE_HZ,
    NUM_DROPPED_FRAMES_PER_STATION,
    STATION_CLOCK_OFFSETS_MS,
    flight_duration_s,
    generate_mock_tracking_session,
    rocket_trajectory,
)
from .optical_models import (
    FlightEvent,
    FrameObservation,
    PixelObservation,
    StationCalibration,
    TrackingFlight,
    TrackingQualityMetrics,
    TrackingSession,
    TrackingStation,
    TrajectoryPoint,
    TriangulatedPoint,
)
from .optical_debrief import compute_derived_flight_data
from .optical_events import detect_flight_events
from .optical_quality import compute_quality_metrics_for_flight
from .optical_rays import _interp_bearing_series, generate_rays_for_flight
from .optical_serializers import export_tracking_session
from .optical_summary import build_flight_summary
from .optical_trajectory import _moving_average, assemble_trajectory_for_flight
from .optical_triangulation import _residual, _solve_point, triangulate_flight
from .optical_validation import (
    compute_position_error,
    validate_flight_against_known_point,
    validate_pipeline_against_mock_flight,
)


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


def _normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    return tuple(c / length for c in v)


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


class PixelBearingRoundTripTests(TestCase):
    def test_pixel_to_bearing_recovers_original_direction(self):
        calibration = _calibration(facing_deg=30.0, pitch_deg=10.0, roll_deg=0.0)
        station_position = (5.0, -2.0, 0.0)
        for point in [(0.0, 20.0, 5.0), (10.0, 30.0, 15.0), (-8.0, 25.0, 2.0)]:
            expected_direction = _normalize(tuple(point[i] - station_position[i] for i in range(3)))
            pixel = project_to_pixel(point, station_position, calibration)
            self.assertIsNotNone(pixel)
            recovered_direction = pixel_to_bearing_vector(pixel[0], pixel[1], calibration)
            for a, b in zip(expected_direction, recovered_direction):
                self.assertAlmostEqual(a, b, places=6)

    def test_pixel_to_bearing_round_trips_with_roll(self):
        calibration = _calibration(facing_deg=200.0, pitch_deg=-15.0, roll_deg=12.0)
        station_position = (0.0, 0.0, 0.0)
        point = (-15.0, -40.0, 10.0)
        expected_direction = _normalize(tuple(point[i] - station_position[i] for i in range(3)))
        pixel = project_to_pixel(point, station_position, calibration)
        recovered_direction = pixel_to_bearing_vector(pixel[0], pixel[1], calibration)
        for a, b in zip(expected_direction, recovered_direction):
            self.assertAlmostEqual(a, b, places=6)


class SolveBoresightFromTapTests(TestCase):
    def test_recovers_the_calibration_that_generated_the_tap(self):
        # A tap of the pad, as it actually appears under some (possibly
        # compass-noisy) calibration, should let us recover that exact
        # calibration's facing/pitch from the station's known position
        # alone - this is the whole point of the "refine accuracy" step.
        station_position = (30.0, -10.0, 0.0)
        true_direction = tuple(-p for p in station_position)  # pad at the origin

        actual_facing_deg = 293.43
        actual_pitch_deg = -3.0
        calibration = _calibration(facing_deg=actual_facing_deg, pitch_deg=actual_pitch_deg)

        tap_pixel = project_to_pixel((0.0, 0.0, 0.0), station_position, calibration)
        self.assertIsNotNone(tap_pixel)

        solved_facing, solved_pitch = solve_boresight_from_tap(
            true_direction, tap_pixel[0], tap_pixel[1], calibration, pitch_hint_deg=actual_pitch_deg
        )
        self.assertAlmostEqual(solved_facing, actual_facing_deg, places=4)
        self.assertAlmostEqual(solved_pitch, actual_pitch_deg, places=4)

    def test_pitch_hint_disambiguates_the_correct_root(self):
        station_position = (0.0, 40.0, 0.0)
        true_direction = tuple(-p for p in station_position)
        calibration = _calibration(facing_deg=180.0, pitch_deg=25.0)

        tap_pixel = project_to_pixel((0.0, 0.0, 0.0), station_position, calibration)
        solved_facing, solved_pitch = solve_boresight_from_tap(
            true_direction, tap_pixel[0], tap_pixel[1], calibration, pitch_hint_deg=25.0
        )
        self.assertAlmostEqual(solved_facing, 180.0, places=4)
        self.assertAlmostEqual(solved_pitch, 25.0, places=4)


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


class OpticalStationApiTests(TestCase):
    """Phase 2: station setup + position entry endpoints."""

    def setUp(self):
        res = self.client.post(
            "/api/optical/sessions/",
            data=json.dumps({"name": "Test Session"}),
            content_type="application/json",
        )
        self.session_id = res.json()["id"]

    def _create_station(self, label, **fields):
        return self.client.post(
            f"/api/optical/sessions/{self.session_id}/stations/",
            data=json.dumps({"label": label, "position_source": "mock", **fields}),
            content_type="application/json",
        )

    def test_station_index_auto_increments_with_no_upper_bound(self):
        indices = [self._create_station(f"Station {i}").json()["station_index"] for i in range(5)]
        self.assertEqual(indices, [1, 2, 3, 4, 5])

    def test_create_response_includes_token_but_list_does_not(self):
        create_data = self._create_station("Station A").json()
        self.assertIn("device_token", create_data)

        list_res = self.client.get(f"/api/optical/sessions/{self.session_id}/stations/")
        self.assertEqual(list_res.status_code, 200)
        for station in list_res.json():
            self.assertNotIn("device_token", station)

    def test_manual_source_requires_latitude_and_longitude(self):
        res = self._create_station("Station B", position_source="manual")
        self.assertEqual(res.status_code, 400)

    def test_surveyed_source_requires_xyz(self):
        res = self._create_station("Station C", position_source="surveyed_enu", surveyed_x_m=1.0)
        self.assertEqual(res.status_code, 400)

    def test_position_endpoint_updates_the_right_station_by_token(self):
        token = self._create_station("Station D").json()["device_token"]

        res = self.client.post(
            "/api/optical/stations/position/",
            data=json.dumps(
                {
                    "device_token": token,
                    "position_source": "surveyed_enu",
                    "surveyed_x_m": 12.0,
                    "surveyed_y_m": -5.0,
                    "surveyed_z_m": 0.0,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertEqual(data["position_source"], "surveyed_enu")
        self.assertEqual(data["surveyed_x_m"], 12.0)

    def test_position_endpoint_requires_device_token(self):
        res = self.client.post(
            "/api/optical/stations/position/",
            data=json.dumps({"position_source": "mock"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)


class OpticalCalibrationApiTests(TestCase):
    """Phase 3: camera calibration + geometry-based refinement endpoints."""

    def setUp(self):
        session_res = self.client.post(
            "/api/optical/sessions/",
            data=json.dumps({"name": "Calibration Test Session"}),
            content_type="application/json",
        )
        self.session_id = session_res.json()["id"]

    def _create_station(self, label, **fields):
        res = self.client.post(
            f"/api/optical/sessions/{self.session_id}/stations/",
            data=json.dumps({"label": label, "position_source": "mock", **fields}),
            content_type="application/json",
        )
        return res.json()

    def _post_calibration(self, token, **overrides):
        payload = {
            "device_token": token,
            "image_width_px": 1920,
            "image_height_px": 1080,
            "fov_horizontal_deg": 60.0,
            "fov_vertical_deg": 34.0,
            "facing_deg": 10.0,
            "pitch_deg": 5.0,
            "roll_deg": 0.0,
            "orientation_source": "sensor",
        }
        payload.update(overrides)
        return self.client.post(
            "/api/optical/stations/calibration/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_calibration_post_creates_active_row_and_deactivates_previous(self):
        token = self._create_station("Station A")["device_token"]

        first = self._post_calibration(token)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertTrue(first.json()["is_active"])

        second = self._post_calibration(token, facing_deg=20.0)
        self.assertEqual(second.status_code, 201, second.content)

        station = TrackingStation.objects.get(device_token=token)
        active = list(station.calibrations.filter(is_active=True))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].facing_deg, 20.0)
        self.assertEqual(station.calibrations.filter(is_active=False).count(), 1)

    def test_refine_updates_facing_pitch_for_mock_station(self):
        station_data = self._create_station(
            "Station B", surveyed_x_m=30.0, surveyed_y_m=-10.0, surveyed_z_m=0.0
        )
        token = station_data["device_token"]

        actual_facing_deg, actual_pitch_deg = 293.43, -3.0
        self._post_calibration(token, facing_deg=actual_facing_deg, pitch_deg=actual_pitch_deg)

        calibration = _calibration(facing_deg=actual_facing_deg, pitch_deg=actual_pitch_deg)
        tap_pixel = project_to_pixel((0.0, 0.0, 0.0), (30.0, -10.0, 0.0), calibration)

        res = self.client.post(
            "/api/optical/stations/calibration/refine/",
            data=json.dumps(
                {"device_token": token, "tap_pixel_x": tap_pixel[0], "tap_pixel_y": tap_pixel[1]}
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertEqual(data["orientation_source"], "geometry_refined")
        self.assertAlmostEqual(data["facing_deg"], actual_facing_deg, places=3)
        self.assertAlmostEqual(data["pitch_deg"], actual_pitch_deg, places=3)

    def test_refine_rejects_gps_source_station(self):
        station_data = self._create_station(
            "Station C", position_source="gps", gps_latitude=40.0, gps_longitude=-111.0
        )
        token = station_data["device_token"]
        self._post_calibration(token)

        res = self.client.post(
            "/api/optical/stations/calibration/refine/",
            data=json.dumps({"device_token": token, "tap_pixel_x": 960, "tap_pixel_y": 540}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)


class OpticalFlightAndObservationApiTests(TestCase):
    """Phase 4: flight numbering + manual observation upload."""

    def setUp(self):
        session_res = self.client.post(
            "/api/optical/sessions/",
            data=json.dumps({"name": "Flight Test Session"}),
            content_type="application/json",
        )
        self.session_id = session_res.json()["id"]
        station_res = self.client.post(
            f"/api/optical/sessions/{self.session_id}/stations/",
            data=json.dumps({"label": "Station A", "position_source": "mock"}),
            content_type="application/json",
        )
        self.token = station_res.json()["device_token"]

    def _create_flight(self):
        res = self.client.post(
            f"/api/optical/sessions/{self.session_id}/flights/",
            data=json.dumps({}),
            content_type="application/json",
        )
        return res.json()

    def _upload(self, flight_number, observations, **overrides):
        payload = {
            "device_token": self.token,
            "flight_number": flight_number,
            "image_width_px": 1920,
            "image_height_px": 1080,
            "observations": observations,
        }
        payload.update(overrides)
        return self.client.post(
            "/api/optical/stations/observations/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_flight_numbers_auto_increment(self):
        numbers = [self._create_flight()["number"] for _ in range(3)]
        self.assertEqual(numbers, [1, 2, 3])

    def test_upload_creates_frames_and_pixels_with_skips(self):
        flight = self._create_flight()
        observations = [
            {"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": 100.0, "pixel_y": 200.0},
            {"frame_index": 1, "local_timestamp_ms": 33, "pixel_x": None, "pixel_y": None},
            {"frame_index": 2, "local_timestamp_ms": 66, "pixel_x": 110.0, "pixel_y": 210.0},
        ]
        res = self._upload(flight["number"], observations)
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertEqual(data["frames"], 3)
        self.assertEqual(data["pixels"], 2)

        station = TrackingStation.objects.get(device_token=self.token)
        self.assertEqual(FrameObservation.objects.filter(station=station).count(), 3)
        self.assertEqual(PixelObservation.objects.filter(frame__station=station).count(), 2)
        skipped_frame = FrameObservation.objects.get(station=station, frame_index=1)
        self.assertEqual(skipped_frame.pixel_observations.count(), 0)

    def test_retagging_same_frame_preserves_history(self):
        flight = self._create_flight()
        self._upload(
            flight["number"],
            [{"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": 100.0, "pixel_y": 200.0}],
        )
        self._upload(
            flight["number"],
            [{"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": 105.0, "pixel_y": 205.0}],
        )

        station = TrackingStation.objects.get(device_token=self.token)
        frame = FrameObservation.objects.get(station=station, frame_index=0)
        self.assertEqual(frame.pixel_observations.count(), 2)
        current = frame.pixel_observations.filter(is_current=True)
        self.assertEqual(current.count(), 1)
        self.assertEqual(current.first().pixel_x, 105.0)

    def test_frame_index_does_not_collide_across_flights(self):
        flight1 = self._create_flight()
        flight2 = self._create_flight()
        for flight in (flight1, flight2):
            res = self._upload(
                flight["number"],
                [{"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": 50.0, "pixel_y": 60.0}],
            )
            self.assertEqual(res.status_code, 201, res.content)

        station = TrackingStation.objects.get(device_token=self.token)
        self.assertEqual(FrameObservation.objects.filter(station=station, frame_index=0).count(), 2)
        self.assertEqual(TrackingFlight.objects.filter(session_id=self.session_id).count(), 2)

    def test_manual_observation_bearing_matches_generating_direction(self):
        # Integration: tag a pixel corresponding to a known world direction
        # under the station's active calibration, then confirm Phase 3's
        # pixel_to_bearing_vector recovers that same direction from a real
        # (non-mock) PixelObservation row.
        self.client.post(
            "/api/optical/stations/calibration/",
            data=json.dumps(
                {
                    "device_token": self.token,
                    "image_width_px": 1920,
                    "image_height_px": 1080,
                    "fov_horizontal_deg": 60.0,
                    "fov_vertical_deg": 34.0,
                    "facing_deg": 45.0,
                    "pitch_deg": 10.0,
                    "roll_deg": 0.0,
                    "orientation_source": "sensor",
                }
            ),
            content_type="application/json",
        )
        calibration = _calibration(facing_deg=45.0, pitch_deg=10.0)
        world_point = (20.0, 30.0, 15.0)
        pixel_x, pixel_y = project_to_pixel(world_point, (0.0, 0.0, 0.0), calibration)

        flight = self._create_flight()
        self._upload(
            flight["number"],
            [{"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": pixel_x, "pixel_y": pixel_y}],
        )

        station = TrackingStation.objects.get(device_token=self.token)
        pixel_obs = PixelObservation.objects.get(frame__station=station, frame__frame_index=0)
        active_calibration = station.calibrations.get(is_active=True)
        recovered_direction = pixel_to_bearing_vector(
            pixel_obs.pixel_x, pixel_obs.pixel_y, active_calibration
        )
        expected_direction = _normalize(world_point)
        for a, b in zip(expected_direction, recovered_direction):
            self.assertAlmostEqual(a, b, places=4)


class ObservationSourceUploadTests(TestCase):
    """Phase 12: assisted-tracking observations carry their own source."""

    def setUp(self):
        session_res = self.client.post(
            "/api/optical/sessions/",
            data=json.dumps({"name": "Observation Source Test Session"}),
            content_type="application/json",
        )
        session_id = session_res.json()["id"]
        station_res = self.client.post(
            f"/api/optical/sessions/{session_id}/stations/",
            data=json.dumps({"label": "Station A", "position_source": "mock"}),
            content_type="application/json",
        )
        self.token = station_res.json()["device_token"]
        flight_res = self.client.post(
            f"/api/optical/sessions/{session_id}/flights/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.flight_number = flight_res.json()["number"]

    def _upload_one(self, frame_index, **entry_overrides):
        entry = {
            "frame_index": frame_index,
            "local_timestamp_ms": frame_index * 33,
            "pixel_x": 100.0,
            "pixel_y": 200.0,
        }
        entry.update(entry_overrides)
        return self.client.post(
            "/api/optical/stations/observations/",
            data=json.dumps(
                {
                    "device_token": self.token,
                    "flight_number": self.flight_number,
                    "image_width_px": 1920,
                    "image_height_px": 1080,
                    "observations": [entry],
                }
            ),
            content_type="application/json",
        )

    def test_assisted_source_is_persisted(self):
        res = self._upload_one(0, observation_source="assisted")
        self.assertEqual(res.status_code, 201, res.content)
        obs = PixelObservation.objects.get(frame__frame_index=0)
        self.assertEqual(obs.observation_source, "assisted")

    def test_automatic_source_is_persisted(self):
        # Phase 13: live tracking uploads observation_source="automatic" -
        # the last of PixelObservation's four documented sources, closing
        # out the full set explicitly rather than leaving it assumed.
        res = self._upload_one(0, observation_source="automatic")
        self.assertEqual(res.status_code, 201, res.content)
        obs = PixelObservation.objects.get(frame__frame_index=0)
        self.assertEqual(obs.observation_source, "automatic")

    def test_missing_source_defaults_to_manual(self):
        res = self._upload_one(0)
        self.assertEqual(res.status_code, 201, res.content)
        obs = PixelObservation.objects.get(frame__frame_index=0)
        self.assertEqual(obs.observation_source, "manual")

    def test_invalid_source_falls_back_to_manual(self):
        res = self._upload_one(0, observation_source="not_a_real_source")
        self.assertEqual(res.status_code, 201, res.content)
        obs = PixelObservation.objects.get(frame__frame_index=0)
        self.assertEqual(obs.observation_source, "manual")


class OpticalClockSyncApiTests(TestCase):
    """Phase 5: server-time echo + per-station clock offset sync."""

    def setUp(self):
        session_res = self.client.post(
            "/api/optical/sessions/",
            data=json.dumps({"name": "Clock Sync Test Session"}),
            content_type="application/json",
        )
        self.session_id = session_res.json()["id"]
        station_res = self.client.post(
            f"/api/optical/sessions/{self.session_id}/stations/",
            data=json.dumps({"label": "Station A", "position_source": "mock"}),
            content_type="application/json",
        )
        self.token = station_res.json()["device_token"]

    def _create_flight(self):
        res = self.client.post(
            f"/api/optical/sessions/{self.session_id}/flights/",
            data=json.dumps({}),
            content_type="application/json",
        )
        return res.json()

    def _sync(self, offset_ms, round_trip_ms=25.0):
        return self.client.post(
            "/api/optical/stations/clock-sync/",
            data=json.dumps(
                {"device_token": self.token, "offset_ms": offset_ms, "round_trip_ms": round_trip_ms}
            ),
            content_type="application/json",
        )

    def test_server_time_returns_a_plausible_timestamp(self):
        before_ms = int(time.time() * 1000)
        res = self.client.get("/api/optical/server-time/")
        after_ms = int(time.time() * 1000)
        self.assertEqual(res.status_code, 200)
        server_time_ms = res.json()["server_time_ms"]
        self.assertGreaterEqual(server_time_ms, before_ms - 1000)
        self.assertLessEqual(server_time_ms, after_ms + 1000)

    def test_clock_sync_updates_station_fields(self):
        res = self._sync(offset_ms=123.4, round_trip_ms=18.0)
        self.assertEqual(res.status_code, 200, res.content)
        station = TrackingStation.objects.get(device_token=self.token)
        self.assertAlmostEqual(station.clock_offset_ms, 123.4, places=3)
        self.assertAlmostEqual(station.clock_round_trip_ms, 18.0, places=3)
        self.assertIsNotNone(station.clock_synced_at)

    def test_upload_after_sync_populates_synchronized_timestamp(self):
        self._sync(offset_ms=500.0)
        flight = self._create_flight()
        self.client.post(
            "/api/optical/stations/observations/",
            data=json.dumps(
                {
                    "device_token": self.token,
                    "flight_number": flight["number"],
                    "image_width_px": 1920,
                    "image_height_px": 1080,
                    "observations": [
                        {"frame_index": 0, "local_timestamp_ms": 1000, "pixel_x": 10.0, "pixel_y": 20.0}
                    ],
                }
            ),
            content_type="application/json",
        )
        station = TrackingStation.objects.get(device_token=self.token)
        frame = FrameObservation.objects.get(station=station, frame_index=0)
        self.assertEqual(frame.synchronized_timestamp_ms, 1500)

    def test_sync_backfills_previously_unsynced_frames(self):
        flight = self._create_flight()
        self.client.post(
            "/api/optical/stations/observations/",
            data=json.dumps(
                {
                    "device_token": self.token,
                    "flight_number": flight["number"],
                    "image_width_px": 1920,
                    "image_height_px": 1080,
                    "observations": [
                        {"frame_index": 0, "local_timestamp_ms": 1000, "pixel_x": 10.0, "pixel_y": 20.0},
                        {"frame_index": 1, "local_timestamp_ms": 1033, "pixel_x": None, "pixel_y": None},
                    ],
                }
            ),
            content_type="application/json",
        )
        station = TrackingStation.objects.get(device_token=self.token)
        self.assertIsNone(
            FrameObservation.objects.get(station=station, frame_index=0).synchronized_timestamp_ms
        )

        res = self._sync(offset_ms=250.0)
        self.assertEqual(res.json()["backfilled_frames"], 2)

        frame0 = FrameObservation.objects.get(station=station, frame_index=0)
        frame1 = FrameObservation.objects.get(station=station, frame_index=1)
        self.assertEqual(frame0.synchronized_timestamp_ms, 1250)
        self.assertEqual(frame1.synchronized_timestamp_ms, 1283)


def _facing_pitch_toward(station_position, target_position):
    """facing_deg/pitch_deg that aims a station's boresight exactly at
    target_position - same derivation optical_mock.py uses to aim its
    synthetic stations at the pad."""
    dx = target_position[0] - station_position[0]
    dy = target_position[1] - station_position[1]
    dz = target_position[2] - station_position[2]
    facing_deg = math.degrees(math.atan2(dx, dy)) % 360
    pitch_deg = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return facing_deg, pitch_deg


def _make_station_aimed_at(session, label, index, position, aim_point):
    station = TrackingStation.objects.create(
        session=session,
        label=label,
        station_index=index,
        position_source=TrackingStation.SOURCE_MOCK,
        surveyed_x_m=position[0],
        surveyed_y_m=position[1],
        surveyed_z_m=position[2],
    )
    facing_deg, pitch_deg = _facing_pitch_toward(position, aim_point)
    StationCalibration.objects.create(
        station=station,
        image_width_px=1920,
        image_height_px=1080,
        fov_horizontal_deg=60.0,
        fov_vertical_deg=34.0,
        facing_deg=facing_deg,
        pitch_deg=pitch_deg,
        roll_deg=0.0,
        orientation_source=StationCalibration.ORIENTATION_SENSOR,
        is_active=True,
    )
    return station


def _make_tracking_station(session, label, index, x, y, z):
    station = TrackingStation.objects.create(
        session=session,
        label=label,
        station_index=index,
        position_source=TrackingStation.SOURCE_MOCK,
        surveyed_x_m=x,
        surveyed_y_m=y,
        surveyed_z_m=z,
    )
    StationCalibration.objects.create(
        station=station,
        image_width_px=1920,
        image_height_px=1080,
        fov_horizontal_deg=60.0,
        fov_vertical_deg=34.0,
        facing_deg=0.0,
        pitch_deg=0.0,
        roll_deg=0.0,
        orientation_source=StationCalibration.ORIENTATION_SENSOR,
        is_active=True,
    )
    return station


def _add_pixel_observation(
    session,
    station,
    flight,
    frame_index,
    synchronized_timestamp_ms,
    pixel_x=960.0,
    pixel_y=540.0,
    local_timestamp_ms=None,
):
    frame = FrameObservation.objects.create(
        session=session,
        station=station,
        flight=flight,
        frame_index=frame_index,
        local_timestamp_ms=local_timestamp_ms if local_timestamp_ms is not None else (synchronized_timestamp_ms or 0),
        synchronized_timestamp_ms=synchronized_timestamp_ms,
        image_width_px=1920,
        image_height_px=1080,
    )
    PixelObservation.objects.create(
        frame=frame,
        pixel_x=pixel_x,
        pixel_y=pixel_y,
        observation_source=PixelObservation.SOURCE_MANUAL,
        valid=True,
        is_current=True,
    )
    return frame


class InterpBearingSeriesTests(TestCase):
    def test_interpolates_between_two_bearings_and_bounds_correctly(self):
        series = [
            (0, np.array([0.0, 1.0, 0.0])),
            (1000, np.array([1.0, 0.0, 0.0])),
        ]
        grid_times = [-100, 0, 500, 1000, 1500]
        results = _interp_bearing_series(series, grid_times)

        self.assertIsNone(results[0])
        self.assertIsNone(results[4])
        for a, b in zip(results[1], [0.0, 1.0, 0.0]):
            self.assertAlmostEqual(a, b, places=6)
        for a, b in zip(results[3], [1.0, 0.0, 0.0]):
            self.assertAlmostEqual(a, b, places=6)

        expected_mid = np.array([0.5, 0.5, 0.0])
        expected_mid = expected_mid / np.linalg.norm(expected_mid)
        for a, b in zip(results[2], expected_mid):
            self.assertAlmostEqual(a, b, places=6)

    def test_fewer_than_two_points_returns_all_none(self):
        results = _interp_bearing_series([(0, np.array([0.0, 1.0, 0.0]))], [0, 100])
        self.assertEqual(results, [None, None])


class GenerateRaysForFlightTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Ray Gen Test")
        self.flight = TrackingFlight.objects.create(session=self.session)

    def test_stations_with_fewer_than_two_observations_are_excluded(self):
        station_one_obs = _make_tracking_station(self.session, "OneObs", 1, 10.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_one_obs, self.flight, 0, 0)

        station_a = _make_tracking_station(self.session, "A", 2, 30.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_a, self.flight, 0, 0)
        _add_pixel_observation(self.session, station_a, self.flight, 1, 1000)

        station_b = _make_tracking_station(self.session, "B", 3, -30.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_b, self.flight, 0, 0)
        _add_pixel_observation(self.session, station_b, self.flight, 1, 1000)

        entries = generate_rays_for_flight(self.flight)
        self.assertGreater(len(entries), 0)
        participating_labels = {station.label for entry in entries for station, _, _ in entry["rays"]}
        self.assertNotIn("OneObs", participating_labels)
        self.assertIn("A", participating_labels)
        self.assertIn("B", participating_labels)

    def test_station_with_an_unsynced_observation_does_not_participate(self):
        station_a = _make_tracking_station(self.session, "A", 1, 30.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_a, self.flight, 0, 0)
        _add_pixel_observation(self.session, station_a, self.flight, 1, 1000)

        station_unsynced = _make_tracking_station(self.session, "Unsynced", 2, -30.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_unsynced, self.flight, 0, 0)
        _add_pixel_observation(
            self.session, station_unsynced, self.flight, 1, None, local_timestamp_ms=1000
        )

        # Only station A has 2+ *synchronized* observations - Unsynced's
        # second tap has no synchronized_timestamp_ms, so it only
        # qualifies with 1, and 2+ qualifying stations are required.
        entries = generate_rays_for_flight(self.flight)
        self.assertEqual(entries, [])

    def test_grid_range_is_the_overlap_of_qualifying_stations(self):
        station_a = _make_tracking_station(self.session, "A", 1, 30.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_a, self.flight, 0, 0)
        _add_pixel_observation(self.session, station_a, self.flight, 1, 2000)

        station_b = _make_tracking_station(self.session, "B", 2, -30.0, 0.0, 0.0)
        _add_pixel_observation(self.session, station_b, self.flight, 0, 500)
        _add_pixel_observation(self.session, station_b, self.flight, 1, 1500)

        entries = generate_rays_for_flight(self.flight)
        self.assertEqual(entries[0]["synchronized_timestamp_ms"], 500)
        self.assertEqual(entries[-1]["synchronized_timestamp_ms"], 1500)


class GenerateRaysForMockFlightTests(TestCase):
    def test_generates_nonempty_grid_with_plausible_ray_directions(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        entries = generate_rays_for_flight(flight)
        self.assertGreater(len(entries), 0)

        # Pick an entry well away from the mock generator's deliberately
        # corrupted frame (which sits at the flight's temporal midpoint)
        # so this checks the interpolation/geometry math, not the known
        # outlier fixture.
        quarter_point = entries[len(entries) // 4]
        self.assertGreaterEqual(len(quarter_point["rays"]), 2)

        t_seconds = quarter_point["synchronized_timestamp_ms"] / 1000
        true_point = np.array(rocket_trajectory(t_seconds))

        for station, origin, direction in quarter_point["rays"]:
            expected_direction = true_point - origin
            expected_direction = expected_direction / np.linalg.norm(expected_direction)
            cos_angle = np.clip(np.dot(direction, expected_direction), -1.0, 1.0)
            angle_deg = math.degrees(math.acos(cos_angle))
            self.assertLess(angle_deg, 2.0, station.label)


class SolvePointAndResidualTests(TestCase):
    def test_two_rays_crossing_at_a_known_point_solve_exactly(self):
        point = np.array([5.0, 5.0, 0.0])
        origin_a = np.array([0.0, 0.0, 0.0])
        direction_a = (point - origin_a) / np.linalg.norm(point - origin_a)
        origin_b = np.array([10.0, 0.0, 0.0])
        direction_b = (point - origin_b) / np.linalg.norm(point - origin_b)

        solved = _solve_point([(origin_a, direction_a), (origin_b, direction_b)])
        for a, b in zip(solved, point):
            self.assertAlmostEqual(a, b, places=6)

    def test_residual_is_zero_on_the_line_and_nonzero_off_it(self):
        origin = np.array([0.0, 0.0, 0.0])
        direction = np.array([0.0, 1.0, 0.0])
        on_line_point = np.array([0.0, 7.0, 0.0])
        off_line_point = np.array([3.0, 7.0, 0.0])

        self.assertAlmostEqual(_residual(on_line_point, origin, direction), 0.0, places=6)
        self.assertGreater(_residual(off_line_point, origin, direction), 2.9)


class TriangulateFlightOutlierTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Triangulation Outlier Test")
        self.flight = TrackingFlight.objects.create(session=self.session)

    def test_a_clear_outlier_station_is_rejected_and_point_stays_accurate(self):
        # Uses 4 stations (3 good + 1 grossly mistapped), not 3: with only
        # 3 rays total, a single bad one pulls the shared least-squares
        # fit toward it enough that its own residual often isn't clearly
        # worse than the *other two* stations' (now also-inflated)
        # residuals - the worst-vs-second-worst heuristic needs a stable
        # "healthy majority" to compare against, which 2-good-vs-1-bad
        # doesn't reliably provide. 3-good-vs-1-bad does. This mirrors the
        # motivation for "avoid hard-coding against future expansion" -
        # more stations make outlier rejection meaningfully more reliable,
        # not just more redundant.
        positions = {
            "A": (30.0, 0.0, 0.0),
            "B": (-30.0, 0.0, 0.0),
            "C": (0.0, 30.0, 0.0),
            "D": (0.0, -30.0, 0.0),
        }
        point_t0 = (0.0, 0.0, 20.0)
        point_t1 = (0.0, 0.0, 25.0)

        stations = {
            label: _make_station_aimed_at(self.session, label, i + 1, pos, point_t0)
            for i, (label, pos) in enumerate(positions.items())
        }

        for frame_index, (t_ms, point) in enumerate([(0, point_t0), (1000, point_t1)]):
            for label, station in stations.items():
                position = positions[label]
                calibration = station.calibrations.get(is_active=True)
                if label == "D":
                    # A gross mistap - roughly a wrong corner of frame,
                    # not a proportional offset from the true target.
                    pixel_x, pixel_y = 1800.0, 950.0
                else:
                    pixel_x, pixel_y = project_to_pixel(point, position, calibration)
                _add_pixel_observation(
                    self.session, station, self.flight, frame_index, t_ms, pixel_x=pixel_x, pixel_y=pixel_y
                )

        created = triangulate_flight(self.flight)
        self.assertGreater(len(created), 0)
        for tp in created:
            self.assertIn(stations["D"].id, tp.rejected_stations)
            for label in ("A", "B", "C"):
                self.assertIn(stations[label].id, tp.stations_used)

            frac = tp.synchronized_timestamp_ms / 1000.0
            expected_z = 20.0 + 5.0 * frac
            self.assertAlmostEqual(tp.z_m, expected_z, delta=0.5)

    def test_rerunning_does_not_duplicate_rows(self):
        position_a = (30.0, 0.0, 0.0)
        position_b = (-30.0, 0.0, 0.0)
        point = (0.0, 0.0, 20.0)
        station_a = _make_station_aimed_at(self.session, "A", 1, position_a, point)
        station_b = _make_station_aimed_at(self.session, "B", 2, position_b, point)

        for frame_index, t_ms in enumerate([0, 1000]):
            for station, position in [(station_a, position_a), (station_b, position_b)]:
                calibration = station.calibrations.get(is_active=True)
                pixel_x, pixel_y = project_to_pixel(point, position, calibration)
                _add_pixel_observation(
                    self.session, station, self.flight, frame_index, t_ms, pixel_x=pixel_x, pixel_y=pixel_y
                )

        first_count = len(triangulate_flight(self.flight))
        second_count = len(triangulate_flight(self.flight))
        self.assertEqual(first_count, second_count)
        self.assertEqual(TriangulatedPoint.objects.filter(flight=self.flight).count(), second_count)


class TriangulateMockFlightTests(TestCase):
    def test_corrupted_frame_shows_elevated_residual_but_stays_bounded(self):
        # The mock flight has only 3 stations, and its one deliberately
        # corrupted observation is a moderate pixel offset, not a gross
        # mistap - confirmed (via the outlier test above, which uses 4
        # stations) that the worst-vs-second-worst heuristic needs a
        # stable "healthy majority" to reliably reject against, which
        # 2-good-vs-1-bad doesn't provide as cleanly as 3-good-vs-1-bad.
        # So this checks what *should* honestly be true at N=3: the
        # corrupted frame's residual is clearly elevated versus its
        # clean neighbors, and the resulting point - while pulled off
        # slightly - stays roughly in the right place, not wildly wrong.
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        created = triangulate_flight(flight)
        self.assertGreater(len(created), 0)

        frame_dt = 1.0 / FRAME_RATE_HZ
        num_frames = int(flight_duration_s() / frame_dt) + 1
        bad_time_ms = (num_frames // 2) * frame_dt * 1000

        sorted_created = sorted(created, key=lambda tp: tp.synchronized_timestamp_ms)
        nearest = min(sorted_created, key=lambda tp: abs(tp.synchronized_timestamp_ms - bad_time_ms))
        idx = sorted_created.index(nearest)
        neighbor = sorted_created[idx - 4]

        self.assertGreater(nearest.residual_error_m, 1.0)
        self.assertLess(neighbor.residual_error_m, 0.5)

        true_z = rocket_trajectory(nearest.synchronized_timestamp_ms / 1000)[2]
        self.assertAlmostEqual(nearest.z_m, true_z, delta=2.0)

    def test_clean_frames_away_from_the_corruption_use_all_stations(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        created = triangulate_flight(flight)

        frame_dt = 1.0 / FRAME_RATE_HZ
        num_frames = int(flight_duration_s() / frame_dt) + 1
        bad_time_ms = (num_frames // 2) * frame_dt * 1000

        far_point = min(created, key=lambda tp: abs(tp.synchronized_timestamp_ms - bad_time_ms / 4))
        self.assertEqual(far_point.rejected_stations, [])
        true_z = rocket_trajectory(far_point.synchronized_timestamp_ms / 1000)[2]
        self.assertAlmostEqual(far_point.z_m, true_z, delta=1.0)


class ObservationUploadAutoTriggersTriangulationTests(TestCase):
    def test_upload_populates_triangulated_points_once_two_stations_have_data(self):
        session = TrackingSession.objects.create(name="Auto Triangulate Test")
        flight = TrackingFlight.objects.create(session=session)

        position_a = (30.0, 0.0, 0.0)
        position_b = (-30.0, 0.0, 0.0)
        point = (0.0, 0.0, 20.0)

        station_a = _make_station_aimed_at(session, "A", 1, position_a, point)
        station_b = _make_station_aimed_at(session, "B", 2, position_b, point)
        for station in (station_a, station_b):
            station.clock_offset_ms = 0.0
            station.save(update_fields=["clock_offset_ms"])

        def upload(station, position):
            calibration = station.calibrations.get(is_active=True)
            pixel_x, pixel_y = project_to_pixel(point, position, calibration)
            return self.client.post(
                "/api/optical/stations/observations/",
                data=json.dumps(
                    {
                        "device_token": station.device_token,
                        "flight_number": flight.number,
                        "image_width_px": 1920,
                        "image_height_px": 1080,
                        "observations": [
                            {"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": pixel_x, "pixel_y": pixel_y},
                            {"frame_index": 1, "local_timestamp_ms": 1000, "pixel_x": pixel_x, "pixel_y": pixel_y},
                        ],
                    }
                ),
                content_type="application/json",
            )

        res_a = upload(station_a, position_a)
        self.assertEqual(res_a.status_code, 201, res_a.content)
        self.assertEqual(TriangulatedPoint.objects.filter(flight=flight).count(), 0)

        res_b = upload(station_b, position_b)
        self.assertEqual(res_b.status_code, 201, res_b.content)
        self.assertGreater(TriangulatedPoint.objects.filter(flight=flight).count(), 0)


def _make_triangulated_point(
    flight,
    t_ms,
    x,
    y,
    z,
    stations_used=None,
    rejected_stations=None,
    residual_error_m=None,
):
    stations_used = stations_used if stations_used is not None else []
    return TriangulatedPoint.objects.create(
        session=flight.session,
        flight=flight,
        synchronized_timestamp_ms=t_ms,
        x_m=x,
        y_m=y,
        z_m=z,
        stations_used=stations_used,
        stations_used_count=len(stations_used),
        rejected_stations=rejected_stations if rejected_stations is not None else [],
        residual_error_m=residual_error_m,
    )


class MovingAverageTests(TestCase):
    def test_centered_average_with_shrinking_window_at_edges(self):
        values = [0.0, 10.0, 20.0, 30.0, 40.0]
        result = _moving_average(values, window_points=3)

        self.assertAlmostEqual(result[0], (0.0 + 10.0) / 2)
        self.assertAlmostEqual(result[1], (0.0 + 10.0 + 20.0) / 3)
        self.assertAlmostEqual(result[2], (10.0 + 20.0 + 30.0) / 3)
        self.assertAlmostEqual(result[3], (20.0 + 30.0 + 40.0) / 3)
        self.assertAlmostEqual(result[4], (30.0 + 40.0) / 2)

    def test_window_of_one_returns_values_unchanged(self):
        values = [1.0, 2.0, 3.0]
        self.assertEqual(_moving_average(values, window_points=1), values)


class AssembleTrajectoryForFlightTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Trajectory Assembly Test")
        self.flight = TrackingFlight.objects.create(session=self.session)

    def test_small_gap_gets_linearly_interpolated(self):
        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_triangulated_point(self.flight, 50, 5.0, 0.0, 0.0)
        _make_triangulated_point(self.flight, 100, 10.0, 0.0, 0.0)
        _make_triangulated_point(self.flight, 250, 25.0, 0.0, 0.0)  # 150ms gap after t=100

        created = assemble_trajectory_for_flight(self.flight)
        by_time = {tp.timestamp_ms: tp for tp in created}

        self.assertEqual(set(by_time.keys()), {0, 50, 100, 150, 200, 250})
        self.assertFalse(by_time[100].is_gap_filled)
        self.assertIsNotNone(by_time[100].source_point)
        self.assertTrue(by_time[150].is_gap_filled)
        self.assertIsNone(by_time[150].source_point)
        self.assertAlmostEqual(by_time[150].raw_x_m, 15.0)
        self.assertTrue(by_time[200].is_gap_filled)
        self.assertAlmostEqual(by_time[200].raw_x_m, 20.0)
        self.assertFalse(by_time[250].is_gap_filled)

    def test_large_gap_is_left_as_an_honest_hole(self):
        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_triangulated_point(self.flight, 2000, 100.0, 0.0, 0.0)  # 2000ms gap

        created = assemble_trajectory_for_flight(self.flight, max_gap_fill_ms=1000)
        self.assertEqual(len(created), 2)
        timestamps = {tp.timestamp_ms for tp in created}
        self.assertEqual(timestamps, {0, 2000})

    def test_rerunning_does_not_duplicate_rows(self):
        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_triangulated_point(self.flight, 50, 1.0, 0.0, 0.0)
        _make_triangulated_point(self.flight, 100, 2.0, 0.0, 0.0)

        first_count = len(assemble_trajectory_for_flight(self.flight))
        second_count = len(assemble_trajectory_for_flight(self.flight))
        self.assertEqual(first_count, second_count)
        self.assertEqual(TrajectoryPoint.objects.filter(flight=self.flight).count(), second_count)

    def test_fewer_than_two_triangulated_points_produces_nothing(self):
        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0)
        self.assertEqual(assemble_trajectory_for_flight(self.flight), [])


class AssembleTrajectoryForMockFlightTests(TestCase):
    def test_filtered_trajectory_tracks_the_simulated_flight_profile(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulated = triangulate_flight(flight)
        assembled = assemble_trajectory_for_flight(flight)

        self.assertGreater(len(assembled), 0)
        # Small dropped-frame gaps are well within the default threshold,
        # so the assembled trajectory should be close in size to the raw
        # triangulated points (plus a handful of gap-filled slots).
        self.assertGreaterEqual(len(assembled), len(triangulated))

        max_filtered_z = max(tp.filtered_z_m for tp in assembled)
        max_raw_z = max(tp.raw_z_m for tp in assembled)
        self.assertAlmostEqual(max_filtered_z, max_raw_z, delta=1.0)

        true_apogee_m = max(rocket_trajectory(t / 1000.0)[2] for t in range(0, 20000, 50))
        self.assertAlmostEqual(max_filtered_z, true_apogee_m, delta=1.5)


class ObservationUploadAutoAssemblesTrajectoryTests(TestCase):
    def test_upload_populates_trajectory_points_once_two_stations_have_data(self):
        session = TrackingSession.objects.create(name="Auto Trajectory Test")
        flight = TrackingFlight.objects.create(session=session)

        position_a = (30.0, 0.0, 0.0)
        position_b = (-30.0, 0.0, 0.0)
        point = (0.0, 0.0, 20.0)

        station_a = _make_station_aimed_at(session, "A", 1, position_a, point)
        station_b = _make_station_aimed_at(session, "B", 2, position_b, point)
        for station in (station_a, station_b):
            station.clock_offset_ms = 0.0
            station.save(update_fields=["clock_offset_ms"])

        def upload(station, position):
            calibration = station.calibrations.get(is_active=True)
            pixel_x, pixel_y = project_to_pixel(point, position, calibration)
            return self.client.post(
                "/api/optical/stations/observations/",
                data=json.dumps(
                    {
                        "device_token": station.device_token,
                        "flight_number": flight.number,
                        "image_width_px": 1920,
                        "image_height_px": 1080,
                        "observations": [
                            {"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": pixel_x, "pixel_y": pixel_y},
                            {"frame_index": 1, "local_timestamp_ms": 1000, "pixel_x": pixel_x, "pixel_y": pixel_y},
                        ],
                    }
                ),
                content_type="application/json",
            )

        upload(station_a, position_a)
        self.assertEqual(TrajectoryPoint.objects.filter(flight=flight).count(), 0)

        upload(station_b, position_b)
        self.assertGreater(TrajectoryPoint.objects.filter(flight=flight).count(), 0)


def _make_trajectory_point(flight, t_ms, x, y, z, is_gap_filled=False):
    return TrajectoryPoint.objects.create(
        session=flight.session,
        flight=flight,
        timestamp_ms=t_ms,
        raw_x_m=x,
        raw_y_m=y,
        raw_z_m=z,
        filtered_x_m=x,
        filtered_y_m=y,
        filtered_z_m=z,
        is_interpolated=is_gap_filled,
        is_gap_filled=is_gap_filled,
    )


class ComputeDerivedFlightDataTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Derived Flight Data Test")
        self.flight = TrackingFlight.objects.create(session=self.session)

    def test_hand_built_trajectory_matches_exact_expected_values(self):
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_trajectory_point(self.flight, 1000, 2.0, 1.0, 20.0)
        _make_trajectory_point(self.flight, 2000, 4.0, 2.0, 30.0)  # apogee
        _make_trajectory_point(self.flight, 3000, 6.0, 3.0, 15.0)
        _make_trajectory_point(self.flight, 4000, 8.0, 4.0, 0.0)  # landing

        data = compute_derived_flight_data(self.flight)

        self.assertAlmostEqual(data["flight_duration_s"], 4.0)
        self.assertAlmostEqual(data["time_to_apogee_s"], 2.0)
        self.assertAlmostEqual(data["apogee_height_m"], 30.0)
        self.assertAlmostEqual(data["ascent_rate_max_m_s"], 20.0)
        self.assertAlmostEqual(data["descent_rate_max_m_s"], 15.0)
        self.assertAlmostEqual(data["max_speed_m_s"], math.sqrt(2**2 + 1**2 + 20**2), places=4)
        self.assertAlmostEqual(data["drift_at_apogee_m"], math.hypot(4.0, 2.0), places=4)
        self.assertAlmostEqual(data["landing_estimate"]["distance_m"], math.hypot(8.0, 4.0), places=4)
        expected_bearing = math.degrees(math.atan2(8.0, 4.0)) % 360
        self.assertAlmostEqual(data["landing_estimate"]["bearing_deg"], expected_bearing, places=4)
        self.assertEqual(data["limitations"], [])

    def test_fewer_than_two_points_returns_gracefully(self):
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        data = compute_derived_flight_data(self.flight)
        self.assertIsNone(data["flight_duration_s"])
        self.assertIsNone(data["apogee_height_m"])
        self.assertTrue(data["limitations"])

    def test_landing_well_above_pad_is_flagged_as_a_caution(self):
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_trajectory_point(self.flight, 1000, 0.0, 0.0, 20.0)
        _make_trajectory_point(self.flight, 2000, 0.0, 0.0, 15.0)  # last point still 15m up

        data = compute_derived_flight_data(self.flight)
        self.assertTrue(any("landing" in msg.lower() for msg in data["limitations"]))

    def test_gap_filled_points_are_noted_in_limitations(self):
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_trajectory_point(self.flight, 50, 1.0, 0.0, 1.0, is_gap_filled=True)
        _make_trajectory_point(self.flight, 100, 2.0, 0.0, 2.0)

        data = compute_derived_flight_data(self.flight)
        self.assertTrue(any("interpolated" in msg.lower() for msg in data["limitations"]))


class ComputeDerivedFlightDataMockFlightTests(TestCase):
    def test_apogee_and_duration_track_the_simulated_flight(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)

        data = compute_derived_flight_data(flight)
        self.assertIsNotNone(data["apogee_height_m"])
        self.assertAlmostEqual(data["apogee_height_m"], 45.0, delta=1.5)
        self.assertAlmostEqual(data["flight_duration_s"], flight_duration_s(), delta=0.3)
        self.assertGreater(data["landing_estimate"]["distance_m"], 0.0)


class TrackingFlightDebriefApiTests(TestCase):
    def test_debrief_endpoint_returns_expected_keys(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)

        res = self.client.get(f"/api/optical/flights/{flight.id}/debrief/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for key in (
            "flight_duration_s",
            "time_to_apogee_s",
            "apogee_height_m",
            "ascent_rate_max_m_s",
            "descent_rate_max_m_s",
            "max_speed_m_s",
            "drift_at_apogee_m",
            "landing_estimate",
            "limitations",
        ):
            self.assertIn(key, data)
        self.assertAlmostEqual(data["apogee_height_m"], 45.0, delta=1.5)


class DetectFlightEventsTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Flight Event Detection Test")
        self.flight = TrackingFlight.objects.create(session=self.session)

    def _build_full_scenario(self):
        # Vertical-only motion with a genuine accelerate-then-decelerate
        # ascent, so burnout (peak speed) is a real, separate instant
        # from first_motion/launch.
        z_by_t = {
            0: 0.0,
            100: 0.05,  # still within the first_motion threshold
            200: 0.5,  # first_motion/launch triggers here
            300: 3.0,  # accelerating (v=25 m/s)
            400: 8.0,  # peak speed (v=50 m/s) -> burnout proxy at t_mid=350
            500: 12.0,  # already decelerating (v=40 m/s)
            600: 15.0,
            700: 17.0,
            800: 18.0,
            900: 18.2,  # apogee
            1000: 18.0,
            1100: 15.0,
            1200: 10.0,
            1300: 5.0,
            1400: 0.5,  # landing near the pad
        }
        for t, z in sorted(z_by_t.items()):
            _make_trajectory_point(self.flight, t, 0.0, 0.0, z)

    def test_full_scenario_detects_events_in_the_right_order(self):
        self._build_full_scenario()
        events = detect_flight_events(self.flight)
        by_type = {e.event_type: e for e in events}

        self.assertEqual(set(by_type.keys()), {"first_motion", "launch", "burnout", "apogee", "landing"})
        self.assertEqual(by_type["first_motion"].timestamp_ms, 200)
        self.assertEqual(by_type["launch"].timestamp_ms, 200)
        self.assertEqual(by_type["burnout"].timestamp_ms, 350)
        self.assertEqual(by_type["apogee"].timestamp_ms, 900)
        self.assertEqual(by_type["landing"].timestamp_ms, 1400)

        self.assertLess(by_type["launch"].timestamp_ms, by_type["burnout"].timestamp_ms)
        self.assertLess(by_type["burnout"].timestamp_ms, by_type["apogee"].timestamp_ms)
        self.assertLess(by_type["apogee"].timestamp_ms, by_type["landing"].timestamp_ms)

        for event in events:
            self.assertIsNone(event.confidence)
            self.assertTrue(event.detection_method)

        self.assertEqual(by_type["landing"].detection_method, "height dropped back near the pad")
        self.assertEqual(by_type["landing"].notes, "")

    def test_no_motion_produces_no_events(self):
        for t in range(0, 500, 100):
            _make_trajectory_point(self.flight, t, 0.0, 0.0, 0.05)  # never crosses 0.3m
        events = detect_flight_events(self.flight)
        self.assertEqual(events, [])
        self.assertEqual(FlightEvent.objects.filter(flight=self.flight).count(), 0)

    def test_coverage_ending_early_flags_landing_as_unconfirmed(self):
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_trajectory_point(self.flight, 500, 0.0, 0.0, 20.0)
        _make_trajectory_point(self.flight, 1000, 0.0, 0.0, 25.0)  # coverage ends while still airborne

        events = detect_flight_events(self.flight)
        landing = next(e for e in events if e.event_type == "landing")
        self.assertIn("multi-station coverage", landing.detection_method.lower())
        self.assertTrue(landing.notes)

    def test_rerunning_does_not_duplicate_rows(self):
        self._build_full_scenario()
        first_count = len(detect_flight_events(self.flight))
        second_count = len(detect_flight_events(self.flight))
        self.assertEqual(first_count, second_count)
        self.assertEqual(FlightEvent.objects.filter(flight=self.flight).count(), second_count)


class DetectFlightEventsMockFlightTests(TestCase):
    def test_events_land_at_sensible_times(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)

        events = detect_flight_events(flight)
        by_type = {e.event_type: e for e in events}
        self.assertEqual(set(by_type.keys()), {"first_motion", "launch", "burnout", "apogee", "landing"})

        # Matches Phase 9's own apogee-time check for this same fixture.
        self.assertAlmostEqual(by_type["apogee"].timestamp_ms / 1000, 2.95, delta=0.3)

        # The mock's trajectory model has no modeled thrust ramp - full
        # velocity is attained instantly at t=0 (see optical_mock.py's
        # rocket_trajectory), so "peak ascent speed" (burnout's proxy)
        # coincides with launch for this fixture. That's the simulation's
        # simplification showing through honestly, not a bug in the
        # detector - a real flight's gradual thrust build-up would give
        # burnout a genuinely separate timestamp, as the hand-built
        # scenario above already confirms.
        self.assertAlmostEqual(
            by_type["burnout"].timestamp_ms, by_type["launch"].timestamp_ms, delta=100
        )


class ObservationUploadAutoDetectsFlightEventsTests(TestCase):
    def test_upload_populates_flight_events_once_two_stations_have_data(self):
        session = TrackingSession.objects.create(name="Auto Flight Events Test")
        flight = TrackingFlight.objects.create(session=session)

        position_a = (30.0, 0.0, 0.0)
        position_b = (-30.0, 0.0, 0.0)
        # Two distinct instants with real vertical motion, so the
        # auto-triggered detection has something to actually find.
        point0 = (0.0, 0.0, 0.0)
        point1 = (0.0, 0.0, 5.0)

        station_a = _make_station_aimed_at(session, "A", 1, position_a, point0)
        station_b = _make_station_aimed_at(session, "B", 2, position_b, point0)
        for station in (station_a, station_b):
            station.clock_offset_ms = 0.0
            station.save(update_fields=["clock_offset_ms"])

        def upload(station, position):
            calibration = station.calibrations.get(is_active=True)
            pixel0 = project_to_pixel(point0, position, calibration)
            pixel1 = project_to_pixel(point1, position, calibration)
            return self.client.post(
                "/api/optical/stations/observations/",
                data=json.dumps(
                    {
                        "device_token": station.device_token,
                        "flight_number": flight.number,
                        "image_width_px": 1920,
                        "image_height_px": 1080,
                        "observations": [
                            {"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": pixel0[0], "pixel_y": pixel0[1]},
                            {"frame_index": 1, "local_timestamp_ms": 1000, "pixel_x": pixel1[0], "pixel_y": pixel1[1]},
                        ],
                    }
                ),
                content_type="application/json",
            )

        upload(station_a, position_a)
        self.assertEqual(FlightEvent.objects.filter(flight=flight).count(), 0)

        upload(station_b, position_b)
        events = FlightEvent.objects.filter(flight=flight)
        self.assertGreater(events.count(), 0)
        self.assertTrue(events.filter(event_type=FlightEvent.EVENT_FIRST_MOTION).exists())


class ComputeQualityMetricsForFlightTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Quality Metrics Test")
        self.flight = TrackingFlight.objects.create(session=self.session)
        self._next_index = 1

    def _make_station(self, label, round_trip_ms):
        station = TrackingStation.objects.create(
            session=self.session,
            label=label,
            station_index=self._next_index,
            position_source=TrackingStation.SOURCE_MOCK,
        )
        self._next_index += 1
        station.clock_round_trip_ms = round_trip_ms
        station.save(update_fields=["clock_round_trip_ms"])
        return station

    def test_hand_built_metrics_match_exact_expected_values(self):
        station_a = self._make_station("A", 10.0)
        station_b = self._make_station("B", 20.0)
        station_c = self._make_station("C", 30.0)

        ids3 = [station_a.id, station_b.id, station_c.id]
        ids2 = [station_a.id, station_b.id]

        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0, stations_used=ids3, residual_error_m=0.1)
        _make_triangulated_point(self.flight, 50, 0.0, 0.0, 0.0, stations_used=ids3, residual_error_m=0.2)
        _make_triangulated_point(
            self.flight, 100, 0.0, 0.0, 0.0,
            stations_used=ids2, rejected_stations=[station_c.id], residual_error_m=1.5,
        )
        _make_triangulated_point(self.flight, 150, 0.0, 0.0, 0.0, stations_used=ids3, residual_error_m=0.05)
        _make_triangulated_point(
            self.flight, 200, 0.0, 0.0, 0.0,
            stations_used=ids2, rejected_stations=[station_c.id], residual_error_m=2.0,
        )

        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0, is_gap_filled=False)
        _make_trajectory_point(self.flight, 50, 0.0, 0.0, 0.0, is_gap_filled=True)
        _make_trajectory_point(self.flight, 100, 0.0, 0.0, 0.0, is_gap_filled=False)
        _make_trajectory_point(self.flight, 150, 0.0, 0.0, 0.0, is_gap_filled=False)

        metrics = compute_quality_metrics_for_flight(self.flight)

        self.assertAlmostEqual(metrics.pct_three_station, 60.0)
        self.assertAlmostEqual(metrics.pct_two_station, 40.0)
        self.assertAlmostEqual(metrics.pct_interpolated, 25.0)
        self.assertAlmostEqual(metrics.mean_residual_m, (0.1 + 0.2 + 1.5 + 0.05 + 2.0) / 5)
        self.assertAlmostEqual(metrics.max_residual_m, 2.0)
        self.assertEqual(metrics.num_outliers_rejected, 2)
        self.assertAlmostEqual(metrics.sync_quality_score, 20.0)

    def test_sync_quality_score_only_counts_contributing_stations(self):
        station_a = self._make_station("A", 10.0)
        station_b = self._make_station("B", 20.0)
        self._make_station("Never Used", 999.0)

        _make_triangulated_point(
            self.flight, 0, 0.0, 0.0, 0.0, stations_used=[station_a.id, station_b.id]
        )
        metrics = compute_quality_metrics_for_flight(self.flight)
        self.assertAlmostEqual(metrics.sync_quality_score, 15.0)

    def test_no_triangulated_points_returns_none_and_clears_stale_row(self):
        station_a = self._make_station("A", 10.0)
        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0, stations_used=[station_a.id])
        compute_quality_metrics_for_flight(self.flight)
        self.assertEqual(TrackingQualityMetrics.objects.filter(flight=self.flight).count(), 1)

        TriangulatedPoint.objects.filter(flight=self.flight).delete()
        result = compute_quality_metrics_for_flight(self.flight)
        self.assertIsNone(result)
        self.assertEqual(TrackingQualityMetrics.objects.filter(flight=self.flight).count(), 0)

    def test_rerunning_updates_the_same_row(self):
        station_a = self._make_station("A", 10.0)
        _make_triangulated_point(self.flight, 0, 0.0, 0.0, 0.0, stations_used=[station_a.id])

        first = compute_quality_metrics_for_flight(self.flight)
        second = compute_quality_metrics_for_flight(self.flight)
        self.assertEqual(first.id, second.id)
        self.assertEqual(TrackingQualityMetrics.objects.filter(flight=self.flight).count(), 1)


class ComputeQualityMetricsForMockFlightTests(TestCase):
    def test_sensible_values_against_the_mock_flight(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)

        metrics = compute_quality_metrics_for_flight(flight)
        self.assertIsNotNone(metrics)
        self.assertGreaterEqual(metrics.pct_three_station, 0.0)
        self.assertLessEqual(metrics.pct_three_station, 100.0)
        self.assertGreaterEqual(metrics.pct_two_station, 0.0)
        self.assertIsNotNone(metrics.mean_residual_m)
        self.assertGreaterEqual(metrics.num_outliers_rejected, 0)


class ObservationUploadAutoComputesQualityMetricsTests(TestCase):
    def test_upload_populates_quality_metrics_once_two_stations_have_data(self):
        session = TrackingSession.objects.create(name="Auto Quality Metrics Test")
        flight = TrackingFlight.objects.create(session=session)

        position_a = (30.0, 0.0, 0.0)
        position_b = (-30.0, 0.0, 0.0)
        point = (0.0, 0.0, 20.0)

        station_a = _make_station_aimed_at(session, "A", 1, position_a, point)
        station_b = _make_station_aimed_at(session, "B", 2, position_b, point)
        for station in (station_a, station_b):
            station.clock_offset_ms = 0.0
            station.clock_round_trip_ms = 15.0
            station.save(update_fields=["clock_offset_ms", "clock_round_trip_ms"])

        def upload(station, position):
            calibration = station.calibrations.get(is_active=True)
            pixel_x, pixel_y = project_to_pixel(point, position, calibration)
            return self.client.post(
                "/api/optical/stations/observations/",
                data=json.dumps(
                    {
                        "device_token": station.device_token,
                        "flight_number": flight.number,
                        "image_width_px": 1920,
                        "image_height_px": 1080,
                        "observations": [
                            {"frame_index": 0, "local_timestamp_ms": 0, "pixel_x": pixel_x, "pixel_y": pixel_y},
                            {"frame_index": 1, "local_timestamp_ms": 1000, "pixel_x": pixel_x, "pixel_y": pixel_y},
                        ],
                    }
                ),
                content_type="application/json",
            )

        upload(station_a, position_a)
        self.assertEqual(TrackingQualityMetrics.objects.filter(flight=flight).count(), 0)

        upload(station_b, position_b)
        self.assertEqual(TrackingQualityMetrics.objects.filter(flight=flight).count(), 1)


class BuildFlightSummaryTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Flight Summary Test")
        self.flight = TrackingFlight.objects.create(session=self.session)

    def test_hand_built_flight_summary_includes_everything(self):
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_trajectory_point(self.flight, 1000, 2.0, 1.0, 20.0)
        _make_trajectory_point(self.flight, 2000, 4.0, 2.0, 10.0)

        FlightEvent.objects.create(
            session=self.session,
            flight=self.flight,
            event_type=FlightEvent.EVENT_APOGEE,
            timestamp_ms=1000,
            detection_method="maximum filtered height",
        )
        TrackingQualityMetrics.objects.create(
            flight=self.flight,
            pct_three_station=100.0,
            pct_two_station=0.0,
            mean_residual_m=0.1,
            max_residual_m=0.2,
            num_outliers_rejected=0,
        )

        summary = build_flight_summary(self.flight)

        self.assertEqual(summary["flight_number"], self.flight.number)
        self.assertIsNotNone(summary["stats"]["apogee_height_m"])
        self.assertEqual(len(summary["events"]), 1)
        self.assertEqual(summary["events"][0]["event_type"], "apogee")
        self.assertIsNotNone(summary["quality"])
        self.assertAlmostEqual(summary["quality"]["pct_three_station"], 100.0)
        self.assertEqual(len(summary["trajectory"]), 3)

    def test_flight_with_no_data_degrades_gracefully(self):
        summary = build_flight_summary(self.flight)
        self.assertIsNone(summary["stats"]["apogee_height_m"])
        self.assertTrue(summary["stats"]["limitations"])
        self.assertEqual(summary["events"], [])
        self.assertIsNone(summary["quality"])
        self.assertEqual(summary["trajectory"], [])


class ExportTrackingSessionTests(TestCase):
    def test_two_flights_derived_data_stays_correctly_separated(self):
        session = TrackingSession.objects.create(name="Export Test")
        flight_a = TrackingFlight.objects.create(session=session)
        flight_b = TrackingFlight.objects.create(session=session)

        _make_triangulated_point(flight_a, 0, 1.0, 1.0, 10.0, stations_used=[1, 2])
        _make_triangulated_point(flight_b, 0, 2.0, 2.0, 20.0, stations_used=[1, 2])
        FlightEvent.objects.create(
            session=session, flight=flight_a, event_type=FlightEvent.EVENT_APOGEE, timestamp_ms=0
        )

        export = export_tracking_session(session)

        self.assertEqual(export["schema_version"], "1.0")
        self.assertEqual(len(export["flights"]), 2)

        entry_a = next(f for f in export["flights"] if f["flight_number"] == flight_a.number)
        entry_b = next(f for f in export["flights"] if f["flight_number"] == flight_b.number)

        self.assertEqual(len(entry_a["triangulated_points"]), 1)
        self.assertEqual(entry_a["triangulated_points"][0]["z_m"], 10.0)
        self.assertEqual(len(entry_a["flight_events"]), 1)

        self.assertEqual(len(entry_b["triangulated_points"]), 1)
        self.assertEqual(entry_b["triangulated_points"][0]["z_m"], 20.0)
        self.assertEqual(len(entry_b["flight_events"]), 0)


class TrackingFlightSummaryApiTests(TestCase):
    def test_summary_endpoint_returns_expected_keys(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)
        detect_flight_events(flight)
        compute_quality_metrics_for_flight(flight)

        res = self.client.get(f"/api/optical/flights/{flight.id}/summary/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for key in ("flight_number", "stats", "events", "quality", "trajectory"):
            self.assertIn(key, data)
        self.assertGreater(len(data["trajectory"]), 0)
        self.assertGreater(len(data["events"]), 0)
        self.assertIsNotNone(data["quality"])


class TrackingSessionExportApiTests(TestCase):
    def test_export_endpoint_returns_expected_shape(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)
        detect_flight_events(flight)
        compute_quality_metrics_for_flight(flight)

        res = self.client.get(f"/api/optical/sessions/{session.id}/export/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["schema_version"], "1.0")
        self.assertIn("session", data)
        self.assertIn("flights", data)
        self.assertEqual(len(data["flights"]), 1)
        flight_entry = data["flights"][0]
        for key in ("triangulated_points", "trajectory_points", "flight_events", "quality_metrics", "derived_stats"):
            self.assertIn(key, flight_entry)
        self.assertGreater(len(flight_entry["trajectory_points"]), 0)


class ComputePositionErrorTests(TestCase):
    def test_pure_horizontal_offset(self):
        error = compute_position_error((3.0, 4.0, 10.0), (0.0, 0.0, 10.0))
        self.assertAlmostEqual(error["horizontal_error_m"], 5.0)
        self.assertAlmostEqual(error["vertical_error_m"], 0.0)
        self.assertAlmostEqual(error["total_error_m"], 5.0)

    def test_pure_vertical_offset(self):
        error = compute_position_error((1.0, 1.0, 12.0), (1.0, 1.0, 9.5))
        self.assertAlmostEqual(error["horizontal_error_m"], 0.0)
        self.assertAlmostEqual(error["vertical_error_m"], 2.5)
        self.assertAlmostEqual(error["total_error_m"], 2.5)

    def test_combined_3d_offset(self):
        # 3-4-12 gives a horizontal leg of 5 and a 3D hypotenuse of 13.
        error = compute_position_error((3.0, 4.0, 12.0), (0.0, 0.0, 0.0))
        self.assertAlmostEqual(error["horizontal_error_m"], 5.0)
        self.assertAlmostEqual(error["vertical_error_m"], 12.0)
        self.assertAlmostEqual(error["total_error_m"], 13.0)


class ValidateFlightAgainstKnownPointTests(TestCase):
    def setUp(self):
        self.session = TrackingSession.objects.create(name="Validation Test")
        self.flight = TrackingFlight.objects.create(session=self.session)
        _make_trajectory_point(self.flight, 0, 0.0, 0.0, 0.0)
        _make_trajectory_point(self.flight, 100, 1.0, 0.0, 20.0)  # apogee
        _make_trajectory_point(self.flight, 200, 2.0, 0.0, 5.0)

    def test_no_trajectory_points_returns_detail(self):
        empty_flight = TrackingFlight.objects.create(session=self.session)
        result = validate_flight_against_known_point(empty_flight, 0.0, 0.0, 0.0)
        self.assertIn("detail", result)

    def test_defaults_to_apogee_point(self):
        result = validate_flight_against_known_point(self.flight, 1.0, 0.0, 20.0)
        self.assertEqual(result["compared_timestamp_ms"], 100)
        self.assertAlmostEqual(result["total_error_m"], 0.0)

    def test_explicit_timestamp_overrides_apogee_default(self):
        result = validate_flight_against_known_point(
            self.flight, 2.0, 0.0, 5.0, at_timestamp_ms=200
        )
        self.assertEqual(result["compared_timestamp_ms"], 200)
        self.assertAlmostEqual(result["total_error_m"], 0.0)

    def test_explicit_timestamp_picks_nearest_point(self):
        result = validate_flight_against_known_point(
            self.flight, 0.0, 0.0, 0.0, at_timestamp_ms=80
        )
        self.assertEqual(result["compared_timestamp_ms"], 100)

    def test_known_offset_reports_correct_error(self):
        result = validate_flight_against_known_point(self.flight, 0.0, 0.0, 0.0, at_timestamp_ms=0)
        self.assertEqual(result["compared_timestamp_ms"], 0)
        self.assertAlmostEqual(result["total_error_m"], 0.0)


class ValidatePipelineAgainstMockFlightTests(TestCase):
    """The project's first numerical-accuracy regression test: runs the
    real Phase 7-8 pipeline against the mock generator's known ground
    truth and asserts the error stays within concrete bounds. If a
    future change to the triangulation/trajectory math quietly degrades
    accuracy, this is the test that would catch it."""

    def test_pipeline_accuracy_stays_within_expected_bounds(self):
        result = validate_pipeline_against_mock_flight()

        self.assertIsNotNone(result["mean_error_m"])
        self.assertIsNotNone(result["max_error_m"])
        self.assertGreater(len(result["per_point_errors"]), 0)

        # Generous enough to allow for the mock's deliberate pixel noise,
        # dropped frames, and one deliberately corrupted observation -
        # tight enough to catch a genuine regression in the math.
        self.assertLess(result["mean_error_m"], 0.2)
        self.assertLess(result["max_error_m"], 3.0)

        self.assertIn("simulated", result["caveat"].lower())
        self.assertIn("real", result["caveat"].lower())


class TrackingFlightValidationApiTests(TestCase):
    def test_validation_endpoint_returns_computed_error(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)

        apogee_point = max(
            flight.trajectory_points.all(), key=lambda p: p.filtered_z_m
        )
        res = self.client.post(
            f"/api/optical/flights/{flight.id}/validate/",
            data=json.dumps(
                {
                    "known_x_m": apogee_point.filtered_x_m,
                    "known_y_m": apogee_point.filtered_y_m,
                    "known_z_m": apogee_point.filtered_z_m,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["compared_timestamp_ms"], apogee_point.timestamp_ms)
        self.assertAlmostEqual(data["total_error_m"], 0.0)

    def test_validation_endpoint_requires_known_position(self):
        session = generate_mock_tracking_session()
        flight = session.flights.first()
        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)

        res = self.client.post(
            f"/api/optical/flights/{flight.id}/validate/",
            data=json.dumps({"known_x_m": 1.0}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
