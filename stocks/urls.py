from django.urls import path
from .views import get_item_pricing

urlpatterns = [
    path('get-item-pricing/', get_item_pricing, name='get_item_pricing'),
]