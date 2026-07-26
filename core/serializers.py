from django.utils import timezone
from rest_framework import serializers

from .models import Launch, Result, Session, Station

CONNECTION_STALE_SECONDS = 15


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Session
        fields = ["id", "name", "created_at"]


class StationSerializer(serializers.ModelSerializer):
    # Computed rather than a stored flag - "connected" is always freshly
    # judged from how recently last_seen_at was touched, so it self-heals
    # even if a disconnect signal never fires (e.g. the server process
    # itself got restarted mid-connection).
    connected = serializers.SerializerMethodField()

    class Meta:
        model = Station
        fields = ["id", "label", "distance_ft", "bearing_degrees", "connected", "created_at"]

    def get_connected(self, obj):
        if not obj.last_seen_at:
            return False
        age = (timezone.now() - obj.last_seen_at).total_seconds()
        return age < CONNECTION_STALE_SECONDS


class StationCreateResponseSerializer(StationSerializer):
    class Meta(StationSerializer.Meta):
        fields = StationSerializer.Meta.fields + ["device_token"]


class LaunchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Launch
        fields = ["id", "number", "name", "status", "launched_at", "landed_at"]


class ResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = Result
        fields = ["id", "best_altitude_ft", "method", "station_breakdown", "computed_at"]


class LaunchHistorySerializer(LaunchSerializer):
    # A SerializerMethodField (rather than a plain nested ResultSerializer)
    # so a launch with no computed Result yet still gets an explicit
    # "result": null in the output instead of DRF silently omitting the key
    # (a nested read-only field skips itself when the reverse OneToOne
    # lookup raises Result.DoesNotExist).
    result = serializers.SerializerMethodField()

    class Meta(LaunchSerializer.Meta):
        fields = LaunchSerializer.Meta.fields + ["result"]

    def get_result(self, obj):
        if hasattr(obj, "result"):
            return ResultSerializer(obj.result).data
        return None
