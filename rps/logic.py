"""
logic.py  –  Main bot FSM dispatcher.

Design principles:
  - All state is stored in BotUser.status (string).
  - Money is in integer cents (avoid float).
  - Celery is used for ALL outbound messages.
  - Inline callbacks are routed through views.py → handle_callback().
  - Games: RPS and Tic-Tac-Toe (TTT). War is placeholder.
"""

import os
import re
import time
import random
import requests
from django.db.models import Q
from django.utils import timezone
from django.core.files.base import ContentFile
from datetime import timedelta

from rps.models import (
    BotUser, GameMatch, FriendRequest, Friendship,
    WithdrawalRequest, DepositRequest, CryptoDepositRequest, Report,
)
from rps.tg_api import send_message, send_message_direct, download_tg_file
from rps.keyboards import (
    main_menu, back_kb, cancel_search_kb,
    rps_bet_kb, rps_move_kb,
    ttt_board_kb,
    c4f_board_kb,
    profile_menu_kb, wallet_menu_kb,
    deposit_amount_kb, deposit_method_kb, crypto_proof_kb,
    friends_menu_kb,
    admin_report_inline_kb, admin_withdrawal_inline_kb, admin_deposit_inline_kb,
    game_invite_inline_kb, friend_req_inline_kb,
)

# ─── Config ───────────────────────────────────────────────────────────────────

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "8093967783")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()]

TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "8093967783")
TELEGRAM_TOKEN         = os.getenv("TELEGRAM_TOKEN", "")
BOT_USERNAME           = os.getenv("BOT_USERNAME", "your_bot")

# Game fees & prizes (in cents)
RPS_SEARCH_FEE   = 20   # $0.20 search fee
TTT_SEARCH_FEE   = 30   # $0.30 search fee
C4F_SEARCH_FEE   = 30   # $0.30 search fee

RPS_OFFLINE_FEE  = 5    # $0.05 play fee
TTT_OFFLINE_FEE  = 5    # $0.05 play fee
C4F_OFFLINE_FEE  = 5    # $0.05 play fee

RPS_OFFLINE_WIN  = 35   # $0.35 win reward (offline)
TTT_OFFLINE_WIN  = 35   # $0.35 win reward (offline)
C4F_OFFLINE_WIN  = 35   # $0.35 win reward (offline)

# Available online bet amounts per game
RPS_BET_OPTIONS  = [30, 50, 100, 200, 500]    # $0.30 $0.50 $1 $2 $5
TTT_BET_OPTIONS  = [50, 70, 100, 150, 200]    # $0.50 $0.70 $1 $1.50 $2
C4F_BET_OPTIONS  = [50, 100, 200, 300, 500]   # $0.50 $1 $2 $3 $5

FRIEND_REQ_FEE   = 10   # $0.10
GAME_INVITE_FEE  = 20   # $0.20

MIN_WITHDRAWAL   = 1500  # $15.00

REFERRAL_BONUS   = 50    # $0.50 on join (if inviter exists; full bonus on profile complete)
SIGNUP_BONUS     = 50    # $0.50 given to every new user immediately on /start

# Crypto wallets
# Only TRON (USDT-TRC20) is accepted for deposits.
CRYPTO_WALLETS = {
    "USDT (TRC20)": os.getenv("WALLET_USDT_TRC20", "TSEfwvtG48EoAXkP7HnbYsCxm7AtQXhUSu"),
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_IDS


def _fmt(cents: int) -> str:
    return f"${cents/100:.2f}"


def _cents_from_dollar_str(s: str):
    """Parse '0.30$' or '$0.30' → 30 (cents). Returns None on failure."""
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return round(float(s) * 100)
    except ValueError:
        return None


def _tg_admin_post(method: str, **kwargs):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, timeout=10, **kwargs)
        return r.json()
    except Exception as e:
        print(f"Admin Telegram API error ({method}): {e}")
        return None


# ─── Registration guard ───────────────────────────────────────────────────────

def _ensure_profile(chat_id: int, user: BotUser) -> bool:
    """Returns True if user has completed required fields (name + age)."""
    return bool(user.full_name and user.age)


# ─── Main dispatcher ─────────────────────────────────────────────────────────

def handle_bot_logic(chat_id: int, text: str, photo_id=None, current_username=None):
    try:
        user, created = BotUser.objects.get_or_create(chat_id=chat_id)
    except Exception as e:
        print(f"DB error on get_or_create: {e}")
        return

    if user.is_banned:
        send_message(chat_id, "🚫 حساب شما مسدود شده است.")
        return

    # ── Signup bonus: $0.50 the moment a user is created ───────────────────────
    if created:
        user.add_dollars(SIGNUP_BONUS / 100)

    # Update username silently
    if current_username and user.username != current_username:
        user.username = current_username
        user.save(update_fields=['username'])

    is_admin = _is_admin(chat_id)
    mk = main_menu(is_admin)

    # ── Handle new user registration flow FIRST ────────────────────────────────
    if created or (not user.full_name or not user.age):
        # Handle referral on first join
        if created and text and text.startswith("/start "):
            _handle_referral(user, text)

        result = _registration_flow(chat_id, user, text, mk)
        if result is not False:  # False means fall-through to main menu
            return

    # ── Ongoing state-machine flows ───────────────────────────────────────────
    status = user.status

    # Profile editing states
    if status.startswith('edit_'):
        return _handle_profile_edit(chat_id, user, text, photo_id, mk)

    # Deposit flows
    if status.startswith('deposit_'):
        return _handle_deposit_flow(chat_id, user, text, photo_id, mk)

    # Crypto flows
    if status.startswith('crypto_'):
        return _handle_crypto_flow(chat_id, user, text, photo_id, mk)

    # Withdrawal flow
    if status == 'wait_withdrawal_wallet':
        return _handle_withdrawal_wallet(chat_id, user, text, mk)
    if status == 'wait_withdrawal_amount':
        return _handle_withdrawal_amount(chat_id, user, text, mk)

    # Search for friend
    if status == 'search_friend':
        return _handle_search_friend(chat_id, user, text, mk)

    # Report reason
    if status.startswith('report_reason_'):
        return _handle_report_reason(chat_id, user, text, status, mk)

    # RPS move
    if status.startswith('playing_rps_'):
        return _handle_rps_move(chat_id, user, text, mk)

    # Admin broadcast
    if status == 'wait_broadcast' and is_admin:
        return _handle_broadcast(chat_id, user, text, mk)

    # ── Text commands ──────────────────────────────────────────────────────────

    if not text:
        return

    # Start / Back
    if text in ("/start", "🔙 بازگشت"):
        _reset_user(user)
        return send_message(chat_id, _welcome_msg(user), mk)

    # ── Admin panel ───────────────────────────────────────────────────────────
    if is_admin and text == "⚙️ پنل مدیریت":
        return _admin_panel(chat_id, user, mk)

    # ── Main menu buttons ─────────────────────────────────────────────────────
    if text == "🎯 دوز (Tic-Tac-Toe)":
        return _game_menu(chat_id, user, 'ttt', mk)

    if text == "✊ سنگ کاغذ قیچی":
        return _game_menu(chat_id, user, 'rps', mk)

    if text == "🔴 چهار در یک (Connect Four)":
        return _game_menu(chat_id, user, 'c4f', mk)

    if text == "⚔️ بازی جنگ (به زودی)":
        return send_message(chat_id, "🚧 این بازی به زودی اضافه می‌شود! منتظر آپدیت باشید.", mk)

    if text == "👤 پروفایل من":
        return _show_profile(chat_id, user)

    if text == "👥 دوستان":
        return _friends_menu(chat_id, user)

    if text == "💰 کیف پول":
        return _wallet_menu(chat_id, user)

    if text == "🏆 رتبه‌بندی":
        return _show_leaderboard(chat_id, mk)

    if text == "❓ راهنما":
        return _show_help(chat_id, mk)

    # Profile sub-menu
    if text == "✏️ ویرایش نام":
        user.status = 'edit_name'; user.save(update_fields=['status'])
        return send_message(chat_id, "✏️ نام جدید خود را وارد کنید:", back_kb())

    if text == "✏️ ویرایش سن":
        user.status = 'edit_age'; user.save(update_fields=['status'])
        return send_message(chat_id, "✏️ سن خود را وارد کنید (عدد):", back_kb())

    if text == "📱 ویرایش شماره":
        user.status = 'edit_phone'; user.save(update_fields=['status'])
        return send_message(chat_id, "📱 شماره تلفن خود را وارد کنید:", back_kb())

    if text == "💳 ویرایش کیف پول ترون":
        user.status = 'edit_tron'; user.save(update_fields=['status'])
        return send_message(chat_id, "💳 آدرس کیف پول ترون (TRC20) خود را وارد کنید:", back_kb())

    if text == "🖼 تغییر آواتار":
        user.status = 'edit_avatar'; user.save(update_fields=['status'])
        return send_message(chat_id, "🖼 عکس پروفایل خود را ارسال کنید:", back_kb())

    if text == "🔗 لینک دعوت":
        return _show_referral_link(chat_id, user)

    # Wallet sub-menu
    if text == "➕ شارژ کیف پول":
        return send_message(chat_id, "💵 روش شارژ را انتخاب کنید:", deposit_method_kb())

    if text == "💳 پرداخت کارتی (رسید)":
        return send_message(chat_id, "💵 مبلغ مورد نظر را انتخاب کنید:", deposit_amount_kb())

    if text == "🪙 واریز کریپتو":
        return _start_crypto_deposit(chat_id, user)

    if text == "💸 درخواست برداشت":
        return _start_withdrawal(chat_id, user)

    # Deposit amount selection
    if text and "💵" in text and "$" in text:
        return _handle_deposit_amount_selected(chat_id, user, text, mk)

    # RPS game bet selection
    if text and "💰" in text and "$" in text and user.status.startswith('bet_'):
        return _handle_bet_selected(chat_id, user, text, mk)

    # RPS moves
    if text in ("🪨 سنگ", "📄 کاغذ", "✂️ قیچی"):
        return _handle_rps_move(chat_id, user, text, mk)

    # Search cancel / offline
    if text == "❌ انصراف از جستجو":
        return _cancel_search(chat_id, user, mk)

    if text == "🤖 بازی آفلاین (با ربات)":
        return _start_offline_game(chat_id, user, mk)

    # Friends sub-menu
    if text == "👥 لیست دوستان":
        return _list_friends(chat_id, user)

    if text == "📨 درخواست‌های دریافتی":
        return _show_friend_requests(chat_id, user)

    if text == "🔍 افزودن دوست":
        user.status = 'search_friend'; user.save(update_fields=['status'])
        return send_message(chat_id, "🔍 یوزرنیم یا chat_id دوستتان را وارد کنید:", back_kb())

    if text == "🎮 دعوت به بازی":
        return _invite_friend_to_game(chat_id, user)

    # Report
    if text == "🚨 گزارش کاربر":
        user.status = 'report_start'; user.save(update_fields=['status'])
        return send_message(chat_id, "🆔 chat_id کاربری که می‌خواهید گزارش دهید را وارد کنید:", back_kb())

    if status == 'report_start' and text and text.lstrip('-').isdigit():
        return _start_report(chat_id, user, int(text), mk)


# ─── Registration flow ────────────────────────────────────────────────────────

def _handle_referral(user: BotUser, text: str):
    try:
        inviter_id = int(text.strip().split(" ")[1])
        inviter = BotUser.objects.get(chat_id=inviter_id)
        if inviter.chat_id != user.chat_id:
            user.referred_by = inviter
            user.save(update_fields=['referred_by'])
            send_message(
                inviter.chat_id,
                f"🎊 یک نفر با لینک دعوت شما ثبت‌نام کرد!\n"
                f"💵 پس از تکمیل پروفایل توسط دوست شما، *{_fmt(REFERRAL_BONUS)}* به شما اضافه می‌شود."
            )
    except Exception:
        pass


def _registration_flow(chat_id, user, text, mk):
    """
    Guide new users through name → age → (optionally phone).
    Returns False to indicate the main handler should continue.
    Returns None/anything else to stop here.
    """
    status = user.status

    # Step 1: ask name
    if not user.full_name and status not in ('reg_name', 'reg_age'):
        user.status = 'reg_name'
        user.save(update_fields=['status'])
        send_message(
            chat_id,
            "👋 *خوش آمدید!*\n\n"
            f"🎁 *{_fmt(SIGNUP_BONUS)}* هدیه خوش‌آمدگویی به کیف پول شما اضافه شد!\n\n"
            "برای شروع، لطفاً *نام کامل* خود را وارد کنید:"
        )
        return True

    if status == 'reg_name':
        if not text or len(text.strip()) < 2:
            send_message(chat_id, "⚠️ لطفاً یک نام معتبر (حداقل ۲ حرف) وارد کنید:")
            return True
        user.full_name = text.strip()[:100]
        user.status = 'reg_age'
        user.save(update_fields=['full_name', 'status'])
        send_message(chat_id, "✅ نام ثبت شد!\n\n🎂 حالا *سن* خود را وارد کنید (عدد):")
        return True

    if status == 'reg_age':
        if not text or not text.strip().isdigit() or not (10 <= int(text.strip()) <= 100):
            send_message(chat_id, "⚠️ لطفاً سن معتبر وارد کنید (بین ۱۰ تا ۱۰۰):")
            return True
        user.age = int(text.strip())
        user.status = 'idle'
        user.save(update_fields=['age', 'status'])
        bonus_given = user.check_and_grant_profile_bonus()
        bonus_line = (
            f"💵 پاداش تکمیل پروفایل: *{_fmt(50)}* به کیف پول شما اضافه شد!\n\n"
            if bonus_given else "\n"
        )
        send_message(
            chat_id,
            f"🎉 *ثبت‌نام کامل شد!*\n\n"
            f"👤 نام: *{user.full_name}*\n"
            f"🎂 سن: *{user.age}*\n"
            f"{bonus_line}"
            "با دکمه‌های زیر به بازی بپردازید 👇",
            mk,
        )
        return True

    # Profile complete, fall through
    return False


def _reset_user(user: BotUser):
    if user.status != 'idle':
        user.status = 'idle'
        user.save(update_fields=['status'])


def _welcome_msg(user: BotUser) -> str:
    name = user.full_name or user.username or "کاربر"
    bal = _fmt(user.balance_cents)
    return (
        f"👋 سلام *{name}!*\n\n"
        f"💰 موجودی: *{bal}*\n"
        f"🏆 برد: {user.wins} | ❌ باخت: {user.losses} | 🎯 نرخ برد: {user.win_rate}\n\n"
        "یک گزینه را انتخاب کنید 👇"
    )


# ─── Profile ─────────────────────────────────────────────────────────────────

def _show_profile(chat_id, user: BotUser):
    sub_count = BotUser.objects.filter(referred_by=user).count()
    link = f"https://t.me/{BOT_USERNAME}?start={user.chat_id}"
    avatar_str = "✅ تنظیم شده" if user.avatar_file_id else "❌ تنظیم نشده"
    phone_str = user.phone or "تنظیم نشده"
    tron_str = f"`{user.tron_wallet}`" if user.tron_wallet else "تنظیم نشده"
    msg = (
        "👤 *پروفایل شما*\n"
        "─────────────\n"
        f"📛 نام: *{user.full_name}*\n"
        f"🎂 سن: *{user.age}*\n"
        f"📱 شماره: {phone_str}\n"
        f"💳 کیف پول ترون: {tron_str}\n"
        f"🖼 آواتار: {avatar_str}\n"
        "─────────────\n"
        f"💰 موجودی: *{_fmt(user.balance_cents)}*\n"
        f"🏆 برد: {user.wins} | ❌ باخت: {user.losses}\n"
        f"📊 نرخ برد: {user.win_rate} از {user.total_games} بازی\n"
        "─────────────\n"
        f"👥 زیرمجموعه: {sub_count} نفر\n"
        f"🔗 لینک دعوت:\n`{link}`\n\n"
        "برای ویرایش، دکمه‌های زیر را لمس کنید 👇"
    )
    # Show avatar if set
    if user.avatar_file_id:
        from rps.tg_api import send_photo_direct
        send_photo_direct(chat_id, user.avatar_file_id, caption=msg,
                          reply_markup=profile_menu_kb())
    else:
        send_message(chat_id, msg, profile_menu_kb())


def _handle_profile_edit(chat_id, user, text, photo_id, mk):
    status = user.status

    if text == "🔙 بازگشت":
        _reset_user(user)
        return _show_profile(chat_id, user)

    if status == 'edit_name':
        if not text or len(text.strip()) < 2:
            return send_message(chat_id, "⚠️ نام باید حداقل ۲ حرف داشته باشد:")
        user.full_name = text.strip()[:100]
        user.status = 'idle'
        user.save(update_fields=['full_name', 'status'])
        return send_message(chat_id, f"✅ نام به *{user.full_name}* تغییر یافت.", profile_menu_kb())

    if status == 'edit_age':
        if not text or not text.strip().isdigit() or not (10 <= int(text.strip()) <= 100):
            return send_message(chat_id, "⚠️ سن معتبر (۱۰–۱۰۰) وارد کنید:")
        user.age = int(text.strip())
        user.status = 'idle'
        user.save(update_fields=['age', 'status'])
        return send_message(chat_id, f"✅ سن به *{user.age}* تغییر یافت.", profile_menu_kb())

    if status == 'edit_phone':
        if not text or not re.match(r'^\+?[\d\s\-]{8,15}$', text.strip()):
            return send_message(chat_id, "⚠️ شماره تلفن معتبر وارد کنید:")
        user.phone = text.strip()[:20]
        user.status = 'idle'
        user.save(update_fields=['phone', 'status'])
        return send_message(chat_id, "✅ شماره تلفن ثبت شد.", profile_menu_kb())

    if status == 'edit_tron':
        addr = text.strip() if text else ""
        if not addr or not re.match(r'^T[A-Za-z0-9]{33}$', addr):
            return send_message(chat_id, "⚠️ آدرس ترون معتبر (شروع با T و ۳۴ کاراکتر) وارد کنید:")
        user.tron_wallet = addr
        user.status = 'idle'
        user.save(update_fields=['tron_wallet', 'status'])
        return send_message(chat_id, f"✅ آدرس کیف پول ترون ثبت شد:\n`{addr}`", profile_menu_kb())

    if status == 'edit_avatar':
        if not photo_id:
            return send_message(chat_id, "⚠️ لطفاً یک عکس ارسال کنید:")
        user.avatar_file_id = photo_id
        user.status = 'idle'
        user.save(update_fields=['avatar_file_id', 'status'])
        return send_message(chat_id, "✅ آواتار پروفایل شما به‌روزرسانی شد! 🖼", profile_menu_kb())


def _show_referral_link(chat_id, user):
    link = f"https://t.me/{BOT_USERNAME}?start={user.chat_id}"
    sub_count = BotUser.objects.filter(referred_by=user).count()
    send_message(
        chat_id,
        f"🔗 *لینک دعوت اختصاصی شما*\n\n"
        f"`{link}`\n\n"
        f"👥 تعداد دعوت‌شده‌ها: *{sub_count} نفر*\n"
        f"💵 پاداش هر دعوت موفق (تکمیل پروفایل): *{_fmt(REFERRAL_BONUS)}*\n\n"
        "لینک را کپی کنید و برای دوستانتان بفرستید!",
        back_kb(),
    )


# ─── Wallet / Deposit / Withdrawal ───────────────────────────────────────────

def _wallet_menu(chat_id, user):
    msg = (
        "💰 *کیف پول شما*\n"
        "─────────────\n"
        f"💵 موجودی: *{_fmt(user.balance_cents)}*\n"
        f"📈 حداقل برداشت: *{_fmt(MIN_WITHDRAWAL)}*\n\n"
        "برای شارژ یا برداشت، گزینه مورد نظر را انتخاب کنید 👇"
    )
    send_message(chat_id, msg, wallet_menu_kb())


def _handle_deposit_amount_selected(chat_id, user, text, mk):
    """User tapped a $1/$5/$10/$20 button."""
    # Extract dollar amount
    match = re.search(r'\$([\d]+)', text)
    if not match:
        return
    dollars = int(match.group(1))
    valid = [1, 5, 10, 20]
    if dollars not in valid:
        return send_message(chat_id, "⚠️ مبلغ نامعتبر است.", mk)
    cents = dollars * 100
    user.status = f'deposit_amount_{cents}'
    user.save(update_fields=['status'])
    send_message(
        chat_id,
        f"💳 *شارژ {_fmt(cents)}*\n\n"
        "برای واریز، روش پرداخت را انتخاب کنید:",
        deposit_method_kb(),
    )


def _handle_deposit_flow(chat_id, user, text, photo_id, mk):
    status = user.status

    if text == "🔙 بازگشت":
        _reset_user(user)
        return _wallet_menu(chat_id, user)

    # User selected card payment method
    if text == "💳 پرداخت کارتی (رسید)" and status.startswith('deposit_amount_'):
        cents = int(status.split('_')[2])
        user.status = f'deposit_receipt_{cents}'
        user.save(update_fields=['status'])
        return send_message(
            chat_id,
            f"💳 *واریز {_fmt(cents)} از طریق کارت*\n\n"
            "لطفاً رسید پرداخت خود را به‌صورت عکس ارسال کنید:",
            back_kb(),
        )

    if text == "🪙 واریز کریپتو" and status.startswith('deposit_amount_'):
        return _start_crypto_deposit(chat_id, user)

    # Waiting for receipt photo
    if status.startswith('deposit_receipt_') and photo_id:
        cents = int(status.split('_')[2])
        req = DepositRequest.objects.create(user=user, amount_cents=cents)
        req.receipt_image.save(f"{chat_id}_{int(time.time())}.jpg", ContentFile(
            download_tg_file(photo_id) or b""
        ))
        req.save()
        user.status = 'idle'
        user.save(update_fields=['status'])
        # Notify admin
        _notify_admin_deposit(req)
        return send_message(
            chat_id,
            f"✅ *رسید ثبت شد!*\n\n"
            f"💵 مبلغ: *{_fmt(cents)}*\n"
            f"🔖 شماره: `#{req.pk}`\n\n"
            "پس از تایید توسط ادمین، موجودی شما شارژ می‌شود.",
            mk,
        )

    if status.startswith('deposit_receipt_') and not photo_id:
        return send_message(chat_id, "⚠️ لطفاً فقط *عکس* ارسال کنید.")


def _notify_admin_deposit(req: DepositRequest):
    inline_kb = admin_deposit_inline_kb(req.pk)
    caption = (
        f"💳 *درخواست واریز کارتی* `#{req.pk}`\n\n"
        f"👤 کاربر: `{req.user.chat_id}`\n"
        f"📛 نام: {req.user.full_name}\n"
        f"💵 مبلغ: *{_fmt(req.amount_cents)}*\n"
    )
    if req.receipt_image:
        from rps.tg_api import send_photo_direct
        send_photo_direct(
            TELEGRAM_ADMIN_CHAT_ID,
            open(req.receipt_image.path, 'rb').read(),
            caption=caption,
            reply_markup=inline_kb,
        )
    else:
        from rps.tg_api import send_message_direct
        send_message_direct(TELEGRAM_ADMIN_CHAT_ID, caption, inline_kb)


def _start_withdrawal(chat_id, user):
    if user.balance_cents < MIN_WITHDRAWAL:
        return send_message(
            chat_id,
            f"⚠️ *موجودی ناکافی برای برداشت*\n\n"
            f"💵 موجودی شما: *{_fmt(user.balance_cents)}*\n"
            f"💵 حداقل برداشت: *{_fmt(MIN_WITHDRAWAL)}*\n\n"
            "با بازی و پیروزی موجودی خود را افزایش دهید!",
            wallet_menu_kb(),
        )
    user.status = 'wait_withdrawal_wallet'
    user.save(update_fields=['status'])
    current_wallet = f"\n💳 کیف پول فعلی: `{user.tron_wallet}`" if user.tron_wallet else ""
    send_message(
        chat_id,
        f"💸 *درخواست برداشت*\n\n"
        f"💵 موجودی: *{_fmt(user.balance_cents)}*{current_wallet}\n\n"
        "آدرس کیف پول ترون (TRC20) برای دریافت را وارد کنید:",
        back_kb(),
    )


def _handle_withdrawal_wallet(chat_id, user, text, mk):
    if text == "🔙 بازگشت":
        _reset_user(user); return _wallet_menu(chat_id, user)
    if not text or not re.match(r'^T[A-Za-z0-9]{33}$', text.strip()):
        return send_message(chat_id, "⚠️ آدرس ترون معتبر (شروع با T، ۳۴ کاراکتر) وارد کنید:")
    user.status = f'wait_withdrawal_amount:{text.strip()}'
    user.save(update_fields=['status'])
    send_message(
        chat_id,
        f"💵 مبلغ برداشت را وارد کنید (دلار):\n"
        f"💰 موجودی: *{_fmt(user.balance_cents)}*\n"
        f"📌 حداقل: *{_fmt(MIN_WITHDRAWAL)}*\n\n"
        "مثال: `15` یا `20`",
        back_kb(),
    )


def _handle_withdrawal_amount(chat_id, user, text, mk):
    if text == "🔙 بازگشت":
        _reset_user(user); return _wallet_menu(chat_id, user)
    parts = user.status.split(':', 1)
    wallet = parts[1] if len(parts) > 1 else ''
    if not text or not text.strip().replace('.', '').isdigit():
        return send_message(chat_id, "⚠️ یک عدد معتبر وارد کنید (مثل 15 یا 20):")
    cents = round(float(text.strip()) * 100)
    if cents < MIN_WITHDRAWAL:
        return send_message(chat_id, f"⚠️ حداقل برداشت {_fmt(MIN_WITHDRAWAL)} است.")
    if cents > user.balance_cents:
        return send_message(chat_id, f"⚠️ موجودی شما ({_fmt(user.balance_cents)}) کافی نیست.")
    # Deduct and create request
    user.balance_cents -= cents
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])
    req = WithdrawalRequest.objects.create(user=user, amount_cents=cents, tron_wallet=wallet)
    # Notify admin
    _notify_admin_withdrawal(req)
    send_message(
        chat_id,
        f"✅ *درخواست برداشت ثبت شد!*\n\n"
        f"💵 مبلغ: *{_fmt(cents)}*\n"
        f"💳 آدرس: `{wallet}`\n"
        f"🔖 شماره: `#{req.pk}`\n\n"
        "پس از بررسی توسط ادمین، پرداخت انجام می‌شود.",
        mk,
    )


def _notify_admin_withdrawal(req: WithdrawalRequest):
    from rps.tg_api import send_message_direct
    msg = (
        f"💸 *درخواست برداشت* `#{req.pk}`\n\n"
        f"👤 کاربر: `{req.user.chat_id}`\n"
        f"📛 نام: {req.user.full_name}\n"
        f"💵 مبلغ: *{_fmt(req.amount_cents)}*\n"
        f"💳 آدرس ترون:\n`{req.tron_wallet}`"
    )
    send_message_direct(TELEGRAM_ADMIN_CHAT_ID, msg, admin_withdrawal_inline_kb(req.pk))


# ─── Crypto deposit ───────────────────────────────────────────────────────────

def _start_crypto_deposit(chat_id, user):
    """Only one coin (USDT TRC20) is supported, so skip straight to the address."""
    coin_name = next(iter(CRYPTO_WALLETS))
    addr = CRYPTO_WALLETS[coin_name]
    safe = coin_name.replace(" ", "_").replace("(", "").replace(")", "")
    user.status = f'crypto_select_proof:{safe}'
    user.save(update_fields=['status'])
    send_message(
        chat_id,
        f"🪙 *واریز کریپتو – {coin_name}*\n\n"
        f"آدرس کیف پول (شبکه ترون / TRC20):\n`{addr}`\n\n"
        "⚠️ فقط از شبکه *TRC20* استفاده کنید، در غیر این صورت واریز شما از بین می‌رود.\n\n"
        "پس از واریز، مدرک پرداخت را ارسال کنید:",
        crypto_proof_kb(),
    )


def _handle_crypto_flow(chat_id, user, text, photo_id, mk):
    status = user.status

    if text == "🔙 بازگشت":
        _reset_user(user); return _wallet_menu(chat_id, user)

    if status.startswith('crypto_select_proof:'):
        coin_safe = status.split(':', 1)[1]
        if text == "📸 ارسال اسکرین‌شات":
            user.status = f'crypto_wait_ss:{coin_safe}'
            user.save(update_fields=['status'])
            return send_message(chat_id, "📸 تصویر اسکرین‌شات را ارسال کنید:", back_kb())
        if text == "🔢 کد پیگیری / TxHash":
            user.status = f'crypto_wait_tx:{coin_safe}'
            user.save(update_fields=['status'])
            return send_message(chat_id, "🔢 کد پیگیری یا TxHash را وارد کنید:", back_kb())

    if status.startswith('crypto_wait_ss:') and photo_id:
        coin_safe = status.split(':', 1)[1]
        user.status = f'crypto_got_ss_{photo_id}:{coin_safe}'
        user.save(update_fields=['status'])
        return send_message(chat_id, "✅ اسکرین‌شات دریافت شد.\n💵 مبلغ واریزی را به دلار وارد کنید (مثلاً: 10):")

    if status.startswith('crypto_wait_tx:') and text:
        coin_safe = status.split(':', 1)[1]
        user.status = f'crypto_got_tx_{text}:{coin_safe}'
        user.save(update_fields=['status'])
        return send_message(chat_id, "✅ کد دریافت شد.\n💵 مبلغ واریزی را به دلار وارد کنید (مثلاً: 10):")

    # Amount entry
    if (status.startswith('crypto_got_ss_') or status.startswith('crypto_got_tx_')) and text:
        if not text.strip().replace('.', '').isdigit():
            return send_message(chat_id, "⚠️ یک عدد معتبر وارد کنید:")
        cents = round(float(text.strip()) * 100)
        if cents < 100:
            return send_message(chat_id, "⚠️ حداقل مبلغ ۱ دلار است.")

        parts = status.rsplit(':', 1)
        coin_safe = parts[1] if len(parts) > 1 else 'unknown'
        coin_name = _resolve_coin(coin_safe)
        data_part = parts[0]

        if data_part.startswith('crypto_got_ss_'):
            proof_type = 'screenshot'
            proof_data = data_part.replace('crypto_got_ss_', '')
        else:
            proof_type = 'tracking'
            proof_data = data_part.replace('crypto_got_tx_', '')

        deposit = CryptoDepositRequest.objects.create(
            user=user, coin=coin_name,
            amount_cents=cents, proof_type=proof_type, proof_data=proof_data,
        )
        user.status = 'idle'
        user.save(update_fields=['status'])
        _notify_admin_crypto(deposit)
        send_message(
            chat_id,
            f"✅ *درخواست واریز کریپتو ثبت شد!*\n\n"
            f"🪙 ارز: {coin_name}\n"
            f"💵 مبلغ: *{_fmt(cents)}*\n"
            f"🔖 شماره: `#{deposit.pk}`\n\n"
            "پس از تایید ادمین، موجودی شما شارژ می‌شود.",
            mk,
        )


def _resolve_coin(coin_safe: str) -> str:
    for c in CRYPTO_WALLETS:
        if c.replace(" ", "_").replace("(", "").replace(")", "") == coin_safe:
            return c
    return coin_safe.replace('_', ' ')


def _notify_admin_crypto(deposit: CryptoDepositRequest):
    user = deposit.user
    uname = f"@{user.username}" if user.username else f"ID:{user.chat_id}"
    caption = (
        f"💎 *درخواست واریز کریپتو* `#{deposit.pk}`\n\n"
        f"👤 کاربر: {uname}\n"
        f"📛 نام: {user.full_name}\n"
        f"🆔 Chat ID: `{user.chat_id}`\n"
        f"🪙 ارز: {deposit.coin}\n"
        f"💵 مبلغ: *{_fmt(deposit.amount_cents)}*\n"
        f"📋 نوع مدرک: {'📸 اسکرین‌شات' if deposit.proof_type == 'screenshot' else '🔢 کد پیگیری'}\n"
    )
    if deposit.proof_type == "tracking":
        caption += f"🔢 کد: `{deposit.proof_data}`\n"
    inline_kb = admin_deposit_inline_kb(deposit.pk, crypto=True)
    if deposit.proof_type == "screenshot":
        from rps.tg_api import send_photo_direct
        send_photo_direct(TELEGRAM_ADMIN_CHAT_ID, deposit.proof_data, caption=caption, reply_markup=inline_kb)
    else:
        from rps.tg_api import send_message_direct
        send_message_direct(TELEGRAM_ADMIN_CHAT_ID, caption, inline_kb)


# ─── Games ───────────────────────────────────────────────────────────────────

def _game_menu(chat_id, user, game_type, mk):
    """Show bet selection for online play, or offline option."""
    names = {'rps': 'سنگ کاغذ قیچی', 'ttt': 'دوز', 'c4f': 'چهار در یک'}
    fees  = {'rps': RPS_SEARCH_FEE, 'ttt': TTT_SEARCH_FEE, 'c4f': C4F_SEARCH_FEE}
    off_fees = {'rps': RPS_OFFLINE_FEE, 'ttt': TTT_OFFLINE_FEE, 'c4f': C4F_OFFLINE_FEE}

    game_name   = names[game_type]
    fee         = fees[game_type]
    offline_fee = off_fees[game_type]

    msg = (
        f"🎮 *{game_name}*\n\n"
        f"🔵 *بازی آنلاین:*\n"
        f"  هزینه جستجو: *{_fmt(fee)}*\n"
        f"  مبلغ شرط: به انتخاب شما\n"
        f"  برنده: مجموع دو شرط ➡ برنده\n\n"
        f"⚪ *بازی آفلاین (با ربات):*\n"
        f"  هزینه: *{_fmt(offline_fee)}*\n"
        f"  جایزه برد: *{_fmt(C4F_OFFLINE_WIN)}*\n\n"
        "مبلغ شرط آنلاین را انتخاب کنید (یا بازی با ربات):"
    )
    user.status = f'bet_{game_type}'
    user.save(update_fields=['status'])
    bet_kb = rps_bet_kb(game_type)
    kb = {
        "keyboard": [[{"text": "🤖 بازی آفلاین (با ربات)"}]] + bet_kb["keyboard"],
        "resize_keyboard": True,
    }
    send_message(chat_id, msg, kb)


def _handle_bet_selected(chat_id, user, text, mk):
    """User picked a bet amount like '💰 0.30$'."""
    status = user.status
    if not status.startswith('bet_'):
        return
    game_type = status.split('_')[1]  # 'rps', 'ttt', or 'c4f'

    cents = _cents_from_dollar_str(text.replace("💰", "").strip())
    if cents is None:
        return send_message(chat_id, "⚠️ مبلغ نامعتبر.")

    valid_bets = {'rps': RPS_BET_OPTIONS, 'ttt': TTT_BET_OPTIONS, 'c4f': C4F_BET_OPTIONS}
    if cents not in valid_bets.get(game_type, []):
        return send_message(chat_id, "⚠️ این مبلغ معتبر نیست.")

    fees = {'rps': RPS_SEARCH_FEE, 'ttt': TTT_SEARCH_FEE, 'c4f': C4F_SEARCH_FEE}
    fee = fees[game_type]
    total_cost = fee + cents

    if user.balance_cents < total_cost:
        return send_message(
            chat_id,
            f"❌ *موجودی ناکافی!*\n\n"
            f"💰 موجودی: *{_fmt(user.balance_cents)}*\n"
            f"💸 هزینه کل (شرط + کارمزد جستجو): *{_fmt(total_cost)}*",
            mk,
        )

    # Deduct search fee and bet
    user.balance_cents -= total_cost
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])

    # Check for waiting match
    waiting = (
        GameMatch.objects
        .filter(game_type=game_type, bet_cents=cents, status='searching')
        .exclude(player1=user)
        .first()
    )

    if waiting:
        _join_match(waiting, user, game_type, cents, mk)
    else:
        _create_search(chat_id, user, game_type, cents, fee)


def _create_search(chat_id, user, game_type, bet_cents, fee_cents):
    from rps.tasks import search_animation_task, expire_search_task
    now = timezone.now()
    match = GameMatch.objects.create(
        game_type=game_type,
        player1=user,
        bet_cents=bet_cents,
        search_fee_cents=fee_cents,
        status='searching',
        search_started_at=now,
    )
    user.status = f'searching_{match.pk}'
    user.save(update_fields=['status'])

    names = {'rps': 'سنگ کاغذ قیچی', 'ttt': 'دوز', 'c4f': 'چهار در یک'}
    game_name = names.get(game_type, game_type)

    from rps.tg_api import send_message_direct
    from rps.keyboards import cancel_search_kb
    result = send_message_direct(
        chat_id,
        f"🔍 *در حال جستجوی حریف...*\n\n"
        f"🎮 بازی: *{game_name}*\n"
        f"💰 شرط: *{_fmt(bet_cents)}*\n\n"
        "_لطفاً صبر کنید..._",
        cancel_search_kb(),
    )
    if result and result.get('ok'):
        match.p1_search_msg_id = result['result']['message_id']
        match.save(update_fields=['p1_search_msg_id'])

    search_animation_task.apply_async(args=[match.pk, 0], countdown=3)
    expire_search_task.apply_async(args=[match.pk], countdown=300)


def _join_match(match: GameMatch, user: BotUser, game_type: str, bet_cents: int, mk):
    match.player2 = user
    match.status = 'active'
    match.save(update_fields=['player2', 'status'])

    p1 = match.player1
    p1.status = f'playing_{game_type}_{match.pk}'
    p1.save(update_fields=['status'])
    user.status = f'playing_{game_type}_{match.pk}'
    user.save(update_fields=['status'])

    names = {'rps': 'سنگ کاغذ قیچی', 'ttt': 'دوز', 'c4f': 'چهار در یک'}
    game_name = names.get(game_type, game_type)
    p2_name = user.full_name or user.username or "کاربر"
    p1_name = p1.full_name or p1.username or "کاربر"

    start_msg_p1 = (
        f"🎉 *حریف پیدا شد!*\n\n"
        f"🎮 بازی: *{game_name}*\n"
        f"💰 شرط: *{_fmt(bet_cents)}*\n"
        f"👤 حریف: *{p2_name}*\n\n"
    )
    start_msg_p2 = (
        f"🎉 *حریف پیدا شد!*\n\n"
        f"🎮 بازی: *{game_name}*\n"
        f"💰 شرط: *{_fmt(bet_cents)}*\n"
        f"👤 حریف: *{p1_name}*\n\n"
    )

    if game_type == 'rps':
        move_prompt = "حرکت خود را انتخاب کنید 👇"
        send_message(p1.chat_id, start_msg_p1 + move_prompt, rps_move_kb())
        send_message(user.chat_id, start_msg_p2 + move_prompt, rps_move_kb())

    elif game_type == 'ttt':
        board_str = match.ttt_board
        send_message(
            p1.chat_id,
            start_msg_p1 + "✅ *نوبت شماست* (❌)",
            ttt_board_kb(board_str, match.pk),
        )
        send_message(
            user.chat_id,
            start_msg_p2 + "⏳ نوبت حریف است (⭕)",
            None,
        )

    elif game_type == 'c4f':
        board_str = match.c4f_board
        send_message(
            p1.chat_id,
            start_msg_p1 + "✅ *نوبت شماست!* شما 🔴 هستید.\nیک ستون را انتخاب کنید:",
            c4f_board_kb(board_str, match.pk),
        )
        send_message(
            user.chat_id,
            start_msg_p2 + "⏳ نوبت حریف است. شما 🟡 هستید.",
            c4f_board_kb(board_str, match.pk),
        )


def _start_offline_game(chat_id, user, mk):
    """Start a game against the bot."""
    status = user.status
    game_type = 'rps'
    if 'ttt' in status:
        game_type = 'ttt'
    elif 'c4f' in status:
        game_type = 'c4f'
    elif status.startswith('bet_'):
        game_type = status.split('_')[1]

    fees = {'rps': RPS_OFFLINE_FEE, 'ttt': TTT_OFFLINE_FEE, 'c4f': C4F_OFFLINE_FEE}
    fee = fees.get(game_type, RPS_OFFLINE_FEE)

    if user.balance_cents < fee:
        user.status = 'idle'
        user.save(update_fields=['status'])
        return send_message(
            chat_id,
            f"❌ موجودی ناکافی!\n💰 موجودی: *{_fmt(user.balance_cents)}*\n"
            f"هزینه بازی آفلاین: *{_fmt(fee)}*",
            mk,
        )

    user.balance_cents -= fee
    match = GameMatch.objects.create(
        game_type=game_type,
        player1=user,
        is_offline=True,
        bet_cents=0,
        search_fee_cents=0,
        status='active',
    )
    user.status = f'playing_{game_type}_{match.pk}'
    user.save(update_fields=['balance_cents', 'status'])

    if game_type == 'rps':
        send_message(chat_id,
            "🤖 *بازی آفلاین – سنگ کاغذ قیچی*\n\nحرکت خود را انتخاب کنید:",
            rps_move_kb())
    elif game_type == 'ttt':
        send_message(chat_id,
            "🤖 *بازی آفلاین – دوز*\n\nشما ❌ هستید. نوبت شماست!\nیک خانه را انتخاب کنید:",
            ttt_board_kb(match.ttt_board, match.pk))
    elif game_type == 'c4f':
        send_message(chat_id,
            "🤖 *بازی آفلاین – چهار در یک*\n\nشما 🔴 هستید. نوبت شماست!\nیک ستون را انتخاب کنید:",
            c4f_board_kb(match.c4f_board, match.pk))


def _cancel_search(chat_id, user, mk):
    """Cancel search and refund fee."""
    status = user.status
    if not status.startswith('searching_'):
        return send_message(chat_id, "⚠️ جستجویی فعال نیست.", mk)
    match_id = int(status.split('_')[1])
    try:
        match = GameMatch.objects.get(pk=match_id, status='searching')
        if match.search_fee_cents > 0:
            user.balance_cents += match.search_fee_cents
        # Also refund bet
        if match.bet_cents > 0:
            user.balance_cents += match.bet_cents
        match.status = 'cancelled'
        match.save(update_fields=['status'])
    except GameMatch.DoesNotExist:
        pass
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])
    send_message(chat_id, "✅ جستجو لغو شد. هزینه به کیف پول شما بازگشت.", mk)


def _handle_rps_move(chat_id, user, text, mk):
    status = user.status
    if not status.startswith('playing_rps_'):
        return
    match_id = int(status.split('_')[2])
    try:
        match = GameMatch.objects.select_related('player1', 'player2').get(pk=match_id)
    except GameMatch.DoesNotExist:
        return send_message(chat_id, "⚠️ بازی یافت نشد.", mk)

    if text not in ("🪨 سنگ", "📄 کاغذ", "✂️ قیچی"):
        return

    if match.player1.chat_id == chat_id:
        if match.p1_move:
            return send_message(chat_id, "⏳ حرکت شما ثبت شده، منتظر حریف...")
        match.p1_move = text
    else:
        if match.p2_move:
            return send_message(chat_id, "⏳ حرکت شما ثبت شده، منتظر حریف...")
        match.p2_move = text
    match.save(update_fields=['p1_move', 'p2_move'])

    if match.is_offline:
        # Bot makes its move
        bot_moves = ["🪨 سنگ", "📄 کاغذ", "✂️ قیچی"]
        match.p2_move = random.choice(bot_moves)
        match.save(update_fields=['p2_move'])
        _finish_rps(match, mk)
    elif match.p1_move and match.p2_move:
        _finish_rps(match, mk)
    else:
        send_message(chat_id, "✅ حرکت ثبت شد. ⏳ منتظر حریف...")


def _finish_rps(match: GameMatch, mk):
    p1 = match.player1
    p2 = match.player2
    m1, m2 = match.p1_move, match.p2_move

    wins_over = {"🪨 سنگ": "✂️ قیچی", "📄 کاغذ": "🪨 سنگ", "✂️ قیچی": "📄 کاغذ"}

    if m1 == m2:
        result = 'draw'
    elif wins_over[m1] == m2:
        result = 'p1'
    else:
        result = 'p2'

    p1_mk = main_menu(_is_admin(p1.chat_id))
    p2_mk = main_menu(_is_admin(p2.chat_id)) if p2 else None

    if result == 'draw':
        # Refund both
        p1.balance_cents += match.bet_cents
        p1.wins += 0; p1.losses += 0; p1.total_games += 1
        send_message(p1.chat_id, f"🤝 *مساوی!*\n\nشما: {m1}\nحریف: {m2}\n\n💰 شرط برگشت داده شد.", p1_mk)
        if p2 and not match.is_offline:
            p2.balance_cents += match.bet_cents
            p2.total_games += 1
            send_message(p2.chat_id, f"🤝 *مساوی!*\n\nشما: {m2}\nحریف: {m1}\n\n💰 شرط برگشت داده شد.", p2_mk)

    elif result == 'p1':
        if match.is_offline:
            prize = RPS_OFFLINE_WIN
            p1.balance_cents += prize; p1.wins += 1; p1.total_games += 1
            send_message(p1.chat_id,
                f"🎉 *بردید!*\n\nشما: {m1}\nربات: {m2}\n\n💰 جایزه: *{_fmt(prize)}* به کیف پول اضافه شد.", p1_mk)
        else:
            # Winner takes both bets (search fees already deducted)
            prize = match.bet_cents * 2
            p1.balance_cents += prize; p1.wins += 1; p1.total_games += 1
            send_message(p1.chat_id,
                f"🎉 *بردید!*\n\nشما: {m1}\nحریف: {m2}\n\n💰 برنده: *{_fmt(prize)}*", p1_mk)
            if p2:
                p2.losses += 1; p2.total_games += 1
                send_message(p2.chat_id,
                    f"💀 *باختید!*\n\nشما: {m2}\nحریف: {m1}", p2_mk)

    else:  # p2 wins
        if match.is_offline:
            p1.losses += 1; p1.total_games += 1
            send_message(p1.chat_id,
                f"💀 *باختید!*\n\nشما: {m1}\nربات: {m2}", p1_mk)
        else:
            prize = match.bet_cents * 2
            p2.balance_cents += prize; p2.wins += 1; p2.total_games += 1
            send_message(p2.chat_id,
                f"🎉 *بردید!*\n\nشما: {m2}\nحریف: {m1}\n\n💰 برنده: *{_fmt(prize)}*", p2_mk)
            p1.losses += 1; p1.total_games += 1
            send_message(p1.chat_id,
                f"💀 *باختید!*\n\nشما: {m1}\nحریف: {m2}", p1_mk)

    p1.status = 'idle'; p1.save()
    if p2: p2.status = 'idle'; p2.save()
    match.status = 'finished'; match.finished_at = timezone.now(); match.save()


# ─── Tic-Tac-Toe (callback handled in views.py) ──────────────────────────────

def handle_ttt_move(match: GameMatch, player: BotUser, position: int) -> str:
    """
    Called from callback handler in views.py.
    Returns a status string: 'ok', 'not_your_turn', 'invalid', 'already_done'.
    """
    if match.status != 'active':
        return 'already_done'

    is_p1 = match.player1.chat_id == player.chat_id
    is_p2 = match.player2 and match.player2.chat_id == player.chat_id
    if not is_p1 and not is_p2:
        return 'invalid'

    expected_turn = 1 if is_p1 else 2
    if match.ttt_turn != expected_turn:
        return 'not_your_turn'

    symbol = 'X' if is_p1 else 'O'
    if not match.ttt_make_move(position, symbol):
        return 'invalid'

    match.ttt_turn = 2 if is_p1 else 1
    match.save(update_fields=['ttt_board', 'ttt_turn'])

    winner = match.ttt_check_winner()

    if match.is_offline and winner is None:
        # Bot plays immediately
        bot_pos = match.ttt_bot_move()
        match.ttt_make_move(bot_pos, 'O')
        match.ttt_turn = 1
        match.save(update_fields=['ttt_board', 'ttt_turn'])
        winner = match.ttt_check_winner()

    if winner:
        _finish_ttt(match, winner)
    else:
        _send_ttt_board(match)

    return 'ok'


def _finish_ttt(match: GameMatch, winner):
    p1 = match.player1
    p2 = match.player2
    p1_mk = main_menu(_is_admin(p1.chat_id))
    p2_mk = main_menu(_is_admin(p2.chat_id)) if p2 else None
    board_visual = _ttt_visual(match.ttt_board)

    if winner == 'draw':
        p1.balance_cents += match.bet_cents
        p1.total_games += 1; p1.save()
        send_message(p1.chat_id, f"🤝 *مساوی!*\n\n{board_visual}\n\n💰 شرط برگشت داده شد.", p1_mk)
        if p2 and not match.is_offline:
            p2.balance_cents += match.bet_cents
            p2.total_games += 1; p2.save()
            send_message(p2.chat_id, f"🤝 *مساوی!*\n\n{board_visual}\n\n💰 شرط برگشت داده شد.", p2_mk)

    elif winner == 'X':  # p1 wins
        if match.is_offline:
            prize = TTT_OFFLINE_WIN
            p1.balance_cents += prize; p1.wins += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id,
                f"🎉 *بردید!*\n\n{board_visual}\n\n💰 جایزه: *{_fmt(prize)}*", p1_mk)
        else:
            prize = match.bet_cents * 2
            p1.balance_cents += prize; p1.wins += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id,
                f"🎉 *بردید!*\n\n{board_visual}\n\n💰 برنده: *{_fmt(prize)}*", p1_mk)
            if p2:
                p2.losses += 1; p2.total_games += 1; p2.save()
                send_message(p2.chat_id, f"💀 *باختید!*\n\n{board_visual}", p2_mk)

    else:  # O wins = p2
        if match.is_offline:
            p1.losses += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id, f"💀 *باختید!*\n\n{board_visual}", p1_mk)
        else:
            prize = match.bet_cents * 2
            p2.balance_cents += prize; p2.wins += 1; p2.total_games += 1; p2.save()
            send_message(p2.chat_id,
                f"🎉 *بردید!*\n\n{board_visual}\n\n💰 برنده: *{_fmt(prize)}*", p2_mk)
            p1.losses += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id, f"💀 *باختید!*\n\n{board_visual}", p1_mk)

    p1.status = 'idle'; p1.save(update_fields=['status'])
    if p2: p2.status = 'idle'; p2.save(update_fields=['status'])
    match.status = 'finished'; match.finished_at = timezone.now(); match.save()


def _send_ttt_board(match: GameMatch):
    """Update both players with the current board."""
    p1 = match.player1
    p2 = match.player2
    board = match.ttt_board

    if match.ttt_turn == 1:
        turn_p1 = "✅ *نوبت شماست* (❌)"
        turn_p2 = "⏳ نوبت حریف است"
    else:
        turn_p1 = "⏳ نوبت حریف است"
        turn_p2 = "✅ *نوبت شماست* (⭕)"

    send_message(p1.chat_id, turn_p1, ttt_board_kb(board, match.pk))
    if p2:
        send_message(p2.chat_id, turn_p2, ttt_board_kb(board, match.pk))


def _ttt_visual(board: str) -> str:
    emojis = {'.': '⬜', 'X': '❌', 'O': '⭕'}
    rows = []
    for r in range(3):
        row = ''.join(emojis[board[r*3+c]] for c in range(3))
        rows.append(row)
    return '\n'.join(rows)


# ─── Friends ─────────────────────────────────────────────────────────────────

def _friends_menu(chat_id, user):
    req_count = FriendRequest.objects.filter(receiver=user, status='pending').count()
    badge = f" ({req_count} 🔔)" if req_count else ""
    send_message(
        chat_id,
        f"👥 *دوستان*\n\n"
        f"📨 درخواست‌های دریافتی{badge}\n\n"
        "برای افزودن دوست، یوزرنیم یا chat_id آن‌ها را وارد کنید:",
        friends_menu_kb(),
    )


def _list_friends(chat_id, user):
    friends = Friendship.get_friends(user)
    if not friends:
        return send_message(chat_id, "👥 هنوز دوستی اضافه نکرده‌اید.", friends_menu_kb())
    lines = []
    for f in friends[:20]:
        name = f.full_name or f.username or str(f.chat_id)
        lines.append(f"• {name} – 🏆{f.wins} برد")
    send_message(chat_id, "👥 *دوستان شما:*\n\n" + '\n'.join(lines), friends_menu_kb())


def _show_friend_requests(chat_id, user):
    requests_qs = FriendRequest.objects.filter(receiver=user, status='pending').select_related('sender')[:10]
    if not requests_qs:
        return send_message(chat_id, "📭 درخواست دوستی جدیدی ندارید.", friends_menu_kb())
    for req in requests_qs:
        sender = req.sender
        name = sender.full_name or sender.username or str(sender.chat_id)
        send_message(
            chat_id,
            f"📨 *درخواست دوستی*\n\n"
            f"👤 از: *{name}*\n"
            f"🏆 برد: {sender.wins} | ❌ باخت: {sender.losses}\n"
            f"📊 نرخ برد: {sender.win_rate}",
            friend_req_inline_kb(req.pk),
        )


def _handle_search_friend(chat_id, user, text, mk):
    if text == "🔙 بازگشت":
        _reset_user(user); return _friends_menu(chat_id, user)
    target = None
    if text and text.lstrip('@').lstrip('-').isdigit():
        try:
            target = BotUser.objects.get(chat_id=int(text.strip()))
        except BotUser.DoesNotExist:
            pass
    elif text:
        uname = text.lstrip('@').strip()
        target = BotUser.objects.filter(username__iexact=uname).first()

    if not target:
        return send_message(chat_id, "❌ کاربری با این مشخصات یافت نشد.", back_kb())
    if target.chat_id == chat_id:
        return send_message(chat_id, "⚠️ نمی‌توانید خودتان را دنبال کنید!", back_kb())
    if Friendship.are_friends(user, target):
        return send_message(chat_id, "✅ شما قبلاً با این کاربر دوست هستید.", friends_menu_kb())

    # Check for existing request
    if FriendRequest.objects.filter(sender=user, receiver=target, status='pending').exists():
        return send_message(chat_id, "⏳ درخواست دوستی قبلاً ارسال شده، منتظر پاسخ باشید.", friends_menu_kb())

    if user.balance_cents < FRIEND_REQ_FEE:
        return send_message(
            chat_id,
            f"❌ موجودی ناکافی!\n💰 هزینه ارسال درخواست دوستی: *{_fmt(FRIEND_REQ_FEE)}*\n"
            f"💰 موجودی شما: *{_fmt(user.balance_cents)}*",
            friends_menu_kb(),
        )

    user.balance_cents -= FRIEND_REQ_FEE
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])

    req = FriendRequest.objects.create(sender=user, receiver=target, fee_paid=True)
    sender_name = user.full_name or user.username or str(user.chat_id)

    send_message(
        target.chat_id,
        f"📨 *درخواست دوستی جدید!*\n\n"
        f"👤 از: *{sender_name}*\n"
        f"🏆 برد: {user.wins} | نرخ: {user.win_rate}",
        friend_req_inline_kb(req.pk),
    )
    send_message(chat_id, f"✅ درخواست دوستی به *{target.full_name or target.username}* ارسال شد!", friends_menu_kb())


def _invite_friend_to_game(chat_id, user):
    friends = Friendship.get_friends(user)
    if not friends:
        return send_message(chat_id, "👥 هنوز دوستی ندارید. ابتدا یک دوست اضافه کنید.", friends_menu_kb())
    # Show selection (simple text list for now)
    msg = "🎮 *دعوت به بازی*\n\nchat_id دوستتان را وارد کنید:\n\n"
    for f in friends[:10]:
        name = f.full_name or f.username or str(f.chat_id)
        msg += f"• `{f.chat_id}` – {name}\n"
    user.status = 'invite_friend_game'
    user.save(update_fields=['status'])
    send_message(chat_id, msg, back_kb())


# ─── Report ───────────────────────────────────────────────────────────────────

def _start_report(chat_id, user, reported_id, mk):
    try:
        reported = BotUser.objects.get(chat_id=reported_id)
    except BotUser.DoesNotExist:
        _reset_user(user)
        return send_message(chat_id, "❌ کاربری با این chat_id یافت نشد.", mk)

    user.status = f'report_reason_{reported.chat_id}'
    user.save(update_fields=['status'])
    send_message(
        chat_id,
        f"📝 دلیل گزارش برای *{reported.full_name or reported.chat_id}* را بنویسید:",
        back_kb(),
    )


def _handle_report_reason(chat_id, user, text, status, mk):
    if text == "🔙 بازگشت":
        _reset_user(user); return send_message(chat_id, "❌ گزارش لغو شد.", mk)
    reported_id = int(status.split('_')[2])
    try:
        reported = BotUser.objects.get(chat_id=reported_id)
    except BotUser.DoesNotExist:
        _reset_user(user)
        return send_message(chat_id, "❌ کاربر یافت نشد.", mk)

    report = Report.objects.create(reporter=user, reported=reported, reason=text or "")
    _reset_user(user)

    from rps.tg_api import send_message_direct
    from rps.keyboards import admin_report_inline_kb
    reporter_name = user.full_name or user.username or str(user.chat_id)
    reported_name = reported.full_name or reported.username or str(reported.chat_id)
    admin_msg = (
        f"🚨 *گزارش جدید* `#{report.pk}`\n\n"
        f"📤 گزارش‌دهنده: *{reporter_name}* (`{user.chat_id}`)\n"
        f"📥 گزارش‌شده: *{reported_name}* (`{reported.chat_id}`)\n"
        f"📝 دلیل: _{text}_"
    )
    send_message_direct(TELEGRAM_ADMIN_CHAT_ID, admin_msg, admin_report_inline_kb(report.pk))
    send_message(chat_id, "✅ گزارش شما ثبت شد و به تیم پشتیبانی ارسال گردید.", mk)


# ─── Connect Four (callback handled in views.py) ─────────────────────────────

def handle_c4f_move(match: GameMatch, player: BotUser, col: int) -> str:
    """
    Called from callback handler in views.py.
    Returns: 'ok' | 'not_your_turn' | 'col_full' | 'already_done'
    """
    if match.status != 'active':
        return 'already_done'

    is_p1 = match.player1.chat_id == player.chat_id
    is_p2 = match.player2 and match.player2.chat_id == player.chat_id
    if not is_p1 and not is_p2:
        return 'not_your_turn'

    expected_turn = 1 if is_p1 else 2
    if match.c4f_turn != expected_turn:
        return 'not_your_turn'

    symbol = 'R' if is_p1 else 'Y'
    row = match.c4f_drop(col, symbol)
    if row == -1:
        return 'col_full'

    match.c4f_turn = 2 if is_p1 else 1
    match.save(update_fields=['c4f_board', 'c4f_turn'])

    winner = match.c4f_check_winner()

    if match.is_offline and winner is None:
        # Bot plays immediately
        bot_col = match.c4f_bot_move()
        match.c4f_drop(bot_col, 'Y')
        match.c4f_turn = 1
        match.save(update_fields=['c4f_board', 'c4f_turn'])
        winner = match.c4f_check_winner()

    if winner:
        _finish_c4f(match, winner)
    else:
        _send_c4f_board(match)

    return 'ok'


def _finish_c4f(match: GameMatch, winner: str):
    p1 = match.player1
    p2 = match.player2
    p1_mk = main_menu(_is_admin(p1.chat_id))
    p2_mk = main_menu(_is_admin(p2.chat_id)) if p2 else None
    board_visual = _c4f_visual(match.c4f_board)

    if winner == 'draw':
        p1.balance_cents += match.bet_cents
        p1.total_games += 1; p1.save()
        send_message(p1.chat_id,
            f"🤝 *مساوی!*\n\n{board_visual}\n\n💰 شرط برگشت داده شد.", p1_mk)
        if p2 and not match.is_offline:
            p2.balance_cents += match.bet_cents
            p2.total_games += 1; p2.save()
            send_message(p2.chat_id,
                f"🤝 *مساوی!*\n\n{board_visual}\n\n💰 شرط برگشت داده شد.", p2_mk)

    elif winner == 'R':  # p1 wins
        if match.is_offline:
            prize = C4F_OFFLINE_WIN
            p1.balance_cents += prize; p1.wins += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id,
                f"🎉 *بردید!*\n\n{board_visual}\n\n💰 جایزه: *{_fmt(prize)}*", p1_mk)
        else:
            prize = match.bet_cents * 2
            p1.balance_cents += prize; p1.wins += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id,
                f"🎉 *بردید!*\n\n{board_visual}\n\n🏆 برنده: *{_fmt(prize)}*", p1_mk)
            if p2:
                p2.losses += 1; p2.total_games += 1; p2.save()
                send_message(p2.chat_id,
                    f"💀 *باختید!*\n\n{board_visual}", p2_mk)

    else:  # 'Y' = p2 wins
        if match.is_offline:
            p1.losses += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id,
                f"💀 *باختید!*\n\n{board_visual}", p1_mk)
        else:
            prize = match.bet_cents * 2
            p2.balance_cents += prize; p2.wins += 1; p2.total_games += 1; p2.save()
            send_message(p2.chat_id,
                f"🎉 *بردید!*\n\n{board_visual}\n\n🏆 برنده: *{_fmt(prize)}*", p2_mk)
            p1.losses += 1; p1.total_games += 1; p1.save()
            send_message(p1.chat_id,
                f"💀 *باختید!*\n\n{board_visual}", p1_mk)

    p1.status = 'idle'; p1.save(update_fields=['status'])
    if p2: p2.status = 'idle'; p2.save(update_fields=['status'])
    match.status = 'finished'; match.finished_at = timezone.now(); match.save()


def _send_c4f_board(match: GameMatch):
    """Update both players with current board state."""
    p1 = match.player1
    p2 = match.player2
    board = match.c4f_board

    kb = c4f_board_kb(board, match.pk)

    if match.c4f_turn == 1:
        send_message(p1.chat_id, "✅ *نوبت شماست!* 🔴 یک ستون انتخاب کنید:", kb)
        if p2:
            send_message(p2.chat_id, "⏳ نوبت حریف است... 🟡", kb)
    else:
        if p2:
            send_message(p2.chat_id, "✅ *نوبت شماست!* 🟡 یک ستون انتخاب کنید:", kb)
        send_message(p1.chat_id, "⏳ نوبت حریف است... 🔴", kb)


def _c4f_visual(board: str) -> str:
    """Render the 6×7 board as emoji text for result messages."""
    cell = {'.': '⬜', 'R': '🔴', 'Y': '🟡'}
    col_nums = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    rows = [col_nums]
    for row in range(5, -1, -1):  # top to bottom
        rows.append(''.join(cell[board[row * 7 + col]] for col in range(7)))
    return '\n'.join(rows)


# ─── Leaderboard ──────────────────────────────────────────────────────────────

def _show_leaderboard(chat_id, mk):
    top = BotUser.objects.order_by('-wins')[:10]
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, u in enumerate(top):
        name = (u.full_name or u.username or str(u.chat_id))[:15]
        lines.append(f"{medals[i]} *{name}* — {u.wins} برد ({u.win_rate})")
    msg = "🏆 *رتبه‌بندی برتر*\n\n" + '\n'.join(lines) if lines else "هنوز بازی‌ای انجام نشده است."
    send_message(chat_id, msg, mk)


# ─── Help ─────────────────────────────────────────────────────────────────────

def _show_help(chat_id, mk):
    send_message(
        chat_id,
        "❓ *راهنمای ربات*\n\n"
        "🎮 *بازی‌ها:*\n"
        "• *دوز (TTT)*: هزینه جستجو $0.30، شرط: $0.50–$2\n"
        "• *سنگ کاغذ قیچی*: هزینه جستجو $0.20، شرط: $0.30–$5\n"
        "• *چهار در یک*: هزینه جستجو $0.30، شرط: $0.50–$5\n"
        "• *آفلاین (با ربات)*: هزینه $0.05، جایزه برد $0.35\n\n"
        "🔴 *چهار در یک:*\n"
        "تخته ۶×۷ است. مهره بیندازید (⬇️ ستون).\n"
        "اولین کسی که ۴ مهره پشت سر هم (افقی، عمودی یا مورب) بچیند می‌برد!\n\n"
        "💰 *کیف پول:*\n"
        "• شارژ از طریق کارت یا کریپتو\n"
        "• برداشت حداقل $15 به کیف پول ترون\n\n"
        "👥 *دوستان:*\n"
        "• هزینه ارسال درخواست دوستی: $0.10\n"
        "• هزینه دعوت به بازی: $0.20\n\n"
        "🔗 *دعوت:*\n"
        "• دعوت دوست + تکمیل پروفایل = $0.50 برای هر دو\n\n"
        "⏱ *جستجو:*\n"
        "• اگر در ۵ دقیقه حریف پیدا نشد، هزینه برگشت داده می‌شود.",
        mk,
    )


# ─── Admin ────────────────────────────────────────────────────────────────────

def _admin_panel(chat_id, user, mk):
    total = BotUser.objects.count()
    active_games = GameMatch.objects.filter(status='active').count()
    pending_w = WithdrawalRequest.objects.filter(status='pending').count()
    pending_d = DepositRequest.objects.filter(status='pending').count()
    pending_c = CryptoDepositRequest.objects.filter(status='pending').count()
    send_message(
        chat_id,
        f"⚙️ *پنل مدیریت*\n\n"
        f"👤 کاربران: *{total}*\n"
        f"🎮 بازی‌های فعال: *{active_games}*\n"
        f"💸 برداشت در انتظار: *{pending_w}*\n"
        f"💳 واریز کارتی در انتظار: *{pending_d}*\n"
        f"🪙 واریز کریپتو در انتظار: *{pending_c}*\n\n"
        "برای ارسال پیام همگانی، پیام خود را تایپ کنید:",
        mk,
    )
    user.status = 'wait_broadcast'
    user.save(update_fields=['status'])


def _handle_broadcast(chat_id, user, text, mk):
    if text in ("🔙 بازگشت", "/start"):
        _reset_user(user)
        return send_message(chat_id, "❌ ارسال لغو شد.", mk)
    all_users = BotUser.objects.filter(is_banned=False)
    total = all_users.count()
    send_message(chat_id, f"⏳ در حال ارسال به {total} کاربر...")
    success = fail = 0
    for u in all_users:
        try:
            send_message(u.chat_id, text)
            success += 1
        except Exception:
            fail += 1
    _reset_user(user)
    send_message(chat_id, f"✅ پایان\n✔️ موفق: {success}\n❌ ناموفق: {fail}", mk)