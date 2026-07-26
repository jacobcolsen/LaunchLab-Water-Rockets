import asyncio
import json
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .models import Session, Station
from .realtime import broadcast_session_state, build_session_state

HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 15


class SessionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"session_{self.session_id}"
        self.station_id = None
        self.heartbeat_task = None

        query_string = self.scope.get("query_string", b"").decode()
        device_token = parse_qs(query_string).get("device_token", [None])[0]

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        state = await self.get_current_state()
        await self.send(text_data=json.dumps(state))

        if device_token:
            self.station_id = await self.touch_station(device_token=device_token)
            if self.station_id:
                await database_sync_to_async(broadcast_session_state)(self.session_id)
                # This in-memory clock is only for THIS connection's own
                # ping/timeout decision - the source of truth other clients
                # see (last_seen_at) is persisted to the DB below on every
                # pong, so it survives even if this process gets killed.
                self.last_pong = time.monotonic()
                self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())

    async def disconnect(self, close_code):
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if self.station_id:
            await self.clear_station_last_seen(self.station_id)
            await database_sync_to_async(broadcast_session_state)(self.session_id)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return
        if payload.get("type") == "pong" and self.station_id:
            self.last_pong = time.monotonic()
            await self.touch_station(station_id=self.station_id)

    async def heartbeat_loop(self):
        # Detects a phone that vanished without a clean WebSocket close (app
        # swiped away, network dropped) - a plain TCP-level timeout for that
        # can take a very long time, so this pings on a short interval and
        # closes the connection itself once a station stops answering,
        # letting the normal disconnect() cleanup take it from there. This
        # is a nice-to-have for tidying up dead connections promptly - the
        # roster's actual "connected" status no longer depends on it firing
        # correctly, since that's now computed from last_seen_at's age.
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if time.monotonic() - self.last_pong > HEARTBEAT_TIMEOUT_SECONDS:
                    await self.close(code=4000)
                    return
                await self.send(text_data=json.dumps({"type": "ping"}))
        except asyncio.CancelledError:
            pass

    async def session_state(self, event):
        await self.send(text_data=json.dumps(event["state"]))

    @database_sync_to_async
    def get_current_state(self):
        session = Session.objects.get(pk=self.session_id)
        return build_session_state(session)

    @database_sync_to_async
    def touch_station(self, device_token=None, station_id=None):
        """Set last_seen_at = now() for a station, identified by either its
        device_token (on initial connect) or its id (on later pongs).
        Returns the station's id, or None if device_token didn't match one."""
        if device_token is not None:
            station = Station.objects.filter(device_token=device_token).first()
            if station is None:
                return None
        else:
            station = Station.objects.filter(pk=station_id).first()
            if station is None:
                return None
        station.last_seen_at = timezone.now()
        station.save(update_fields=["last_seen_at"])
        return station.id

    @database_sync_to_async
    def clear_station_last_seen(self, station_id):
        Station.objects.filter(pk=station_id).update(last_seen_at=None)
