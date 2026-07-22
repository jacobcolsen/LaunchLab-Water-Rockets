import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .models import Session
from .realtime import build_session_state


class SessionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.session_id = self.scope["url_route"]["kwargs"]["session_id"]
        self.group_name = f"session_{self.session_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        state = await self.get_current_state()
        await self.send(text_data=json.dumps(state))

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def session_state(self, event):
        await self.send(text_data=json.dumps(event["state"]))

    @database_sync_to_async
    def get_current_state(self):
        session = Session.objects.get(pk=self.session_id)
        return build_session_state(session)
