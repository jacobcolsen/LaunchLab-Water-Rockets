from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import Session
from .serializers import LaunchSerializer, ResultSerializer, StationSerializer


def build_session_state(session: Session) -> dict:
    stations = StationSerializer(session.stations.all(), many=True).data
    latest_launch = session.launches.order_by("-number").first()
    launch = LaunchSerializer(latest_launch).data if latest_launch else None
    result = None
    if latest_launch and hasattr(latest_launch, "result"):
        result = ResultSerializer(latest_launch.result).data
    return {"stations": stations, "launch": launch, "result": result}


def broadcast_session_state(session_id: int) -> None:
    session = Session.objects.get(pk=session_id)
    state = build_session_state(session)
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f"session_{session_id}",
        {"type": "session.state", "state": state},
    )
