import os
from django.core.asgi import get_asgi_application

# Ensure this matches your actual Django project package name
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

# Initialize Django ASGI application early to ensure AppRegistry is populated
# before importing code that may use Django models/settings.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

import analytics.routing  # Import your WebSocket routing module

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                analytics.routing.websocket_urlpatterns
            )
        )
    ),
})