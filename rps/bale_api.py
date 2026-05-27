import requests
from django.conf import settings


def send_message_direct(chat_id, text, reply_markup=None):
    """Direct blocking HTTP call — used only by the Celery worker."""
    url = f"https://tapi.bale.ai/bot{settings.BALE_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"API Error: {e}")
        return None


def send_message(chat_id, text, reply_markup=None):
    """Queue the message through Celery/Redis. Use this everywhere in logic.py."""
    from rps.tasks import send_message_task
    send_message_task.delay(chat_id, text, reply_markup)


def get_updates(offset=0):
    url = f"https://tapi.bale.ai/bot{settings.BALE_TOKEN}/getUpdates"
    params = {"offset": offset, "limit": 100, "timeout": 20}
    try:
        response = requests.get(url, params=params, timeout=25)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        print(f"Error getting updates: {e}")
        return None