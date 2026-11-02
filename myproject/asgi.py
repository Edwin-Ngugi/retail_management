import os
from django.core.asgi import get_asgi_application

# 1. Update this to match your exact settings package
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

import analytics.routing

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