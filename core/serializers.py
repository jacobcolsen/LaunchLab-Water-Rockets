from rest_framework import serializers

from .models import Launch, Session, Station


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Session
        fields = ["id", "name", "created_at"]


class StationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Station
        fields = ["id", "label", "distance_ft", "bearing_degrees", "ready", "created_at"]


class StationCreateResponseSerializer(StationSerializer):
    class Meta(StationSerializer.Meta):
        fields = StationSerializer.Meta.fields + ["device_token"]


class LaunchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Launch
        fields = ["id", "number", "status", "launched_at", "landed_at"]
