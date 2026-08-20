"""WebSocket relay + live presence for the optical subsystem - lets the
control page's New Flight ceremony (prepare -> station check-in ->
countdown -> touchdown) play out live on every connected station's
phone, in sync, and lets the control page's roster show a real
connected/disconnected status per station. Mirrors two patterns already
proven for the clinometer (core.consumers.SessionConsumer): the
countdown_start relay, and the heartbeat/last_seen_at presence tracking -
but kept in its own file/group namespace since the optical subsystem
stays decoupled from the clinometer (its own TrackingSession/
TrackingStation, not the clinometer's Session/Station).

Unlike the clinometer's consumer, this one doesn't know which station a
connection belongs to at connect time - the optical station page opens
this socket immediately on page load (to receive the countdown/ready/
touchdown ceremony even before a position exists yet), so identity
arrives later via an explicit `identify` message once the station has a
device_token, rather than a `?device_token=` query string at connect.
No WS-pushed roster state either - the control page already polls the
roster REST endpoint every 5s, so `connected` just needs to be backed by
a fresh last_seen_at for that polling to pick up.

Recording state also persists server-side (TrackingSession.live_flight_
started_at, set on countdown_start and cleared on flight_landed) so a
station that reconnects/re-identifies mid-flight - after missing the
original broadcast entirely - can be told directly whether it should
currently be recording, via a `flight_state_sync` reply sent only to
that one socket, rather than a fire-and-forget group message it may
never have received.
"""
import asyncio
import json
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .basic_auth import check_basic_auth
from .optical_models import TrackingSession, TrackingStation

HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 15

RELAYED_MESSAGE_TYPES = {"countdown_start", "flight_prep_start", "station_ready", "flight_landed"}


class TrackingSessionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Django's MIDDLEWARE (including BasicAuthMiddleware) never runs for
        # this connection - the websocket protocol is routed straight to
        # this consumer in launchlab/asgi.py, bypassing django_asgi_app
        # entirely - so the same shared-password check needs to happen here.
        headers = dict(self.scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        if not check_basic_auth(auth_header):
            await self.close(code=4401)
            return

        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"optical_session_{self.session_id}"
        self.station_id = None
        self.heartbeat_task = None

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        if self.station_id:
            await self.clear_station_last_seen(self.station_id)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return
        message_type = payload.get("type")

        if message_type == "identify":
            device_token = payload.get("device_token")
            if device_token:
                self.station_id = await self.touch_station(device_token=device_token)
                if self.station_id and not self.heartbeat_task:
                    self.last_pong = time.monotonic()
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
                if self.station_id:
                    # Catches up a reconnecting/late-identifying station on
                    # whether a flight is currently live, regardless of
                    # whether it was connected in time for the original
                    # countdown_start/flight_landed broadcast - the station
                    # side's autoStartRecording/autoStopRecording are both
                    # idempotent no-ops if it's already in the right state.
                    recording_active = await self.get_live_flight_started_at() is not None
                    await self.send(text_data=json.dumps(
                        {"type": "flight_state_sync", "recording_active": recording_active}
                    ))
            return

        if message_type == "pong" and self.station_id:
            self.last_pong = time.monotonic()
            await self.touch_station(station_id=self.station_id)
            return

        if message_type in RELAYED_MESSAGE_TYPES:
            if message_type == "countdown_start":
                await self.set_live_flight_started_at(timezone.now())
            elif message_type == "flight_landed":
                await self.set_live_flight_started_at(None)
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "relay.message", "payload": payload},
            )

    async def relay_message(self, event):
        await self.send(text_data=json.dumps(event["payload"]))

    async def heartbeat_loop(self):
        # Detects a phone that vanished without a clean WebSocket close (app
        # swiped away, network dropped) - a plain TCP-level timeout for that
        # can take a very long time, so this pings on a short interval and
        # closes the connection itself once a station stops answering,
        # letting the normal disconnect() cleanup take it from there. The
        # roster's "connected" status doesn't depend on this firing
        # correctly either way, since that's computed from last_seen_at's
        # age (core/optical_serializers.py).
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if time.monotonic() - self.last_pong > HEARTBEAT_TIMEOUT_SECONDS:
                    await self.close(code=4000)
                    return
                await self.send(text_data=json.dumps({"type": "ping"}))
        except asyncio.CancelledError:
            pass

    @database_sync_to_async
    def touch_station(self, device_token=None, station_id=None):
        """Set last_seen_at = now() for a station, identified by either its
        device_token (on identify) or its id (on later pongs). Returns the
        station's id, or None if device_token didn't match one."""
        if device_token is not None:
            station = TrackingStation.objects.filter(device_token=device_token).first()
            if station is None:
                return None
        else:
            station = TrackingStation.objects.filter(pk=station_id).first()
            if station is None:
                return None
        station.last_seen_at = timezone.now()
        station.save(update_fields=["last_seen_at"])
        return station.id

    @database_sync_to_async
    def clear_station_last_seen(self, station_id):
        TrackingStation.objects.filter(pk=station_id).update(last_seen_at=None)

    @database_sync_to_async
    def set_live_flight_started_at(self, value):
        TrackingSession.objects.filter(pk=self.session_id).update(live_flight_started_at=value)

    @database_sync_to_async
    def get_live_flight_started_at(self):
        session = TrackingSession.objects.filter(pk=self.session_id).first()
        return session.live_flight_started_at if session else None
