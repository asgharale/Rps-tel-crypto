# rps/urls.py
from django.urls import path
from .views import tg_webhook

urlpatterns = [
    path('webhook/', tg_webhook),
]