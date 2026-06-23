"""
tg_webhook.py  –  Handles Telegram admin callbacks (Verify / Unverify crypto deposits)

Add to your project's urls.py:
    from rps.tg_webhook import tg_webhook
    path('tg/webhook/', tg_webhook),

Set up the Telegram webhook once:
    curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
         -d "url=https://yourdomain.com/rps/tg/webhook/"
"""

import json
import requests
import os
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")


def _tg_answer_callback(callback_query_id: str, text: str, show_alert=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    requests.post(url, json={
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": show_alert,
    }, timeout=5)


def _tg_edit_caption(chat_id, message_id, new_caption: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageCaption"
    requests.post(url, json={
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": new_caption,
        "parse_mode": "Markdown",
    }, timeout=5)


def _tg_edit_text(chat_id, message_id, new_text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    requests.post(url, json={
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "Markdown",
    }, timeout=5)


@csrf_exempt
def tg_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    try:
        update = json.loads(request.body.decode('utf-8'))
        callback = update.get("callback_query")

        if not callback:
            return HttpResponse(status=200)

        # ── Security: only admin can press these buttons ──────────────────────
        from_id = str(callback["from"]["id"])
        if from_id != str(TELEGRAM_ADMIN_CHAT_ID):
            _tg_answer_callback(callback["id"], "⛔ دسترسی ندارید.", show_alert=True)
            return HttpResponse(status=200)

        data = callback.get("data", "")
        message = callback.get("message", {})
        msg_id = message.get("message_id")
        chat_id = message.get("chat", {}).get("id")

        # ── crypto_verify_<id>  or  crypto_unverify_<id> ─────────────────────
        if data.startswith("crypto_verify_") or data.startswith("crypto_unverify_"):
            from rps.models import CryptoDepositRequest
            from rps.bale_api import send_message as bale_send

            action = "verify" if data.startswith("crypto_verify_") else "unverify"
            deposit_id = int(data.split("_")[-1])

            try:
                deposit = CryptoDepositRequest.objects.select_related('user').get(pk=deposit_id)
            except CryptoDepositRequest.DoesNotExist:
                _tg_answer_callback(callback["id"], "❌ درخواست یافت نشد.", show_alert=True)
                return HttpResponse(status=200)

            if deposit.status != 'pending':
                _tg_answer_callback(
                    callback["id"],
                    f"این درخواست قبلاً {deposit.status} شده است.",
                    show_alert=True,
                )
                return HttpResponse(status=200)

            if action == "verify":
                deposit.approve()
                status_label = "✅ تایید شد"
                user_msg = (
                    f"✅ *واریز کریپتو تایید شد!*\n\n"
                    f"🪙 ارز: {deposit.coin}\n"
                    f"💵 مبلغ: *{deposit.amount:,} تومان* به موجودی شما اضافه شد.\n"
                    f"🔖 شماره پیگیری: `#{deposit.pk}`"
                )
            else:
                deposit.reject()
                status_label = "❌ رد شد"
                user_msg = (
                    f"❌ *واریز کریپتو رد شد*\n\n"
                    f"🔖 شماره پیگیری: `#{deposit.pk}`\n"
                    "در صورت نیاز با پشتیبانی تماس بگیرید."
                )

            # Notify the user in Bale
            bale_send(deposit.user.chat_id, user_msg)

            # Update admin's Telegram message
            suffix = f"\n\n─────\n👮 وضعیت: *{status_label}*"
            try:
                if message.get("photo"):
                    original = message.get("caption", "")
                    _tg_edit_caption(chat_id, msg_id, original + suffix)
                else:
                    original = message.get("text", "")
                    _tg_edit_text(chat_id, msg_id, original + suffix)
            except Exception:
                pass

            _tg_answer_callback(callback["id"], f"{status_label}!", show_alert=False)

    except Exception as e:
        print(f"tg_webhook error: {e}")

    return HttpResponse(status=200)