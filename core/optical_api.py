from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .optical_camera import solve_boresight_from_tap
from .optical_models import (
    FrameObservation,
    PixelObservation,
    StationCalibration,
    TrackingFlight,
    TrackingSession,
    TrackingStation,
)
from .optical_serializers import (
    StationCalibrationSerializer,
    TrackingFlightSerializer,
    TrackingSessionSerializer,
    TrackingStationCreateResponseSerializer,
    TrackingStationSerializer,
)

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


class TrackingObservationUploadView(APIView):
    """Batch upload of manually-tagged pixel observations for one flight.
    The video itself never leaves the phone - only the tapped pixels and
    their timestamps, matching the existing clinometer Sample upload's
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

            frame, _ = FrameObservation.objects.update_or_create(
                station=station,
                flight=flight,
                frame_index=frame_index,
                defaults={
                    "session": station.session,
                    "local_timestamp_ms": local_timestamp_ms,
                    "image_width_px": image_width_px,
                    "image_height_px": image_height_px,
                },
            )
            frame_count += 1

            pixel_x = entry.get("pixel_x")
            pixel_y = entry.get("pixel_y")
            if pixel_x is not None and pixel_y is not None:
                PixelObservation.objects.filter(frame=frame, is_current=True).update(is_current=False)
                PixelObservation.objects.create(
                    frame=frame,
                    pixel_x=pixel_x,
                    pixel_y=pixel_y,
                    observation_source=PixelObservation.SOURCE_MANUAL,
                    valid=True,
                )
                pixel_count += 1

        return Response(
            {"frames": frame_count, "pixels": pixel_count}, status=status.HTTP_201_CREATED
        )
