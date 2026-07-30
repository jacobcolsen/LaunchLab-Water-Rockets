"""
URL configuration for launchlab project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path

from core.api import (
    LaunchDebriefView,
    LaunchListCreateView,
    LaunchMarkLandedView,
    LaunchMarkLaunchedView,
    SampleUploadView,
    SessionComparisonView,
    SessionCreateView,
    StationListCreateView,
    StationRecalibrateView,
)
from core.optical_api import (
    TrackingFlightDebriefView,
    TrackingFlightListCreateView,
    TrackingFlightSummaryView,
    TrackingObservationUploadView,
    TrackingSessionCreateView,
    TrackingSessionExportView,
    TrackingStationCalibrationRefineView,
    TrackingStationCalibrationView,
    TrackingStationClockSyncView,
    TrackingStationListCreateView,
    TrackingStationPositionView,
    server_time_view,
)
from core.optical_views import (
    optical_control_view,
    optical_station_qr_view,
    optical_station_view,
)
from core.views import control_view, health, station_qr_view, station_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health', health, name='health'),
    path('control', control_view, name='control'),
    path('station', station_view, name='station'),
    path(
        'sessions/<int:session_id>/qr.png',
        station_qr_view,
        name='station-qr',
    ),
    path('api/sessions/', SessionCreateView.as_view(), name='api-session-create'),
    path(
        'api/sessions/<int:session_id>/stations/',
        StationListCreateView.as_view(),
        name='api-station-list-create',
    ),
    path(
        'api/sessions/<int:session_id>/launches/',
        LaunchListCreateView.as_view(),
        name='api-launch-list-create',
    ),
    path(
        'api/launches/<int:launch_id>/launched/',
        LaunchMarkLaunchedView.as_view(),
        name='api-launch-launched',
    ),
    path(
        'api/launches/<int:launch_id>/landed/',
        LaunchMarkLandedView.as_view(),
        name='api-launch-landed',
    ),
    path(
        'api/launches/<int:launch_id>/debrief/',
        LaunchDebriefView.as_view(),
        name='api-launch-debrief',
    ),
    path('api/samples/', SampleUploadView.as_view(), name='api-sample-upload'),
    path(
        'api/sessions/<int:session_id>/comparison/',
        SessionComparisonView.as_view(),
        name='api-session-comparison',
    ),
    path(
        'api/stations/recalibrate/',
        StationRecalibrateView.as_view(),
        name='api-station-recalibrate',
    ),
    path('optical/control', optical_control_view, name='optical-control'),
    path('optical/station', optical_station_view, name='optical-station'),
    path(
        'optical/sessions/<int:session_id>/qr.png',
        optical_station_qr_view,
        name='optical-station-qr',
    ),
    path(
        'api/optical/sessions/',
        TrackingSessionCreateView.as_view(),
        name='api-optical-session-create',
    ),
    path(
        'api/optical/sessions/<int:session_id>/stations/',
        TrackingStationListCreateView.as_view(),
        name='api-optical-station-list-create',
    ),
    path(
        'api/optical/stations/position/',
        TrackingStationPositionView.as_view(),
        name='api-optical-station-position',
    ),
    path(
        'api/optical/stations/calibration/',
        TrackingStationCalibrationView.as_view(),
        name='api-optical-station-calibration',
    ),
    path(
        'api/optical/stations/calibration/refine/',
        TrackingStationCalibrationRefineView.as_view(),
        name='api-optical-station-calibration-refine',
    ),
    path(
        'api/optical/sessions/<int:session_id>/flights/',
        TrackingFlightListCreateView.as_view(),
        name='api-optical-flight-list-create',
    ),
    path(
        'api/optical/stations/observations/',
        TrackingObservationUploadView.as_view(),
        name='api-optical-observation-upload',
    ),
    path('api/optical/server-time/', server_time_view, name='api-optical-server-time'),
    path(
        'api/optical/stations/clock-sync/',
        TrackingStationClockSyncView.as_view(),
        name='api-optical-station-clock-sync',
    ),
    path(
        'api/optical/flights/<int:flight_id>/debrief/',
        TrackingFlightDebriefView.as_view(),
        name='api-optical-flight-debrief',
    ),
    path(
        'api/optical/flights/<int:flight_id>/summary/',
        TrackingFlightSummaryView.as_view(),
        name='api-optical-flight-summary',
    ),
    path(
        'api/optical/sessions/<int:session_id>/export/',
        TrackingSessionExportView.as_view(),
        name='api-optical-session-export',
    ),
]
