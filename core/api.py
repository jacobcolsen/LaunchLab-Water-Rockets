from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Launch, Sample, Session, Station
from .realtime import broadcast_session_state
from .serializers import (
    LaunchSerializer,
    SessionSerializer,
    StationCreateResponseSerializer,
    StationSerializer,
)


class SessionCreateView(generics.CreateAPIView):
    queryset = Session.objects.all()
    serializer_class = SessionSerializer
    authentication_classes = []
    permission_classes = [AllowAny]


class StationListCreateView(generics.ListCreateAPIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        return Station.objects.filter(session_id=self.kwargs["session_id"])

    def get_serializer_class(self):
        if self.request.method == "POST":
            return StationCreateResponseSerializer
        return StationSerializer

    def perform_create(self, serializer):
        session = get_object_or_404(Session, pk=self.kwargs["session_id"])
        serializer.save(session=session, ready=True)
        broadcast_session_state(session.id)


class LaunchCreateView(generics.CreateAPIView):
    serializer_class = LaunchSerializer
    authentication_classes = []
    permission_classes = [AllowAny]

    def perform_create(self, serializer):
        session = get_object_or_404(Session, pk=self.kwargs["session_id"])
        serializer.save(session=session)
        broadcast_session_state(session.id)


class LaunchMarkLaunchedView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, launch_id):
        launch = get_object_or_404(Launch, pk=launch_id)
        launch.status = Launch.STATUS_LAUNCHED
        launch.launched_at = timezone.now()
        launch.save()
        broadcast_session_state(launch.session_id)
        return Response(LaunchSerializer(launch).data, status=status.HTTP_200_OK)


class LaunchMarkLandedView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, launch_id):
        launch = get_object_or_404(Launch, pk=launch_id)
        launch.status = Launch.STATUS_LANDED
        launch.landed_at = timezone.now()
        launch.save()
        broadcast_session_state(launch.session_id)
        return Response(LaunchSerializer(launch).data, status=status.HTTP_200_OK)


class SampleUploadView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        device_token = request.data.get("device_token")
        launch_id = request.data.get("launch")
        data = request.data.get("data")

        if not device_token or not launch_id or data is None:
            return Response(
                {"detail": "device_token, launch, and data are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        station = get_object_or_404(Station, device_token=device_token)
        launch = get_object_or_404(Launch, pk=launch_id)

        sample, created = Sample.objects.update_or_create(
            launch=launch, station=station, defaults={"data": data}
        )
        return Response(
            {"id": sample.id, "count": len(data)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
