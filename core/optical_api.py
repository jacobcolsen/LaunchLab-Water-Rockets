from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .optical_models import TrackingSession, TrackingStation
from .optical_serializers import (
    TrackingSessionSerializer,
    TrackingStationCreateResponseSerializer,
    TrackingStationSerializer,
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
