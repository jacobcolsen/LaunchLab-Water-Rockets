import json
import math

from django.test import TestCase

from .optical_camera import (
    camera_basis,
    pixel_to_bearing_vector,
    project_to_pixel,
    solve_boresight_from_tap,
)
from .optical_mock import (
    NUM_DROPPED_FRAMES_PER_STATION,
    STATION_CLOCK_OFFSETS_MS,
    flight_duration_s,
    generate_mock_tracking_session,
    rocket_trajectory,
)
from .optical_models import (
    FrameObservation,
    PixelObservation,
    StationCalibration,
    TrackingFlight,
    TrackingStation,
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
