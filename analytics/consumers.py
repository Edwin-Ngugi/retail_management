import json
from django.core.serializers.json import DjangoJSONEncoder
from channels.generic.websocket import AsyncWebsocketConsumer

class AnalyticsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.branch_id = self.scope["url_route"]["kwargs"]["branch_id"]
        self.room_group_name = f"analytics_branch_{self.branch_id}"

        print(f"--> [WS CONNECTING] Branch: {self.branch_id}, Group: {self.room_group_name}")

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()
        print(f"--> [WS ACCEPTED] Connected to {self.room_group_name}")

    async def disconnect(self, close_code):
        print(f"--> [WS DISCONNECTED] Code: {close_code}")

        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def analytics_update(self, event):
        print(f"--> [WS SENDING EVENT]: {event}")

        payload = {
            "type": "ANALYTICS_UPDATED",
            "event": event.get("event", "DATA_CHANGED"),
            "item_id": event.get("item_id"),
            "updated_item": event.get("updated_item"),
            "changes": event.get("changes"),
        }

        await self.send(
            text_data=json.dumps(payload, cls=DjangoJSONEncoder)
        )