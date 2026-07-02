"""
views.py  –  Telegram webhook handler.

Routes:
  POST /bot/webhook/  →  tg_webhook()
    ├── callback_query  →  handle_callback()
    └── message         →  logic.handle_bot_logic()
"""

import json
import os
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from rps.logic import handle_bot_logic, handle_ttt_move, _is_admin, _fmt
from rps.tg_api import (
    answer_callback_direct,
    edit_message_direct,
    edit_message_caption_direct,
    send_message_direct,
)
from rps.keyboards import (
    main_menu, admin_report_inline_kb, admin_withdrawal_inline_kb, admin_deposit_inline_kb,
    friend_req_inline_kb, game_invite_inline_kb,
)

ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "8093967783")


# ─── Message extraction ───────────────────────────────────────────────────────

def extract_message_data(update: dict):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    first_name = message.get("from", {}).get("first_name", "کاربر")
    username = message.get("from", {}).get("username")
    photo_id = None
    text = ""
    if "photo" in message:
        photo_id = message["photo"][-1]["file_id"]
        text = message.get("caption", "")
    else:
        text = message.get("text", "")
    return chat_id, text, first_name, photo_id, username


# ─── Callback handler ─────────────────────────────────────────────────────────

def handle_callback(callback: dict) -> HttpResponse:
    from_id = callback.get("from", {}).get("id")
    cb_id   = callback.get("id")
    data    = callback.get("data", "")
    message = callback.get("message", {})
    msg_id  = message.get("message_id")
    chat_id = message.get("chat", {}).get("id")

    # ── Connect Four move ─────────────────────────────────────────────────────
    if data.startswith("c4f_"):
        return _handle_c4f_callback(cb_id, from_id, data)

    # ── Tic-Tac-Toe move ──────────────────────────────────────────────────────
    if data.startswith("ttt_"):
        return _handle_ttt_callback(cb_id, from_id, data)

    # ── Minesweeper reveal ────────────────────────────────────────────────────
    if data.startswith("ms_"):
        return _handle_ms_callback(cb_id, from_id, data)

    # ── Broadcast cancel ──────────────────────────────────────────────────────
    if data.startswith("bcast_cancel_"):
        return _handle_broadcast_cancel(cb_id, from_id, data)

    # ── Crypto deposit admin ───────────────────────────────────────────────────
    if data.startswith("crypto_verify_") or data.startswith("crypto_reject_"):
        return _handle_crypto_admin(cb_id, from_id, chat_id, msg_id, message, data)

    # ── Card deposit admin ─────────────────────────────────────────────────────
    if data.startswith("deposit_verify_") or data.startswith("deposit_reject_"):
        return _handle_deposit_admin(cb_id, from_id, chat_id, msg_id, message, data)

    # ── Withdrawal admin ───────────────────────────────────────────────────────
    if data.startswith("withdraw_paid_") or data.startswith("withdraw_reject_"):
        return _handle_withdrawal_admin(cb_id, from_id, chat_id, msg_id, message, data)

    # ── Report admin ───────────────────────────────────────────────────────────
    if data.startswith("report_ignore_") or data.startswith("report_ban_"):
        return _handle_report_admin(cb_id, from_id, chat_id, msg_id, message, data)

    # ── Friend request ─────────────────────────────────────────────────────────
    if data.startswith("freq_accept_") or data.startswith("freq_reject_"):
        return _handle_friend_request(cb_id, from_id, data)

    # ── Game invite ───────────────────────────────────────────────────────────
    if data.startswith("gameinv_accept_") or data.startswith("gameinv_reject_"):
        return _handle_game_invite(cb_id, from_id, data)

    answer_callback_direct(cb_id, "⚠️ دستور نامشخص", show_alert=True)
    return HttpResponse(status=200)


def _require_admin(cb_id, from_id) -> bool:
    if str(from_id) != str(ADMIN_CHAT_ID):
        answer_callback_direct(cb_id, "⛔ دسترسی ندارید.", show_alert=True)
        return False
    return True


def _handle_c4f_callback(cb_id, from_id, data):
    from rps.models import GameMatch, BotUser
    from rps.logic import handle_c4f_move

    parts = data.split("_")
    # format: c4f_{match_id}_{col}  OR  c4f_{match_id}_full
    if len(parts) != 3:
        answer_callback_direct(cb_id, "خطا", show_alert=True)
        return HttpResponse(status=200)

    match_id = int(parts[1])
    col_str  = parts[2]

    if col_str == 'full':
        answer_callback_direct(cb_id, "🚫 این ستون پر است!", show_alert=False)
        return HttpResponse(status=200)

    col = int(col_str)
    if not (0 <= col <= 6):
        answer_callback_direct(cb_id, "⚠️ ستون نامعتبر", show_alert=True)
        return HttpResponse(status=200)

    try:
        match  = GameMatch.objects.select_related('player1', 'player2').get(pk=match_id)
        player = BotUser.objects.get(chat_id=from_id)
    except (GameMatch.DoesNotExist, BotUser.DoesNotExist):
        answer_callback_direct(cb_id, "❌ بازی یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    result = handle_c4f_move(match, player, col)

    if result == 'not_your_turn':
        answer_callback_direct(cb_id, "⏳ نوبت شما نیست!", show_alert=False)
    elif result == 'col_full':
        answer_callback_direct(cb_id, "🚫 این ستون پر است!", show_alert=False)
    elif result == 'already_done':
        answer_callback_direct(cb_id, "✅ بازی تمام شده است.", show_alert=False)
    else:
        answer_callback_direct(cb_id, "✅")

    return HttpResponse(status=200)


# ── TTT callback ──────────────────────────────────────────────────────────────

def _handle_ttt_callback(cb_id, from_id, data):
    from rps.models import GameMatch, BotUser
    parts = data.split("_")
    if len(parts) != 3:
        answer_callback_direct(cb_id, "خطا", show_alert=True)
        return HttpResponse(status=200)

    match_id = int(parts[1])
    position = int(parts[2])

    try:
        match = GameMatch.objects.select_related('player1', 'player2').get(pk=match_id)
        player = BotUser.objects.get(chat_id=from_id)
    except (GameMatch.DoesNotExist, BotUser.DoesNotExist):
        answer_callback_direct(cb_id, "❌ بازی یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    result = handle_ttt_move(match, player, position)

    if result == 'not_your_turn':
        answer_callback_direct(cb_id, "⏳ نوبت شما نیست!", show_alert=False)
    elif result == 'invalid':
        answer_callback_direct(cb_id, "⚠️ این خانه قبلاً پر شده است!", show_alert=False)
    elif result == 'already_done':
        answer_callback_direct(cb_id, "✅ بازی تمام شده است.", show_alert=False)
    else:
        answer_callback_direct(cb_id, "✅")

    return HttpResponse(status=200)


# ── Minesweeper callback ──────────────────────────────────────────────────────

def _handle_ms_callback(cb_id, from_id, data):
    from rps.models import GameMatch, BotUser
    from rps.logic import handle_ms_move

    parts = data.split("_")
    # format: ms_{match_id}_{index}
    if len(parts) != 3:
        answer_callback_direct(cb_id, "خطا", show_alert=True)
        return HttpResponse(status=200)

    match_id = int(parts[1])
    index = int(parts[2])

    try:
        match = GameMatch.objects.select_related('player1').get(pk=match_id)
        player = BotUser.objects.get(chat_id=from_id)
    except (GameMatch.DoesNotExist, BotUser.DoesNotExist):
        answer_callback_direct(cb_id, "❌ بازی یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    result = handle_ms_move(match, player, index)

    if result == 'mine':
        answer_callback_direct(cb_id, "💥 به مین خوردید!", show_alert=True)
    elif result == 'win':
        answer_callback_direct(cb_id, "🎉 بردید!", show_alert=True)
    elif result == 'already':
        answer_callback_direct(cb_id, "این خانه قبلاً باز شده.", show_alert=False)
    elif result == 'already_done':
        answer_callback_direct(cb_id, "✅ بازی تمام شده است.", show_alert=False)
    elif result == 'invalid':
        answer_callback_direct(cb_id, "⚠️ این بازی شما نیست.", show_alert=True)
    else:
        answer_callback_direct(cb_id, "✅")

    return HttpResponse(status=200)


# ── Broadcast cancel callback ─────────────────────────────────────────────────

def _handle_broadcast_cancel(cb_id, from_id, data):
    if not _require_admin(cb_id, from_id):
        return HttpResponse(status=200)

    from rps.models import BroadcastJob
    job_id = int(data.split("_")[-1])

    try:
        job = BroadcastJob.objects.get(pk=job_id)
    except BroadcastJob.DoesNotExist:
        answer_callback_direct(cb_id, "❌ یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if job.status != 'running':
        answer_callback_direct(cb_id, f"این ارسال قبلاً {job.status} شده.", show_alert=True)
        return HttpResponse(status=200)

    job.status = 'cancelled'
    job.save(update_fields=['status'])
    answer_callback_direct(cb_id, "⛔️ درخواست لغو ثبت شد.", show_alert=True)
    return HttpResponse(status=200)


# ── Admin: crypto deposit ──────────────────────────────────────────────────────

def _handle_crypto_admin(cb_id, from_id, chat_id, msg_id, message, data):
    if not _require_admin(cb_id, from_id):
        return HttpResponse(status=200)

    from rps.models import CryptoDepositRequest
    action = "verify" if data.startswith("crypto_verify_") else "reject"
    dep_id = int(data.split("_")[-1])

    try:
        dep = CryptoDepositRequest.objects.select_related('user').get(pk=dep_id)
    except CryptoDepositRequest.DoesNotExist:
        answer_callback_direct(cb_id, "❌ یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if dep.status != 'pending':
        answer_callback_direct(cb_id, f"قبلاً {dep.status} شده.", show_alert=True)
        return HttpResponse(status=200)

    if action == "verify":
        dep.approve()
        label = "✅ تایید شد"
    else:
        dep.reject()
        label = "❌ رد شد"

    _patch_admin_message(chat_id, msg_id, message, label)
    answer_callback_direct(cb_id, label)
    return HttpResponse(status=200)


# ── Admin: card deposit ────────────────────────────────────────────────────────

def _handle_deposit_admin(cb_id, from_id, chat_id, msg_id, message, data):
    if not _require_admin(cb_id, from_id):
        return HttpResponse(status=200)

    from rps.models import DepositRequest
    action = "verify" if data.startswith("deposit_verify_") else "reject"
    dep_id = int(data.split("_")[-1])

    try:
        dep = DepositRequest.objects.select_related('user').get(pk=dep_id)
    except DepositRequest.DoesNotExist:
        answer_callback_direct(cb_id, "❌ یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if dep.status != 'pending':
        answer_callback_direct(cb_id, f"قبلاً {dep.status} شده.", show_alert=True)
        return HttpResponse(status=200)

    dep.status = 'approved' if action == 'verify' else 'rejected'
    dep.save()
    label = "✅ تایید شد" if action == 'verify' else "❌ رد شد"
    _patch_admin_message(chat_id, msg_id, message, label)
    answer_callback_direct(cb_id, label)
    return HttpResponse(status=200)


# ── Admin: withdrawal ─────────────────────────────────────────────────────────

def _handle_withdrawal_admin(cb_id, from_id, chat_id, msg_id, message, data):
    if not _require_admin(cb_id, from_id):
        return HttpResponse(status=200)

    from rps.models import WithdrawalRequest
    action = "paid" if data.startswith("withdraw_paid_") else "reject"
    req_id = int(data.split("_")[-1])

    try:
        req = WithdrawalRequest.objects.select_related('user').get(pk=req_id)
    except WithdrawalRequest.DoesNotExist:
        answer_callback_direct(cb_id, "❌ یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if req.status != 'pending':
        answer_callback_direct(cb_id, f"قبلاً {req.status} شده.", show_alert=True)
        return HttpResponse(status=200)

    req.status = 'paid' if action == 'paid' else 'rejected'
    req.save()  # triggers model save() → notifies user
    label = "✅ پرداخت شد" if action == 'paid' else "❌ رد شد"
    _patch_admin_message(chat_id, msg_id, message, label)
    answer_callback_direct(cb_id, label)
    return HttpResponse(status=200)


# ── Admin: report ──────────────────────────────────────────────────────────────

def _handle_report_admin(cb_id, from_id, chat_id, msg_id, message, data):
    if not _require_admin(cb_id, from_id):
        return HttpResponse(status=200)

    from rps.models import Report, BotUser
    action = "ignore" if data.startswith("report_ignore_") else "ban"
    rep_id = int(data.split("_")[-1])

    try:
        report = Report.objects.select_related('reported').get(pk=rep_id)
    except Report.DoesNotExist:
        answer_callback_direct(cb_id, "❌ یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if report.status != 'pending':
        answer_callback_direct(cb_id, "قبلاً بررسی شده.", show_alert=True)
        return HttpResponse(status=200)

    if action == 'ignore':
        report.status = 'ignored'
        report.save()
        label = "🙈 نادیده گرفته شد"
    else:
        report.status = 'banned'
        report.save()
        reported = report.reported
        reported.is_banned = True
        reported.save(update_fields=['is_banned'])
        send_message_direct(
            reported.chat_id,
            "🚫 حساب شما به دلیل تخلف مسدود شده است.\n"
            "برای اعتراض با پشتیبانی تماس بگیرید."
        )
        label = "🚫 کاربر مسدود شد"

    _patch_admin_message(chat_id, msg_id, message, label)
    answer_callback_direct(cb_id, label)
    return HttpResponse(status=200)


# ── Friend request callback ────────────────────────────────────────────────────

def _handle_friend_request(cb_id, from_id, data):
    from rps.models import FriendRequest, Friendship, BotUser

    action = "accept" if data.startswith("freq_accept_") else "reject"
    req_id = int(data.split("_")[-1])

    try:
        req = FriendRequest.objects.select_related('sender', 'receiver').get(pk=req_id)
    except FriendRequest.DoesNotExist:
        answer_callback_direct(cb_id, "❌ درخواست یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if req.receiver.chat_id != from_id:
        answer_callback_direct(cb_id, "⛔ این درخواست مربوط به شما نیست.", show_alert=True)
        return HttpResponse(status=200)

    if req.status != 'pending':
        answer_callback_direct(cb_id, "این درخواست قبلاً بررسی شده.", show_alert=True)
        return HttpResponse(status=200)

    if action == "accept":
        req.status = 'accepted'; req.save()
        # Create bi-directional friendship
        Friendship.objects.get_or_create(
            user1=min(req.sender, req.receiver, key=lambda u: u.pk),
            user2=max(req.sender, req.receiver, key=lambda u: u.pk),
        )
        sender_name = req.sender.full_name or req.sender.username or str(req.sender.chat_id)
        receiver_name = req.receiver.full_name or req.receiver.username or str(req.receiver.chat_id)
        send_message_direct(
            req.sender.chat_id,
            f"🎉 *{receiver_name}* درخواست دوستی شما را پذیرفت!\n"
            f"👥 حالا می‌توانید به هم بازی دعوت کنید."
        )
        answer_callback_direct(cb_id, f"✅ {sender_name} را به دوستانتان اضافه کردید!")
    else:
        req.status = 'rejected'; req.save()
        answer_callback_direct(cb_id, "❌ درخواست رد شد.")

    return HttpResponse(status=200)


# ── Game invite callback (friendly game, flat $0.10 entry each) ───────────────

def _handle_game_invite(cb_id, from_id, data):
    from rps.models import GameMatch, BotUser

    action = "accept" if data.startswith("gameinv_accept_") else "reject"
    match_id = int(data.split("_")[-1])

    try:
        match = GameMatch.objects.select_related('player1').get(pk=match_id)
        invitee = BotUser.objects.get(chat_id=from_id)
    except (GameMatch.DoesNotExist, BotUser.DoesNotExist):
        answer_callback_direct(cb_id, "❌ دعوتنامه یافت نشد.", show_alert=True)
        return HttpResponse(status=200)

    if match.status != 'searching':
        answer_callback_direct(cb_id, "⏰ این دعوتنامه منقضی شده.", show_alert=True)
        return HttpResponse(status=200)

    from rps.logic import _join_match

    if action == "accept":
        # The entry fee + prize were already fixed when the invite was created.
        fee = match.entry_fee_cents
        if invitee.balance_cents < fee:
            answer_callback_direct(cb_id, f"❌ موجودی کافی ندارید ({_fmt(fee)} لازم است).", show_alert=True)
            return HttpResponse(status=200)
        invitee.balance_cents -= fee
        invitee.save(update_fields=['balance_cents'])
        _join_match(match, invitee)
        answer_callback_direct(cb_id, "✅ بازی شروع شد!")
    else:
        match.status = 'cancelled'
        match.save(update_fields=['status'])
        # Refund the inviter's entry fee since the invite was declined.
        inviter = match.player1
        if match.entry_fee_cents > 0:
            inviter.balance_cents += match.entry_fee_cents
            inviter.save(update_fields=['balance_cents'])
        answer_callback_direct(cb_id, "❌ دعوت رد شد.")
        send_message_direct(
            inviter.chat_id,
            "😔 دوستتان دعوت بازی شما را رد کرد. هزینه ورود به کیف پول شما بازگشت."
        )

    return HttpResponse(status=200)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _patch_admin_message(chat_id, msg_id, message, label):
    """Append status label to admin message (text or caption)."""
    suffix = f"\n\n─────\n👮 وضعیت: *{label}*"
    try:
        if message.get("photo"):
            original = message.get("caption", "")
            edit_message_caption_direct(chat_id, msg_id, original + suffix)
        else:
            original = message.get("text", "")
            edit_message_direct(chat_id, msg_id, original + suffix)
    except Exception as e:
        print(f"_patch_admin_message error: {e}")


# ─── Main webhook view ────────────────────────────────────────────────────────

@csrf_exempt
def tg_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)
    try:
        update = json.loads(request.body.decode('utf-8'))

        if "callback_query" in update:
            return handle_callback(update["callback_query"])

        if "message" in update:
            chat_id, text, first_name, photo_id, username = extract_message_data(update)
            if chat_id:
                handle_bot_logic(chat_id, text, photo_id, username or first_name)

    except Exception as e:
        print(f"Webhook error: {e}")

    return HttpResponse(status=200)