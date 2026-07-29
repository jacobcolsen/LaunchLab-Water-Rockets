"""Synthetic three-station optical tracking dataset (Phase 1).

Generates a fully-populated, self-consistent `TrackingSession`: three
stations at known ENU positions around a launch pad at the origin, a
deterministic water-rocket trajectory, and per-station pixel observations
produced by *forward*-projecting that true 3D path through each station's
camera model - the geometric inverse of what Phase 3's pixel-to-bearing
conversion will need, so that code can reuse the same camera model.

No synchronization or triangulation runs here - this only produces raw
stored data, deliberately including small per-station clock offsets,
pixel noise, several dropped ("lost the rocket") frames, and exactly one
badly-corrupted observation for later outlier-rejection testing.
"""
import math
import random

from .optical_models import (
    FrameObservation,
    PixelObservation,
    StationCalibration,
    TrackingSession,
    TrackingStation,
)

FRAME_RATE_HZ = 30
PIXEL_NOISE_STD_PX = 1.5
NUM_DROPPED_FRAMES_PER_STATION = 6

# Small, fixed per-station clock disagreement (ms) - simulates phones whose
# clocks were never perfectly synchronized before recording.
STATION_CLOCK_OFFSETS_MS = [0, 120, -80]

# (label, east_m, north_m) - roughly encircling the pad at ~40m standoff.
DEFAULT_STATION_LAYOUT = [
    ("Station 1", 40.0, 0.0),
    ("Station 2", -28.0, 32.0),
    ("Station 3", -28.0, -32.0),
]

GRAVITY_M_S2 = 9.81


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _normalize(v):
    length = math.sqrt(_dot(v, v))
    return tuple(c / length for c in v)


def camera_forward_vector(facing_deg, pitch_deg):
    """ENU unit vector the camera boresight points along. Matches the
    (east, north, up) convention used by core.triangulation.ray_direction:
    azimuth 0 = north, 90 = east; positive pitch tilts the boresight up."""
    facing = math.radians(facing_deg)
    pitch = math.radians(pitch_deg)
    return (
        math.cos(pitch) * math.sin(facing),
        math.cos(pitch) * math.cos(facing),
        math.sin(pitch),
    )


def camera_basis(facing_deg, pitch_deg, roll_deg=0.0):
    """Right-handed (right, up, forward) orthonormal unit-vector basis for
    a camera with the given compass facing / boresight pitch / roll, in
    ENU coordinates."""
    forward = camera_forward_vector(facing_deg, pitch_deg)
    world_up = (0.0, 0.0, 1.0)
    right = _normalize(_cross(forward, world_up))
    up = _cross(right, forward)

    if roll_deg:
        roll = math.radians(roll_deg)
        cos_r, sin_r = math.cos(roll), math.sin(roll)
        right, up = (
            tuple(right[i] * cos_r + up[i] * sin_r for i in range(3)),
            tuple(-right[i] * sin_r + up[i] * cos_r for i in range(3)),
        )

    return right, up, forward


def project_to_pixel(world_point, station_position, calibration):
    """Pinhole-camera forward projection of a world ENU point (meters)
    into a station's pixel coordinates, given a StationCalibration-like
    object (facing_deg/pitch_deg/roll_deg/fov_*_deg/image_*_px). Returns
    None if the point is behind the camera."""
    right, up, forward = camera_basis(
        calibration.facing_deg, calibration.pitch_deg, calibration.roll_deg
    )
    v = tuple(world_point[i] - station_position[i] for i in range(3))

    z_cam = _dot(v, forward)
    if z_cam <= 0:
        return None

    x_cam = _dot(v, right)
    y_cam = _dot(v, up)

    half_w = z_cam * math.tan(math.radians(calibration.fov_horizontal_deg) / 2)
    half_h = z_cam * math.tan(math.radians(calibration.fov_vertical_deg) / 2)
    norm_x = x_cam / half_w
    norm_y = y_cam / half_h

    pixel_x = (norm_x + 1) / 2 * calibration.image_width_px
    pixel_y = (1 - (norm_y + 1) / 2) * calibration.image_height_px
    return pixel_x, pixel_y


def rocket_trajectory(
    t_seconds,
    apogee_height_m=45.0,
    drift_east_m_s=0.8,
    drift_north_m_s=-0.4,
    descent_speed_m_s=3.5,
):
    """Deterministic water-rocket flight path in local ENU meters: a
    ballistic ascent to apogee_height_m, then a slower constant-rate
    descent (representing parachute drag), plus a small constant
    horizontal wind drift throughout. Returns (x, y, z)."""
    v0 = math.sqrt(2 * GRAVITY_M_S2 * apogee_height_m)
    t_apogee = v0 / GRAVITY_M_S2

    if t_seconds <= t_apogee:
        z = v0 * t_seconds - 0.5 * GRAVITY_M_S2 * t_seconds**2
    else:
        z = max(0.0, apogee_height_m - descent_speed_m_s * (t_seconds - t_apogee))

    x = drift_east_m_s * t_seconds
    y = drift_north_m_s * t_seconds
    return x, y, z


def flight_duration_s(apogee_height_m=45.0, descent_speed_m_s=3.5):
    v0 = math.sqrt(2 * GRAVITY_M_S2 * apogee_height_m)
    t_apogee = v0 / GRAVITY_M_S2
    return t_apogee + apogee_height_m / descent_speed_m_s


def generate_mock_tracking_session(name="Mock Flight", seed=42):
    """Builds and persists one fully-populated simulated TrackingSession:
    3 stations around a launch pad at the ENU origin, a realistic rocket
    trajectory, and forward-projected pixel observations per station/frame
    - including small clock offsets, pixel noise, several dropped frames,
    and exactly one deliberately-corrupted observation. Returns the
    created TrackingSession."""
    rng = random.Random(seed)

    session = TrackingSession.objects.create(name=name, is_simulated=True)

    stations = []
    aim_height_m = 25.0
    for i, (label, east_m, north_m) in enumerate(DEFAULT_STATION_LAYOUT, start=1):
        station = TrackingStation.objects.create(
            session=session,
            label=label,
            station_index=i,
            position_source=TrackingStation.SOURCE_MOCK,
            surveyed_x_m=east_m,
            surveyed_y_m=north_m,
            surveyed_z_m=0.0,
            measured_height_m=1.5,
        )

        # Aim each camera's boresight at roughly where the rocket will be,
        # so the mock flight stays centered enough in frame to track.
        dx, dy, dz = -east_m, -north_m, aim_height_m - 1.5
        facing_deg = math.degrees(math.atan2(dx, dy)) % 360
        pitch_deg = math.degrees(math.atan2(dz, math.hypot(dx, dy)))

        calibration = StationCalibration.objects.create(
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
        stations.append((station, calibration))

    duration_s = flight_duration_s()
    frame_dt = 1.0 / FRAME_RATE_HZ
    num_frames = int(duration_s / frame_dt) + 1

    # One globally bad tap, partway through the flight, on the first station.
    bad_frame_index = num_frames // 2

    for station_i, (station, calibration) in enumerate(stations):
        station_position = (station.surveyed_x_m, station.surveyed_y_m, station.surveyed_z_m)
        clock_offset_ms = STATION_CLOCK_OFFSETS_MS[station_i % len(STATION_CLOCK_OFFSETS_MS)]
        dropped_frames = set(
            rng.sample(
                range(2, num_frames - 2),
                min(NUM_DROPPED_FRAMES_PER_STATION, num_frames - 4),
            )
        )

        for frame_index in range(num_frames):
            t_seconds = frame_index * frame_dt
            local_timestamp_ms = int(t_seconds * 1000) + clock_offset_ms

            frame = FrameObservation.objects.create(
                session=session,
                station=station,
                frame_index=frame_index,
                local_timestamp_ms=local_timestamp_ms,
                image_width_px=calibration.image_width_px,
                image_height_px=calibration.image_height_px,
            )

            if frame_index in dropped_frames:
                continue  # "lost the rocket" this frame - no PixelObservation

            world_point = rocket_trajectory(t_seconds)
            projected = project_to_pixel(world_point, station_position, calibration)
            if projected is None:
                continue

            pixel_x, pixel_y = projected
            pixel_x += rng.gauss(0, PIXEL_NOISE_STD_PX)
            pixel_y += rng.gauss(0, PIXEL_NOISE_STD_PX)

            is_bad = station_i == 0 and frame_index == bad_frame_index
            if is_bad:
                pixel_x += 400  # deliberately wrong - Phase 7 outlier-rejection fixture
                pixel_y -= 300

            PixelObservation.objects.create(
                frame=frame,
                pixel_x=pixel_x,
                pixel_y=pixel_y,
                confidence=0.4 if is_bad else rng.uniform(0.85, 1.0),
                observation_source=PixelObservation.SOURCE_SIMULATED,
                valid=True,
            )

    return session
