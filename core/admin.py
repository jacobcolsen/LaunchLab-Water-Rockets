from django.contrib import admin

from .models import Launch, Result, Sample, Session, Station


class StationInline(admin.TabularInline):
    model = Station
    extra = 1
    fields = ("label", "distance_ft", "bearing_degrees", "last_seen_at", "device_token")
    readonly_fields = ("device_token", "last_seen_at")


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    inlines = [StationInline]


class SampleInline(admin.TabularInline):
    model = Sample
    extra = 1
    fields = ("station", "data", "uploaded_at")
    readonly_fields = ("uploaded_at",)


class ResultInline(admin.StackedInline):
    model = Result
    extra = 0
    fields = ("best_altitude_ft", "method", "station_breakdown", "computed_at")
    readonly_fields = ("computed_at",)


@admin.register(Launch)
class LaunchAdmin(admin.ModelAdmin):
    list_display = ("session", "number", "name", "status", "launched_at", "landed_at")
    list_filter = ("session", "status")
    inlines = [SampleInline, ResultInline]


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("label", "session", "distance_ft", "bearing_degrees", "last_seen_at")
    list_filter = ("session",)


@admin.register(Sample)
class SampleAdmin(admin.ModelAdmin):
    list_display = ("launch", "station", "uploaded_at")


@admin.register(Result)
class ResultAdmin(admin.ModelAdmin):
    list_display = ("launch", "best_altitude_ft", "method", "computed_at")


# Parallel optical multi-camera tracking subsystem - see optical_admin.py.
from . import optical_admin  # noqa: E402,F401
