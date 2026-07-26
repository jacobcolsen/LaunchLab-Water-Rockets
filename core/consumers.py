import json
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .models import Session, Station
from .realtime import broadcast_session_state, build_session_state


class SessionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"session_{self.session_id}"
        self.station_id = None

        query_string = self.scope.get("query_string", b"").decode()
        device_token = parse_qs(query_string).get("device_token", [None])[0]

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        state = await self.get_current_state()
        await self.send(text_data=json.dumps(state))

        if device_token:
            self.station_id = await self.set_station_ready(device_token, True)
            if self.station_id:
                await database_sync_to_async(broadcast_session_state)(self.session_id)

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if self.station_id:
            await self.set_station_ready_by_id(self.station_id, False)
            await database_sync_to_async(broadcast_session_state)(self.session_id)

    async def session_state(self, event):
        await self.send(text_data=json.dumps(event["state"]))

    @database_sync_to_async
    def get_current_state(self):
        session = Session.objects.get(pk=self.session_id)
        return build_session_state(session)

    @database_sync_to_async
    def set_station_ready(self, device_token, ready):
        station = Station.objects.filter(device_token=device_token).first()
        if station is None:
            return None
        station.ready = ready
        station.save(update_fields=["ready"])
        return station.id

    @database_sync_to_async
    def set_station_ready_by_id(self, station_id, ready):
        Station.objects.filter(pk=station_id).update(ready=ready)
