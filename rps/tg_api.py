"""
tg_api.py  –  Telegram Bot API helpers.

Blocking (*_direct) functions are used by Celery workers.
Async wrappers (send_message, etc.) queue via Celery.
"""

import requests
from django.conf import settings


def _tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_TOKEN}/{method}"


# ─── Blocking calls (used by Celery workers) ──────────────────────────────────

def send_message_direct(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(_tg_url("sendMessage"), json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"send_message_direct error: {e}")
        return None


def send_photo_direct(chat_id, photo, caption=None, reply_markup=None):
    payload = {"chat_id": chat_id, "photo": photo, "parse_mode": "Markdown"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(_tg_url("sendPhoto"), json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"send_photo_direct error: {e}")
        return None


def edit_message_direct(chat_id, message_id, new_text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(_tg_url("editMessageText"), json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"edit_message_direct error: {e}")
        return None


def answer_callback_direct(callback_query_id, text, show_alert=False):
    try:
        r = requests.post(_tg_url("answerCallbackQuery"), json={
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }, timeout=5)
        return r.json()
    except Exception as e:
        print(f"answer_callback_direct error: {e}")
        return None


def kick_chat_member_direct(chat_id: int, user_id: int):
    """Ban user from bot by blocking with Telegram API (requires group bot usage).
    For a private bot, we just set is_banned=True in DB.
    This can also be used if your bot is admin in a channel/group."""
    try:
        r = requests.post(_tg_url("banChatMember"), json={
            "chat_id": chat_id,
            "user_id": user_id,
        }, timeout=5)
        return r.json()
    except Exception as e:
        print(f"kick_chat_member_direct error: {e}")
        return None


def edit_message_caption_direct(chat_id, message_id, new_caption, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": new_caption,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(_tg_url("editMessageCaption"), json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"edit_message_caption_direct error: {e}")
        return None


def download_tg_file(file_id: str) -> bytes | None:
    try:
        res = requests.get(_tg_url(f"getFile?file_id={file_id}"), timeout=10)
        if res.status_code != 200:
            return None
        data = res.json()
        if not data.get("ok"):
            return None
        file_path = data["result"]["file_path"]
        dl_url = f"https://api.telegram.org/file/bot{settings.TELEGRAM_TOKEN}/{file_path}"
        file_res = requests.get(dl_url, timeout=20)
        if file_res.status_code == 200:
            return file_res.content
    except Exception as e:
        print(f"download_tg_file error: {e}")
    return None


def set_webhook(url: str) -> dict:
    try:
        r = requests.post(_tg_url("setWebhook"), json={
            "url": url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        }, timeout=10)
        return r.json()
    except Exception as e:
        print(f"set_webhook error: {e}")
        return {}


# ─── Async wrappers (queue via Celery) ────────────────────────────────────────

def send_message(chat_id, text, reply_markup=None):
    from rps.tasks import send_message_task
    send_message_task.delay(chat_id, text, reply_markup)


def edit_message(chat_id, message_id, new_text, reply_markup=None):
    from rps.tasks import edit_message_task
    edit_message_task.delay(chat_id, message_id, new_text, reply_markup)


def answer_callback(callback_id, text, show_alert=False):
    from rps.tasks import answer_callback_task
    answer_callback_task.delay(callback_id, text, show_alert)