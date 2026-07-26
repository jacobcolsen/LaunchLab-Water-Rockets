from rest_framework import serializers

from .models import Launch, Result, Session, Station


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


class ResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = Result
        fields = ["id", "best_altitude_ft", "method", "station_breakdown", "computed_at"]
