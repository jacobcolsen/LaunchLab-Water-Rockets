from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.permissions import AllowAny

from .models import Session, Station
from .serializers import SessionSerializer, StationCreateResponseSerializer, StationSerializer


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
