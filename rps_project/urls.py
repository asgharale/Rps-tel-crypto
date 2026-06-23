# rps_project/urls.py
from django.contrib import admin
from django.urls import path
from rps.views import bale_webhook
from rps.tg_webhook import tg_webhook
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('bot/webhook/', bale_webhook),       # Bale webhook
    path('tg/webhook/',  tg_webhook),         # Telegram admin callbacks
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)