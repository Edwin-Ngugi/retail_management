# analytics/routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Matches /ws/analytics/1/ or /ws/analytics/branch_name/
    re_path(r'^ws/analytics/(?P<branch_id>[^/]+)/$', consumers.AnalyticsConsumer.as_asgi()),
]