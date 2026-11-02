import json
from django.core.serializers.json import DjangoJSONEncoder
from channels.generic.websocket import AsyncWebsocketConsumer

class AnalyticsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.branch_id = self.scope["url_route"]["kwargs"]["branch_id"]
        
        # Define both channel group targets
        self.analytics_group = f"analytics_branch_{self.branch_id}"
        self.pos_group = f"branch_{self.branch_id}"

        # Join analytics channel group
        await self.channel_layer.group_add(
            self.analytics_group,
            self.channel_name
        )

        # Join POS channel group
        await self.channel_layer.group_add(
            self.pos_group,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        # Leave analytics group
        await self.channel_layer.group_discard(
            self.analytics_group,
            self.channel_name
        )

        # Leave POS group
        await self.channel_layer.group_discard(
            self.pos_group,
            self.channel_name
        )

    async def analytics_update(self, event):
        """
        Forwards the full event payload sent by signals.py directly to the frontend.
        """
        # Exclude internal channel layer message key
        payload = {k: v for k, v in event.items() if k != "type"}

        # Normalize action/event field for frontend listeners
        if "action" not in payload and "event" in payload:
            payload["action"] = payload["event"]

        await self.send(
            text_data=json.dumps(payload, cls=DjangoJSONEncoder)
        )