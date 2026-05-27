from django.urls import path
from .views import handle_bot_request

urlpatterns = [
    path('webhook/', handle_bot_request),
]
