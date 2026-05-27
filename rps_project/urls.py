from django.contrib import admin
from django.urls import path
from rps.views import bale_webhook
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('bot/webhook/', bale_webhook), # آدرس وب‌هوک شما
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
