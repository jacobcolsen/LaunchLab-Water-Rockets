from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .optical_bearing import compute_bearing_series_for_flight
from .optical_camera import solve_boresight_from_tap
from .optical_debrief import compute_derived_flight_data
from .optical_events import detect_flight_events
from .optical_models import (
    FrameObservation,
    PixelObservation,
    StationCalibration,
    TrackingFlight,
    TrackingSession,
    TrackingStation,
)
from .optical_quality import compute_quality_metrics_for_flight
from .optical_serializers import (
    StationCalibrationSerializer,
    TrackingFlightSerializer,
    TrackingSessionSerializer,
    TrackingStationCreateResponseSerializer,
    TrackingStationSerializer,
    export_tracking_session,
)
from .optical_summary import build_flight_summary
from .optical_trajectory import assemble_trajectory_for_flight
from .optical_triangulation import triangulate_flight
from .optical_validation import validate_flight_against_known_point

CALIBRATION_FIELDS = (
    "image_width_px",
    "image_height_px",
    "fov_horizontal_deg",
    "fov_vertical_deg",
    "facing_deg",
    "pitch_deg",
    "roll_deg",
    "orientation_source",
)

POSITION_FIELDS = (
    "label",
    "position_source",
    "gps_latitude",
    "gps_longitude",
    "gps_altitude_m",
    "surveyed_x_m",
    "surveyed_y_m",
    "surveyed_z_m",
    "measured_height_m",
)


class TrackingSessionCreateView(generics.CreateAPIView):
    queryset = TrackingSession.objects.all()
    serializer_class = TrackingSessionSerializer
    authentication_classes = []
    permission_classes = [AllowAny]


class TrackingStationListCreateView(generics.ListCreateAPIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        return TrackingStation.objects.filter(
            session_id=self.kwargs["session_id"]
        ).order_by("station_index")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TrackingStationCreateResponseSerializer
        return TrackingStationSerializer

    def perform_create(self, serializer):
        session = get_object_or_404(TrackingSession, pk=self.kwargs["session_id"])
        serializer.save(session=session, station_index=session.stations.count() + 1)


class TrackingStationPositionView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        device_token = request.data.get("device_token")
        if not device_token:
            return Response(
                {"detail": "device_token is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        station = get_object_or_404(TrackingStation, device_token=device_token)
        data = {field: request.data[field] for field in POSITION_FIELDS if field in request.data}
        serializer = TrackingStationCreateResponseSerializer(station, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class TrackingStationCalibrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        device_token = request.data.get("device_token")
        if not device_token:
            return Response(
                {"detail": "device_token is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        station = get_object_or_404(TrackingStation, device_token=device_token)
        data = {field: request.data[field] for field in CALIBRATION_FIELDS if field in request.data}
        serializer = StationCalibrationSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        StationCalibration.objects.filter(station=station, is_active=True).update(is_active=False)
        calibration = StationCalibration.objects.create(
            station=station, is_active=True, **serializer.validated_data
        )
        return Response(
            StationCalibrationSerializer(calibration).data, status=status.HTTP_201_CREATED
        )


class TrackingStationCalibrationRefineView(APIView):
    """Geometry-based correction: given a tap on the pad and the station's
    already-known ENU position (Phase 2), solves for the exact facing/pitch
    rather than trusting the compass - only possible for stations whose
    position is known in the local ENU frame (surveyed_enu/mock)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        device_token = request.data.get("device_token")
        tap_pixel_x = request.data.get("tap_pixel_x")
        tap_pixel_y = request.data.get("tap_pixel_y")
        if not device_token or tap_pixel_x is None or tap_pixel_y is None:
            return Response(
                {"detail": "device_token, tap_pixel_x, and tap_pixel_y are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        station = get_object_or_404(TrackingStation, device_token=device_token)
        if station.position_source not in (TrackingStation.SOURCE_SURVEYED, TrackingStation.SOURCE_MOCK):
            return Response(
                {
                    "detail": "Accuracy refinement is only available for surveyed or mock "
                    "position sources right now."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if None in (station.surveyed_x_m, station.surveyed_y_m, station.surveyed_z_m):
            return Response(
                {"detail": "This station has no saved (x, y, z) position yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        active = StationCalibration.objects.filter(station=station, is_active=True).first()
        if active is None:
            return Response(
                {"detail": "This station has no calibration to refine yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        station_position = (station.surveyed_x_m, station.surveyed_y_m, station.surveyed_z_m)
        true_direction = tuple(-p for p in station_position)  # the pad sits at the ENU origin
        facing_deg, pitch_deg = solve_boresight_from_tap(
            true_direction, float(tap_pixel_x), float(tap_pixel_y), active, active.pitch_deg
        )

        StationCalibration.objects.filter(station=station, is_active=True).update(is_active=False)
        calibration = StationCalibration.objects.create(
            station=station,
            image_width_px=active.image_width_px,
            image_height_px=active.image_height_px,
            fov_horizontal_deg=active.fov_horizontal_deg,
            fov_vertical_deg=active.fov_vertical_deg,
            facing_deg=facing_deg,
            pitch_deg=pitch_deg,
            roll_deg=active.roll_deg,
            orientation_source=StationCalibration.ORIENTATION_GEOMETRY_REFINED,
            calibration_target_pixel_x=tap_pixel_x,
            calibration_target_pixel_y=tap_pixel_y,
            is_active=True,
        )
        return Response(
            StationCalibrationSerializer(calibration).data, status=status.HTTP_201_CREATED
        )


class TrackingFlightListCreateView(generics.ListCreateAPIView):
    serializer_class = TrackingFlightSerializer
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        return TrackingFlight.objects.filter(session_id=self.kwargs["session_id"])

    def perform_create(self, serializer):
        session = get_object_or_404(TrackingSession, pk=self.kwargs["session_id"])
        serializer.save(session=session)


class TrackingFlightDeleteView(APIView):
    """Deletes a flight and everything derived from it - every
    flight-related model already uses on_delete=CASCADE, so this is a
    complete cleanup with no custom logic needed. Flights get created by
    accident sometimes; this is the undo."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def delete(self, request, flight_id):
        flight = get_object_or_404(TrackingFlight, pk=flight_id)
        flight.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TrackingStationDeleteView(APIView):
    """Deletes a station that's no longer in use, and everything derived
    from it - StationCalibration and FrameObservation (and its
    PixelObservations) already use on_delete=CASCADE, so this is a
    complete cleanup with no custom logic needed."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def delete(self, request, station_id):
        station = get_object_or_404(TrackingStation, pk=station_id)
        station.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TrackingObservationUploadView(APIView):
    """Batch upload of per-frame observations for one flight - either
    tapped pixels (and the video itself never leaves the phone either
    way) or, for orientation-tracked flights, a facing/pitch sample with
    no pixel at all. Matches the existing clinometer Sample upload's
    'buffer locally, upload small derived data' pattern."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        device_token = request.data.get("device_token")
        flight_number = request.data.get("flight_number")
        image_width_px = request.data.get("image_width_px")
        image_height_px = request.data.get("image_height_px")
        observations = request.data.get("observations")

        if not device_token or flight_number is None or not observations:
            return Response(
                {"detail": "device_token, flight_number, and observations are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        station = get_object_or_404(TrackingStation, device_token=device_token)
        flight = get_object_or_404(TrackingFlight, session=station.session, number=flight_number)

        frame_count = 0
        pixel_count = 0
        for entry in observations:
            frame_index = entry.get("frame_index")
            local_timestamp_ms = entry.get("local_timestamp_ms")
            if frame_index is None or local_timestamp_ms is None:
                continue

            defaults = {
                "session": station.session,
                "local_timestamp_ms": local_timestamp_ms,
                "image_width_px": image_width_px,
                "image_height_px": image_height_px,
            }
            if station.clock_offset_ms is not None:
                defaults["synchronized_timestamp_ms"] = round(local_timestamp_ms + station.clock_offset_ms)

            frame, _ = FrameObservation.objects.update_or_create(
                station=station,
                flight=flight,
                frame_index=frame_index,
                defaults=defaults,
            )
            frame_count += 1

            pixel_x = entry.get("pixel_x")
            pixel_y = entry.get("pixel_y")
            facing_deg = entry.get("facing_deg")
            pitch_deg = entry.get("pitch_deg")
            has_pixel = pixel_x is not None and pixel_y is not None
            has_orientation = facing_deg is not None and pitch_deg is not None
            if has_pixel or has_orientation:
                valid_sources = dict(PixelObservation.OBSERVATION_SOURCE_CHOICES)
                observation_source = entry.get("observation_source")
                if observation_source not in valid_sources:
                    observation_source = (
                        PixelObservation.SOURCE_ORIENTATION if has_orientation else PixelObservation.SOURCE_MANUAL
                    )

                PixelObservation.objects.filter(frame=frame, is_current=True).update(is_current=False)
                PixelObservation.objects.create(
                    frame=frame,
                    pixel_x=pixel_x,
                    pixel_y=pixel_y,
                    facing_deg=facing_deg,
                    pitch_deg=pitch_deg,
                    observation_source=observation_source,
                    valid=True,
                )
                pixel_count += 1

        triangulate_flight(flight)
        assemble_trajectory_for_flight(flight)
        detect_flight_events(flight)
        compute_quality_metrics_for_flight(flight)

        return Response(
            {"frames": frame_count, "pixels": pixel_count}, status=status.HTTP_201_CREATED
        )


def server_time_view(request):
    """Stateless time echo for the client-side NTP-style round-trip
    exchange (Phase 5) - no device_token, no station lookup, just the
    server's current clock as fast as possible."""
    return JsonResponse({"server_time_ms": int(timezone.now().timestamp() * 1000)})


class TrackingStationClockSyncView(APIView):
    """Persists the offset a station's phone already computed client-side
    from several round trips against server_time_view, and backfills any
    of that station's frames still missing synchronized_timestamp_ms."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        device_token = request.data.get("device_token")
        offset_ms = request.data.get("offset_ms")
        round_trip_ms = request.data.get("round_trip_ms")
        if not device_token or offset_ms is None:
            return Response(
                {"detail": "device_token and offset_ms are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        station = get_object_or_404(TrackingStation, device_token=device_token)
        station.clock_offset_ms = offset_ms
        station.clock_round_trip_ms = round_trip_ms
        station.clock_synced_at = timezone.now()
        station.save(update_fields=["clock_offset_ms", "clock_round_trip_ms", "clock_synced_at"])

        unsynced_frames = list(
            FrameObservation.objects.filter(station=station, synchronized_timestamp_ms__isnull=True)
        )
        for frame in unsynced_frames:
            frame.synchronized_timestamp_ms = round(frame.local_timestamp_ms + offset_ms)
        FrameObservation.objects.bulk_update(unsynced_frames, ["synchronized_timestamp_ms"])

        return Response(
            {
                "clock_offset_ms": station.clock_offset_ms,
                "clock_round_trip_ms": station.clock_round_trip_ms,
                "clock_synced_at": station.clock_synced_at,
                "backfilled_frames": len(unsynced_frames),
            },
            status=status.HTTP_200_OK,
        )


class TrackingFlightDebriefView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, flight_id):
        flight = get_object_or_404(TrackingFlight, pk=flight_id)
        return Response(compute_derived_flight_data(flight))


class TrackingFlightSummaryView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, flight_id):
        flight = get_object_or_404(TrackingFlight, pk=flight_id)
        return Response(build_flight_summary(flight))


class TrackingSessionExportView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, session_id):
        session = get_object_or_404(TrackingSession, pk=session_id)
        return Response(export_tracking_session(session))


class TrackingFlightBearingView(APIView):
    """Read-only direction-over-time view for a flight - which way each
    tracking camera was actually pointed, for use when too few cameras
    were tracking to triangulate a real 3D position."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, flight_id):
        flight = get_object_or_404(TrackingFlight, pk=flight_id)
        return Response(compute_bearing_series_for_flight(flight))


class TrackingFlightValidationView(APIView):
    """Lets an operator submit one independently-measured reference point
    (e.g. a taped-measured pad crossbar height) against a real flight and
    see the computed error. Read-only in effect - computes and returns,
    persists nothing."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, flight_id):
        flight = get_object_or_404(TrackingFlight, pk=flight_id)
        known_x_m = request.data.get("known_x_m")
        known_y_m = request.data.get("known_y_m")
        known_z_m = request.data.get("known_z_m")
        if None in (known_x_m, known_y_m, known_z_m):
            return Response(
                {"detail": "known_x_m, known_y_m, and known_z_m are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        at_timestamp_ms = request.data.get("at_timestamp_ms")
        result = validate_flight_against_known_point(
            flight,
            float(known_x_m),
            float(known_y_m),
            float(known_z_m),
            at_timestamp_ms=at_timestamp_ms,
        )
        return Response(result)
