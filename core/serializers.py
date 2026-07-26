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
