from django.urls import re_path

from . import consumers
from . import optical_consumers

websocket_urlpatterns = [
    re_path(r"ws/sessions/(?P<session_id>\d+)/$", consumers.SessionConsumer.as_asgi()),
    re_path(
        r"ws/optical/sessions/(?P<session_id>\d+)/$",
        optical_consumers.TrackingSessionConsumer.as_asgi(),
    ),
]
