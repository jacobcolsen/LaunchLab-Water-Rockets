"""Shared pinhole-camera model for the optical tracking subsystem.

Used both by the forward direction (`project_to_pixel`, in
`core/optical_mock.py`'s synthetic data) and the inverse directions
(`pixel_to_bearing_vector`, `solve_boresight_from_tap`, used by real
calibration/observation code) - one canonical camera model, not two.

Coordinate system: local East-North-Up (ENU) meters, as documented in
`core/optical_models.py`. `facing_deg` is a compass bearing (0 = north,
90 = east), `pitch_deg` is the boresight's elevation above the horizon
(positive = up), `roll_deg` is rotation about the boresight.
"""
import math


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


def pixel_to_bearing_vector(pixel_x, pixel_y, calibration):
    """Exact algebraic inverse of project_to_pixel: a tapped pixel ->
    world ENU unit bearing vector from the camera, given its calibration.
    This is the function manual/assisted/automatic observations and
    triangulation build on."""
    right, up, forward = camera_basis(
        calibration.facing_deg, calibration.pitch_deg, calibration.roll_deg
    )
    norm_x = 2 * pixel_x / calibration.image_width_px - 1
    norm_y = 1 - 2 * pixel_y / calibration.image_height_px

    x_cam = norm_x * math.tan(math.radians(calibration.fov_horizontal_deg) / 2)
    y_cam = norm_y * math.tan(math.radians(calibration.fov_vertical_deg) / 2)
    z_cam = 1.0

    direction = tuple(
        x_cam * right[i] + y_cam * up[i] + z_cam * forward[i] for i in range(3)
    )
    return _normalize(direction)


def solve_boresight_from_tap(true_direction, tap_pixel_x, tap_pixel_y, calibration, pitch_hint_deg):
    """Closed-form geometry solve for (facing_deg, pitch_deg): given the
    exact world bearing to a known reference point (true_direction, e.g.
    computed from a station's known ENU position to the pad at the
    origin) and the pixel where that point was tapped, returns the
    boresight orientation that makes the camera model agree exactly
    (assuming roll_deg is unchanged/zero).

    Derivation: with roll=0, camera_basis's (right, up, forward) come from
    a pure azimuth(facing)/elevation(pitch) gimbal. Writing the tapped
    pixel's local direction as (a, b, c) and the target world direction as
    (Te, Tn, Tu), the "up" component (b*cos(pitch) + c*sin(pitch) = Tu) is
    a single-unknown sinusoid - solved directly via acos, giving two
    candidate roots disambiguated by pitch_hint_deg (the sensor capture).
    With pitch fixed, the east/north components reduce to a 2x2 linear
    system solved for facing directly. No iteration.
    """
    te, tn, tu = _normalize(true_direction)

    norm_x = 2 * tap_pixel_x / calibration.image_width_px - 1
    norm_y = 1 - 2 * tap_pixel_y / calibration.image_height_px
    ux = norm_x * math.tan(math.radians(calibration.fov_horizontal_deg) / 2)
    uy = norm_y * math.tan(math.radians(calibration.fov_vertical_deg) / 2)
    a, b, c = _normalize((ux, uy, 1.0))

    phi = math.atan2(c, b)
    r_bc = math.hypot(b, c)
    cos_arg = max(-1.0, min(1.0, tu / r_bc))
    delta = math.acos(cos_arg)
    pitch_hint = math.radians(pitch_hint_deg)

    best = None
    for p in (phi + delta, phi - delta):
        k = c * math.cos(p) - b * math.sin(p)
        denom = a * a + k * k
        if denom < 1e-9:
            continue
        cos_f = (a * te + k * tn) / denom
        sin_f = (k * te - a * tn) / denom
        f = math.atan2(sin_f, cos_f)
        if best is None or abs(p - pitch_hint) < abs(best[0] - pitch_hint):
            best = (p, f)

    pitch_rad, facing_rad = best
    return math.degrees(facing_rad) % 360, math.degrees(pitch_rad)
