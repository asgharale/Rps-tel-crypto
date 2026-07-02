"""
logic.py  –  Main bot FSM dispatcher.

Design principles:
  - All state is stored in BotUser.status (string).
  - Money is in integer cents (avoid float).
  - Celery is used for ALL outbound messages that don't need a message_id back.
  - Turn-based games (TTT / Connect Four / Minesweeper) are updated in-place
    with editMessageText instead of sending a new message every turn.
  - Inline callbacks are routed through views.py → handle_callback().
  - Games: RPS (best of 3), Tic-Tac-Toe, Connect Four, Minesweeper (solo).

Money / fairness note:
  There is NO betting or wagering anywhere in this bot. Every paid game has
  a fixed, published entry fee and a fixed prize the winner receives — the
  amounts never depend on what an opponent chooses to risk.
"""

import os
import re
import time
import random
import requests
from django.utils import timezone
from django.core.files.base import ContentFile

from rps.models import (
    BotUser, Province, GameMatch, FriendRequest, Friendship,
    WithdrawalRequest, DepositRequest, CryptoDepositRequest, Report,
    BroadcastJob,
)
from rps.tg_api import send_message, send_message_direct, download_tg_file
from rps.keyboards import (
    main_menu, back_kb, cancel_search_kb,
    game_mode_kb, level_kb, LEVEL_BUTTON_LABELS,
    rps_move_kb, ttt_board_kb, c4f_board_kb, ms_board_kb,
    profile_menu_kb, province_kb, wallet_menu_kb,
    deposit_amount_kb, deposit_method_kb, crypto_proof_kb,
    friends_menu_kb, friend_pick_kb,
    admin_report_inline_kb, admin_withdrawal_inline_kb, admin_deposit_inline_kb,
    broadcast_cancel_kb,
    game_invite_inline_kb, friend_req_inline_kb,
)

# ─── Config ───────────────────────────────────────────────────────────────────

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "8093967783")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()]

TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "8093967783")
TELEGRAM_TOKEN         = os.getenv("TELEGRAM_TOKEN", "")
BOT_USERNAME           = os.getenv("BOT_USERNAME", "your_bot")

# ── Fixed levels for every competitive game: entry fee → prize for the winner.
# These numbers never change based on an opponent — there is no wagering.
LEVELS = {
    'intermediate': {'label': 'متوسط',   'entry': 30,  'prize': 50},
    'master':       {'label': 'حرفه‌ای', 'entry': 60,  'prize': 100},
    'gods':         {'label': 'خدایان',  'entry': 100, 'prize': 180},
}
LEVEL_ORDER = ['intermediate', 'master', 'gods']

# Playing with a friend is intentionally cheap and simple: flat $0.10 entry
# each, winner receives the combined $0.20 as a prize.
FRIENDLY_ENTRY_FEE = 10   # $0.10
FRIENDLY_PRIZE     = 20   # $0.20

MIN_WITHDRAWAL  = 1000    # $10.00

REFERRAL_BONUS  = 50      # $0.50 on join (if inviter exists; full bonus on profile complete)
SIGNUP_BONUS    = 50      # $0.50 given to every new user immediately on /start

GAME_NAMES = {
    'rps': 'سنگ کاغذ قیچی',
    'ttt': 'دوز',
    'c4f': 'چهار در یک',
    'ms':  'ماین‌یاب',
}

GAME_DESCRIPTIONS = {
    'rps': (
        "✊📄✂️ *سنگ کاغذ قیچی*\n\n"
        "بازی سریع و کلاسیک! هر مسابقه *سه دور* دارد (Best of 3) — "
        "هرکس زودتر ۲ دور را ببرد، برنده کل مسابقه است.\n"
        "در صورت مساوی شدن یک دور، همان دور دوباره تکرار می‌شود."
    ),
    'ttt': (
        "🎯 *دوز (Tic-Tac-Toe)*\n\n"
        "روی جدول کلاسیک *۳ در ۳* به‌نوبت علامت می‌گذارید. "
        "اولین کسی که سه‌تایی (افقی، عمودی یا مورب) بسازد برنده است."
    ),
    'c4f': (
        "🔴 *چهار در یک (Connect Four)*\n\n"
        "مهره‌های خود را در یکی از ۷ ستون رها می‌کنید. "
        "اولین کسی که ۴ مهره پشت‌سرهم (افقی، عمودی یا مورب) بچیند برنده است."
    ),
    'ms': (
        "💣 *ماین‌یاب (Minesweeper)*\n\n"
        "یک بازی *تک‌نفره* کلاسیک؛ رقیب ندارید، فقط با تخته بازی می‌کنید! "
        "خانه‌های امن را باز کنید بدون اینکه به مین بخورید. "
        "اگر تمام خانه‌های امن را پیدا کنید، جایزه را می‌برید؛ "
        "اگر به مین بخورید، فقط هزینه ورود را از دست می‌دهید."
    ),
}

MS_MINES = 6  # constant difficulty on the 5×6 board


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_IDS


def _fmt(cents: int) -> str:
    return f"${cents/100:.2f}"


def _display_name(u: BotUser) -> str:
    return u.full_name or u.username or str(u.chat_id)


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


def _level_from_text(text: str):
    for lvl, label in LEVEL_BUTTON_LABELS.items():
        if text == label:
            return lvl
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
    if status.startswith('wait_withdrawal_amount'):
        return _handle_withdrawal_amount(chat_id, user, text, mk)

    # Search for friend
    if status == 'search_friend':
        return _handle_search_friend(chat_id, user, text, mk)

    # Report reason
    if status.startswith('report_reason_'):
        return _handle_report_reason(chat_id, user, text, status, mk)

    # Game: description → mode choice (search online / bot / friends)
    if status.startswith('gamechoice_'):
        return _handle_game_mode(chat_id, user, text, mk)

    # Game: level choice (entry fee → prize)
    if status.startswith('levelpick_'):
        return _handle_level_pick(chat_id, user, text, mk)

    # Game: choosing a friend to invite
    if status.startswith('friendpick_'):
        return _handle_friend_pick(chat_id, user, text, mk)

    # RPS move (reply-keyboard based)
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

    if text == "💣 ماین‌یاب":
        return _game_menu(chat_id, user, 'ms', mk)

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

    if text == "🏙 ویرایش استان":
        user.status = 'edit_province'; user.save(update_fields=['status'])
        provinces = Province.objects.filter(is_active=True)
        return send_message(chat_id, "🏙 استان خود را انتخاب کنید:", province_kb(provinces))

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

    # Search cancel
    if text == "❌ انصراف از جستجو":
        return _cancel_search(chat_id, user, mk)

    # RPS moves (fallback, normally caught by the 'playing_rps_' status prefix above)
    if text in ("🪨 سنگ", "📄 کاغذ", "✂️ قیچی"):
        return _handle_rps_move(chat_id, user, text, mk)

    # Friends sub-menu
    if text == "👥 لیست دوستان":
        return _list_friends(chat_id, user)

    if text == "📨 درخواست‌های دریافتی":
        return _show_friend_requests(chat_id, user)

    if text == "🔍 افزودن دوست":
        user.status = 'search_friend'; user.save(update_fields=['status'])
        return send_message(chat_id, "🔍 یوزرنیم یا chat_id دوستتان را وارد کنید:", back_kb())

    if text == "🎮 دعوت به بازی":
        return send_message(
            chat_id,
            "🎮 برای دعوت یک دوست به بازی، ابتدا از منوی اصلی بازی مورد نظر را باز کنید "
            "و گزینه‌ی «👥 بازی با دوستان» را انتخاب کنید.",
            friends_menu_kb(),
        )

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
    province_str = user.province.name if (user.province and user.province.name) else "تنظیم نشده"
    tron_str = f"`{user.tron_wallet}`" if user.tron_wallet else "تنظیم نشده"
    msg = (
        "👤 *پروفایل شما*\n"
        "─────────────\n"
        f"📛 نام: *{user.full_name}*\n"
        f"🎂 سن: *{user.age}*\n"
        f"📱 شماره: {phone_str}\n"
        f"🏙 استان: {province_str}\n"
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

    if status == 'edit_province':
        if not text:
            return send_message(chat_id, "⚠️ یک استان را از دکمه‌ها انتخاب کنید:")
        province = Province.objects.filter(is_active=True, name=text.strip()).first()
        if not province:
            provinces = Province.objects.filter(is_active=True)
            return send_message(chat_id, "⚠️ استان یافت نشد، از دکمه‌های زیر انتخاب کنید:", province_kb(provinces))
        user.province = province
        user.status = 'idle'
        user.save(update_fields=['province', 'status'])
        return send_message(chat_id, f"✅ استان به *{province.name}* تغییر یافت.", profile_menu_kb())

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
    """User tapped a $1/$5/$10/$20 button (card-payment path only)."""
    match = re.search(r'\$([\d]+)', text)
    if not match:
        return
    dollars = int(match.group(1))
    valid = [1, 5, 10, 20]
    if dollars not in valid:
        return send_message(chat_id, "⚠️ مبلغ نامعتبر است.", mk)
    cents = dollars * 100
    user.status = f'deposit_receipt_{cents}'
    user.save(update_fields=['status'])
    send_message(
        chat_id,
        f"💳 *واریز {_fmt(cents)} از طریق کارت*\n\n"
        "لطفاً رسید پرداخت خود را به‌صورت عکس ارسال کنید:",
        back_kb(),
    )


def _handle_deposit_flow(chat_id, user, text, photo_id, mk):
    status = user.status

    if text == "🔙 بازگشت":
        _reset_user(user)
        return _wallet_menu(chat_id, user)

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
        "مثال: `10` یا `20`",
        back_kb(),
    )


def _handle_withdrawal_amount(chat_id, user, text, mk):
    if text == "🔙 بازگشت":
        _reset_user(user); return _wallet_menu(chat_id, user)
    parts = user.status.split(':', 1)
    wallet = parts[1] if len(parts) > 1 else ''
    if not text or not text.strip().replace('.', '').isdigit():
        return send_message(chat_id, "⚠️ یک عدد معتبر وارد کنید (مثل 10 یا 20):")
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

CRYPTO_WALLETS = {
    "USDT (TRC20)": os.getenv("WALLET_USDT_TRC20", "TSEfwvtG48EoAXkP7HnbYsCxm7AtQXhUSu"),
}


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
        user.status = f'crypto_amount:{coin_safe}:screenshot:{photo_id}'
        user.save(update_fields=['status'])
        return send_message(chat_id, "✅ اسکرین‌شات دریافت شد.\n💵 مبلغ واریزی را به دلار وارد کنید (مثلاً: 10):")

    if status.startswith('crypto_wait_ss:') and not photo_id:
        return send_message(chat_id, "📸 تصویر اسکرین‌شات را ارسال کنید:", back_kb())

    if status.startswith('crypto_wait_tx:'):
        coin_safe = status.split(':', 1)[1]
        if not text or not text.strip():
            return send_message(chat_id, "🔢 کد پیگیری یا TxHash را وارد کنید:", back_kb())
        user.status = f'crypto_amount:{coin_safe}:tracking:{text.strip()[:200]}'
        user.save(update_fields=['status'])
        return send_message(chat_id, "✅ کد دریافت شد.\n💵 مبلغ واریزی را به دلار وارد کنید (مثلاً: 10):")

    if status.startswith('crypto_amount:'):
        parts = status.split(':', 3)
        _, coin_safe, proof_type, proof_data = parts
        if not text or not text.strip().replace('.', '').isdigit():
            return send_message(chat_id, "⚠️ یک عدد معتبر وارد کنید:")
        dollars = float(text.strip())
        if dollars < 1:
            return send_message(chat_id, "⚠️ حداقل مبلغ ۱ دلار است.")
        cents = round(dollars * 100)
        coin_name = coin_safe.replace("_", " ")
        req = CryptoDepositRequest.objects.create(
            user=user, coin=coin_name,
            amount_cents=cents, proof_type=proof_type, proof_data=proof_data,
        )
        user.status = 'idle'
        user.save(update_fields=['status'])
        _notify_admin_crypto(req, photo_id=proof_data if proof_type == 'screenshot' else None)
        return send_message(
            chat_id,
            f"✅ *درخواست واریز ثبت شد!*\n\n"
            f"💵 مبلغ: *{_fmt(req.amount_cents)}*\n"
            f"🔖 شماره: `#{req.pk}`\n\n"
            "پس از بررسی توسط ادمین، موجودی شما شارژ می‌شود.",
            mk,
        )


def _notify_admin_crypto(req: CryptoDepositRequest, photo_id=None):
    from rps.keyboards import admin_deposit_inline_kb
    caption = (
        f"🪙 *درخواست واریز کریپتو* `#{req.pk}`\n\n"
        f"👤 کاربر: `{req.user.chat_id}`\n"
        f"📛 نام: {req.user.full_name}\n"
        f"💵 مبلغ: *{_fmt(req.amount_cents)}*\n"
        f"🧾 نوع مدرک: {req.get_proof_type_display()}\n"
    )
    inline_kb = admin_deposit_inline_kb(req.pk, crypto=True)
    if photo_id:
        from rps.tg_api import send_photo_direct
        send_photo_direct(TELEGRAM_ADMIN_CHAT_ID, photo_id, caption=caption, reply_markup=inline_kb)
    else:
        caption += f"🔢 کد: `{req.proof_data}`"
        from rps.tg_api import send_message_direct
        send_message_direct(TELEGRAM_ADMIN_CHAT_ID, caption, inline_kb)


# ─── Games: description → mode → level ────────────────────────────────────────

def _game_menu(chat_id, user, game_type, mk):
    """Show the game's description, then either the 3-way mode choice
    (search online / play with bot / play with friends) or — for the
    solo Minesweeper game — go straight to level selection."""
    desc = GAME_DESCRIPTIONS.get(game_type, "")

    if game_type == 'ms':
        user.status = 'levelpick_ms_solo'
        user.save(update_fields=['status'])
        return send_message(chat_id, desc + "\n\n🎚 یک سطح را انتخاب کنید 👇", level_kb())

    user.status = f'gamechoice_{game_type}'
    user.save(update_fields=['status'])
    send_message(chat_id, desc + "\n\n🕹 چطور می‌خواهید بازی کنید؟", game_mode_kb())


def _handle_game_mode(chat_id, user, text, mk):
    game_type = user.status.split('_', 1)[1]

    if text == "🔙 بازگشت":
        _reset_user(user)
        return send_message(chat_id, _welcome_msg(user), mk)

    if text == "🔎 جستجوی آنلاین":
        user.status = f'levelpick_{game_type}_online'
        user.save(update_fields=['status'])
        return send_message(chat_id, "🎚 یک سطح را انتخاب کنید 👇", level_kb())

    if text == "🤖 بازی با ربات":
        note = "\n\nℹ️ بازی دوز همیشه روی جدول کلاسیک ۳ در ۳ انجام می‌شود." if game_type == 'ttt' else ""
        user.status = f'levelpick_{game_type}_bot'
        user.save(update_fields=['status'])
        return send_message(chat_id, "🎚 یک سطح را انتخاب کنید 👇" + note, level_kb())

    if text == "👥 بازی با دوستان":
        friends = Friendship.get_friends(user)
        if not friends:
            _reset_user(user)
            return send_message(chat_id, "👥 هنوز دوستی ندارید. ابتدا از منوی «دوستان» یک دوست اضافه کنید.", mk)
        user.status = f'friendpick_{game_type}'
        user.save(update_fields=['status'])
        return send_message(
            chat_id,
            f"👥 دوست خود را برای بازی *{GAME_NAMES[game_type]}* انتخاب کنید.\n"
            f"💵 هزینه ورود هرکدام: *{_fmt(FRIENDLY_ENTRY_FEE)}* — 🏆 جایزه برنده: *{_fmt(FRIENDLY_PRIZE)}*",
            friend_pick_kb(friends),
        )


def _handle_level_pick(chat_id, user, text, mk):
    parts = user.status.split('_', 2)  # ['levelpick', game_type, mode]
    game_type = parts[1]
    mode = parts[2] if len(parts) > 2 else 'online'

    if text == "🔙 بازگشت":
        _reset_user(user)
        return _game_menu(chat_id, user, game_type, mk)

    level = _level_from_text(text)
    if not level:
        return send_message(chat_id, "⚠️ لطفاً یک سطح را از دکمه‌ها انتخاب کنید:", level_kb())

    entry = LEVELS[level]['entry']
    prize = LEVELS[level]['prize']

    if user.balance_cents < entry:
        _reset_user(user)
        return send_message(
            chat_id,
            f"❌ *موجودی ناکافی!*\n\n"
            f"💰 موجودی شما: *{_fmt(user.balance_cents)}*\n"
            f"💵 هزینه ورود این سطح: *{_fmt(entry)}*",
            mk,
        )

    user.balance_cents -= entry
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])

    if game_type == 'ms':
        return _start_ms_game(chat_id, user, level, entry, prize)

    if mode == 'bot':
        return _start_bot_game(chat_id, user, game_type, level, entry, prize)

    # mode == 'online': try to find a waiting opponent at the same level
    waiting = (
        GameMatch.objects
        .filter(game_type=game_type, level=level, mode='online', status='searching')
        .exclude(player1=user)
        .first()
    )
    if waiting:
        _join_match(waiting, user)
    else:
        _create_search(chat_id, user, game_type, level, entry, prize)


def _create_search(chat_id, user, game_type, level, entry_cents, prize_cents):
    from rps.tasks import search_animation_task, expire_search_task
    now = timezone.now()
    match = GameMatch.objects.create(
        game_type=game_type,
        mode='online',
        level=level,
        player1=user,
        entry_fee_cents=entry_cents,
        prize_cents=prize_cents,
        status='searching',
        search_started_at=now,
    )
    user.status = f'searching_{match.pk}'
    user.save(update_fields=['status'])

    game_name = GAME_NAMES.get(game_type, game_type)
    level_label = LEVELS[level]['label']

    result = send_message_direct(
        chat_id,
        f"🔍 *در حال جستجوی حریف...*\n\n"
        f"🎮 بازی: *{game_name}*\n"
        f"🎚 سطح: *{level_label}*\n"
        f"💵 هزینه ورود: *{_fmt(entry_cents)}*\n"
        f"🏆 جایزه برنده: *{_fmt(prize_cents)}*\n\n"
        "_لطفاً صبر کنید..._",
        cancel_search_kb(),
    )
    if result and result.get('ok'):
        match.p1_search_msg_id = result['result']['message_id']
        match.save(update_fields=['p1_search_msg_id'])

    search_animation_task.apply_async(args=[match.pk, 0], countdown=3)
    expire_search_task.apply_async(args=[match.pk], countdown=300)


def _cancel_search(chat_id, user, mk):
    """Cancel search / a pending friendly invite and refund the entry fee."""
    status = user.status
    if not status.startswith('searching_'):
        return send_message(chat_id, "⚠️ جستجویی فعال نیست.", mk)
    match_id = int(status.split('_')[1])
    try:
        match = GameMatch.objects.get(pk=match_id, status='searching')
        if match.entry_fee_cents > 0:
            user.balance_cents += match.entry_fee_cents
        match.status = 'cancelled'
        match.save(update_fields=['status'])
    except GameMatch.DoesNotExist:
        pass
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])
    send_message(chat_id, "✅ جستجو لغو شد. هزینه ورود به کیف پول شما بازگشت.", mk)


def _join_match(match: GameMatch, user: BotUser):
    """
    Pairs up player2 with an existing match (either matched from an online
    search queue, or a friendly invite that was just accepted) and sends
    the opening message for each game type — capturing message_ids so
    every following turn can edit these same messages in place.
    """
    match.player2 = user
    match.status = 'active'
    match.save(update_fields=['player2', 'status'])

    p1 = match.player1
    p1.status = f'playing_{match.game_type}_{match.pk}'
    p1.save(update_fields=['status'])
    user.status = f'playing_{match.game_type}_{match.pk}'
    user.save(update_fields=['status'])

    game_name = GAME_NAMES.get(match.game_type, match.game_type)
    p2_name = _display_name(user)
    p1_name = _display_name(p1)
    fee_line = f"💵 هزینه ورود: *{_fmt(match.entry_fee_cents)}*  🏆 جایزه برنده: *{_fmt(match.prize_cents)}*\n\n"

    header_p1 = f"🎉 *حریف پیدا شد!*\n\n🎮 بازی: *{game_name}*\n👤 حریف: *{p2_name}*\n{fee_line}"
    header_p2 = f"🎉 *حریف پیدا شد!*\n\n🎮 بازی: *{game_name}*\n👤 حریف: *{p1_name}*\n{fee_line}"

    if match.game_type == 'rps':
        move_prompt = "🥊 *دور 1 از 3* — حرکت خود را انتخاب کنید 👇"
        r1 = send_message_direct(p1.chat_id, header_p1 + move_prompt, rps_move_kb())
        r2 = send_message_direct(user.chat_id, header_p2 + move_prompt, rps_move_kb())
        match.p1_msg_id = r1['result']['message_id'] if r1 and r1.get('ok') else None
        match.p2_msg_id = r2['result']['message_id'] if r2 and r2.get('ok') else None
        match.save(update_fields=['p1_msg_id', 'p2_msg_id'])

    elif match.game_type == 'ttt':
        board_str = match.ttt_board
        r1 = send_message_direct(p1.chat_id, header_p1 + "✅ *نوبت شماست* (❌)", ttt_board_kb(board_str, match.pk))
        r2 = send_message_direct(user.chat_id, header_p2 + "⏳ نوبت حریف است (⭕)", ttt_board_kb(board_str, match.pk))
        match.p1_msg_id = r1['result']['message_id'] if r1 and r1.get('ok') else None
        match.p2_msg_id = r2['result']['message_id'] if r2 and r2.get('ok') else None
        match.save(update_fields=['p1_msg_id', 'p2_msg_id'])

    elif match.game_type == 'c4f':
        board_str = match.c4f_board
        r1 = send_message_direct(p1.chat_id, header_p1 + "✅ *نوبت شماست!* شما 🔴 هستید.\nیک ستون را انتخاب کنید:", c4f_board_kb(board_str, match.pk))
        r2 = send_message_direct(user.chat_id, header_p2 + "⏳ نوبت حریف است. شما 🟡 هستید.", c4f_board_kb(board_str, match.pk))
        match.p1_msg_id = r1['result']['message_id'] if r1 and r1.get('ok') else None
        match.p2_msg_id = r2['result']['message_id'] if r2 and r2.get('ok') else None
        match.save(update_fields=['p1_msg_id', 'p2_msg_id'])


def _start_bot_game(chat_id, user, game_type, level, entry_cents, prize_cents):
    """Start a game against the bot (fixed level → entry fee → prize)."""
    match = GameMatch.objects.create(
        game_type=game_type,
        mode='bot',
        level=level,
        player1=user,
        is_offline=True,
        entry_fee_cents=entry_cents,
        prize_cents=prize_cents,
        status='active',
    )
    user.status = f'playing_{game_type}_{match.pk}'
    user.save(update_fields=['status'])

    level_label = LEVELS[level]['label']
    header = (
        f"🤖 *بازی با ربات – {GAME_NAMES[game_type]}*\n\n"
        f"🎚 سطح: *{level_label}*  💵 ورود: *{_fmt(entry_cents)}*  🏆 جایزه: *{_fmt(prize_cents)}*\n\n"
    )

    if game_type == 'rps':
        r = send_message_direct(chat_id, header + "🥊 *دور 1 از 3* — حرکت خود را انتخاب کنید:", rps_move_kb())
    elif game_type == 'ttt':
        r = send_message_direct(chat_id, header + "شما ❌ هستید. نوبت شماست!\nیک خانه را انتخاب کنید:", ttt_board_kb(match.ttt_board, match.pk))
    elif game_type == 'c4f':
        r = send_message_direct(chat_id, header + "شما 🔴 هستید. نوبت شماست!\nیک ستون را انتخاب کنید:", c4f_board_kb(match.c4f_board, match.pk))
    else:
        r = None

    if r and r.get('ok'):
        match.p1_msg_id = r['result']['message_id']
        match.save(update_fields=['p1_msg_id'])


# ─── RPS (best of 3) ───────────────────────────────────────────────────────────

RPS_MOVES = ("🪨 سنگ", "📄 کاغذ", "✂️ قیچی")


def _handle_rps_move(chat_id, user, text, mk):
    status = user.status
    if not status.startswith('playing_rps_'):
        return
    match_id = int(status.split('_')[2])
    try:
        match = GameMatch.objects.select_related('player1', 'player2').get(pk=match_id)
    except GameMatch.DoesNotExist:
        return send_message(chat_id, "⚠️ بازی یافت نشد.", mk)

    if text not in RPS_MOVES:
        return

    if match.player1.chat_id == chat_id:
        if match.p1_move:
            return
        match.p1_move = text
    else:
        if match.p2_move:
            return
        match.p2_move = text
    match.save(update_fields=['p1_move', 'p2_move'])

    if match.is_offline and not match.p2_move:
        match.p2_move = random.choice(RPS_MOVES)
        match.save(update_fields=['p2_move'])

    if match.p1_move and match.p2_move:
        _resolve_rps_round(match, mk)


def _resolve_rps_round(match: GameMatch, mk):
    p1, p2 = match.player1, match.player2
    result = match.rps_round_winner()
    m1, m2 = match.p1_move, match.p2_move

    if result == 'draw':
        # Replay the same round — it does not count toward the 2-win target.
        match.p1_move = None
        match.p2_move = None
        match.save(update_fields=['p1_move', 'p2_move'])
        _edit_rps(match, p1.chat_id, match.p1_msg_id,
                  f"🤝 *دور {match.rps_round} مساوی شد!*\n\nشما: {m1}\nحریف: {m2}\n\n🔁 همین دور را دوباره بازی کنید:")
        if match.p2_msg_id:
            _edit_rps(match, p2.chat_id, match.p2_msg_id,
                      f"🤝 *دور {match.rps_round} مساوی شد!*\n\nشما: {m2}\nحریف: {m1}\n\n🔁 همین دور را دوباره بازی کنید:")
        return

    if result == 'p1':
        match.rps_p1_wins += 1
    else:
        match.rps_p2_wins += 1

    if match.rps_p1_wins == 2 or match.rps_p2_wins == 2:
        match.save(update_fields=['rps_p1_wins', 'rps_p2_wins'])
        return _finish_rps_match(match)

    # Otherwise, move to the next round.
    match.rps_round += 1
    match.p1_move = None
    match.p2_move = None
    match.save(update_fields=['rps_round', 'rps_p1_wins', 'rps_p2_wins', 'p1_move', 'p2_move'])

    score_line = f"📊 نتیجه: شما {match.rps_p1_wins} – حریف {match.rps_p2_wins}"
    _edit_rps(match, p1.chat_id, match.p1_msg_id,
              f"{'🎉' if result=='p1' else '💀'} *دور را {'بردید' if result=='p1' else 'باختید'}!*\n\n"
              f"شما: {m1}\nحریف: {m2}\n\n{score_line}\n\n🥊 *دور {match.rps_round} از 3* — حرکت بعدی خود را انتخاب کنید:")
    if match.p2_msg_id:
        score_line2 = f"📊 نتیجه: شما {match.rps_p2_wins} – حریف {match.rps_p1_wins}"
        _edit_rps(match, p2.chat_id, match.p2_msg_id,
                  f"{'🎉' if result=='p2' else '💀'} *دور را {'بردید' if result=='p2' else 'باختید'}!*\n\n"
                  f"شما: {m2}\nحریف: {m1}\n\n{score_line2}\n\n🥊 *دور {match.rps_round} از 3* — حرکت بعدی خود را انتخاب کنید:")


def _edit_rps(match, chat_id, msg_id, text):
    from rps.tg_api import edit_message
    if msg_id:
        edit_message(chat_id, msg_id, text)
    else:
        send_message(chat_id, text)


def _finish_rps_match(match: GameMatch):
    p1, p2 = match.player1, match.player2
    p1_mk = main_menu(_is_admin(p1.chat_id))
    p2_mk = main_menu(_is_admin(p2.chat_id)) if p2 else None
    p1_won = match.rps_p1_wins == 2

    score_text = f"📊 نتیجه نهایی: {match.rps_p1_wins} – {match.rps_p2_wins}"

    if match.is_offline:
        if p1_won:
            p1.balance_cents += match.prize_cents
            p1.wins += 1
            _edit_rps(match, p1.chat_id, match.p1_msg_id, f"🎉 *بردید!*\n\n{score_text}\n💰 جایزه: *{_fmt(match.prize_cents)}*")
        else:
            p1.losses += 1
            _edit_rps(match, p1.chat_id, match.p1_msg_id, f"💀 *باختید!*\n\n{score_text}")
        p1.total_games += 1
        p1.status = 'idle'
        p1.save(update_fields=['balance_cents', 'wins', 'losses', 'total_games', 'status'])
        send_message(p1.chat_id, "بازی تمام شد. یک گزینه انتخاب کنید 👇", p1_mk)
    else:
        if p1_won:
            p1.balance_cents += match.prize_cents
            p1.wins += 1; p2.losses += 1
            _edit_rps(match, p1.chat_id, match.p1_msg_id, f"🎉 *بردید!*\n\n{score_text}\n💰 جایزه: *{_fmt(match.prize_cents)}*")
            _edit_rps(match, p2.chat_id, match.p2_msg_id, f"💀 *باختید!*\n\n{score_text}")
        else:
            p2.balance_cents += match.prize_cents
            p2.wins += 1; p1.losses += 1
            _edit_rps(match, p2.chat_id, match.p2_msg_id, f"🎉 *بردید!*\n\n{score_text}\n💰 جایزه: *{_fmt(match.prize_cents)}*")
            _edit_rps(match, p1.chat_id, match.p1_msg_id, f"💀 *باختید!*\n\n{score_text}")
        p1.total_games += 1; p2.total_games += 1
        p1.status = 'idle'; p2.status = 'idle'
        p1.save(update_fields=['balance_cents', 'wins', 'losses', 'total_games', 'status'])
        p2.save(update_fields=['balance_cents', 'wins', 'losses', 'total_games', 'status'])
        send_message(p1.chat_id, "بازی تمام شد. یک گزینه انتخاب کنید 👇", p1_mk)
        send_message(p2.chat_id, "بازی تمام شد. یک گزینه انتخاب کنید 👇", p2_mk)

    match.status = 'finished'
    match.finished_at = timezone.now()
    match.save(update_fields=['status', 'finished_at'])


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
    board_visual = _ttt_visual(match.ttt_board)

    if winner == 'draw':
        p1.total_games += 1; p1.status = 'idle'; p1.save(update_fields=['total_games', 'status'])
        _edit_board(p1.chat_id, match.p1_msg_id, f"🤝 *مساوی!*\n\n{board_visual}")
        send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
        if p2 and not match.is_offline:
            p2.total_games += 1; p2.status = 'idle'; p2.save(update_fields=['total_games', 'status'])
            _edit_board(p2.chat_id, match.p2_msg_id, f"🤝 *مساوی!*\n\n{board_visual}")
            send_message(p2.chat_id, "بازی تمام شد.", main_menu(_is_admin(p2.chat_id)))

    elif winner == 'X':  # p1 wins
        if match.is_offline:
            p1.balance_cents += match.prize_cents; p1.wins += 1; p1.total_games += 1
            p1.status = 'idle'; p1.save(update_fields=['balance_cents', 'wins', 'total_games', 'status'])
            _edit_board(p1.chat_id, match.p1_msg_id, f"🎉 *بردید!*\n\n{board_visual}\n\n💰 جایزه: *{_fmt(match.prize_cents)}*")
            send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
        else:
            p1.balance_cents += match.prize_cents; p1.wins += 1; p1.total_games += 1
            p1.status = 'idle'; p1.save(update_fields=['balance_cents', 'wins', 'total_games', 'status'])
            _edit_board(p1.chat_id, match.p1_msg_id, f"🎉 *بردید!*\n\n{board_visual}\n\n🏆 جایزه: *{_fmt(match.prize_cents)}*")
            send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
            if p2:
                p2.losses += 1; p2.total_games += 1
                p2.status = 'idle'; p2.save(update_fields=['losses', 'total_games', 'status'])
                _edit_board(p2.chat_id, match.p2_msg_id, f"💀 *باختید!*\n\n{board_visual}")
                send_message(p2.chat_id, "بازی تمام شد.", main_menu(_is_admin(p2.chat_id)))

    else:  # O wins = p2
        if match.is_offline:
            p1.losses += 1; p1.total_games += 1
            p1.status = 'idle'; p1.save(update_fields=['losses', 'total_games', 'status'])
            _edit_board(p1.chat_id, match.p1_msg_id, f"💀 *باختید!*\n\n{board_visual}")
            send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
        else:
            p2.balance_cents += match.prize_cents; p2.wins += 1; p2.total_games += 1
            p2.status = 'idle'; p2.save(update_fields=['balance_cents', 'wins', 'total_games', 'status'])
            _edit_board(p2.chat_id, match.p2_msg_id, f"🎉 *بردید!*\n\n{board_visual}\n\n🏆 جایزه: *{_fmt(match.prize_cents)}*")
            send_message(p2.chat_id, "بازی تمام شد.", main_menu(_is_admin(p2.chat_id)))
            p1.losses += 1; p1.total_games += 1
            p1.status = 'idle'; p1.save(update_fields=['losses', 'total_games', 'status'])
            _edit_board(p1.chat_id, match.p1_msg_id, f"💀 *باختید!*\n\n{board_visual}")
            send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))

    match.status = 'finished'; match.finished_at = timezone.now(); match.save()


def _edit_board(chat_id, msg_id, text, kb=None):
    from rps.tg_api import edit_message
    if msg_id:
        edit_message(chat_id, msg_id, text, kb)
    else:
        send_message(chat_id, text, kb)


def _send_ttt_board(match: GameMatch):
    """Update both players' existing messages with the current board (no new messages)."""
    p1 = match.player1
    p2 = match.player2
    board = match.ttt_board
    kb = ttt_board_kb(board, match.pk)

    if match.ttt_turn == 1:
        turn_p1 = "✅ *نوبت شماست* (❌)"
        turn_p2 = "⏳ نوبت حریف است"
    else:
        turn_p1 = "⏳ نوبت حریف است"
        turn_p2 = "✅ *نوبت شماست* (⭕)"

    _edit_board(p1.chat_id, match.p1_msg_id, turn_p1, kb)
    if p2:
        _edit_board(p2.chat_id, match.p2_msg_id, turn_p2, kb)


def _ttt_visual(board: str) -> str:
    emojis = {'.': '⬜', 'X': '❌', 'O': '⭕'}
    rows = []
    for r in range(3):
        row = ''.join(emojis[board[r*3+c]] for c in range(3))
        rows.append(row)
    return '\n'.join(rows)


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
    board_visual = _c4f_visual(match.c4f_board)

    if winner == 'draw':
        p1.total_games += 1; p1.status = 'idle'; p1.save(update_fields=['total_games', 'status'])
        _edit_board(p1.chat_id, match.p1_msg_id, f"🤝 *مساوی!*\n\n{board_visual}")
        send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
        if p2 and not match.is_offline:
            p2.total_games += 1; p2.status = 'idle'; p2.save(update_fields=['total_games', 'status'])
            _edit_board(p2.chat_id, match.p2_msg_id, f"🤝 *مساوی!*\n\n{board_visual}")
            send_message(p2.chat_id, "بازی تمام شد.", main_menu(_is_admin(p2.chat_id)))

    elif winner == 'R':  # p1 wins
        p1.balance_cents += match.prize_cents; p1.wins += 1; p1.total_games += 1
        p1.status = 'idle'; p1.save(update_fields=['balance_cents', 'wins', 'total_games', 'status'])
        _edit_board(p1.chat_id, match.p1_msg_id, f"🎉 *بردید!*\n\n{board_visual}\n\n🏆 جایزه: *{_fmt(match.prize_cents)}*")
        send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
        if p2 and not match.is_offline:
            p2.losses += 1; p2.total_games += 1
            p2.status = 'idle'; p2.save(update_fields=['losses', 'total_games', 'status'])
            _edit_board(p2.chat_id, match.p2_msg_id, f"💀 *باختید!*\n\n{board_visual}")
            send_message(p2.chat_id, "بازی تمام شد.", main_menu(_is_admin(p2.chat_id)))
        elif match.is_offline:
            pass

    else:  # 'Y' = p2 wins
        if match.is_offline:
            p1.losses += 1; p1.total_games += 1
            p1.status = 'idle'; p1.save(update_fields=['losses', 'total_games', 'status'])
            _edit_board(p1.chat_id, match.p1_msg_id, f"💀 *باختید!*\n\n{board_visual}")
            send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))
        else:
            p2.balance_cents += match.prize_cents; p2.wins += 1; p2.total_games += 1
            p2.status = 'idle'; p2.save(update_fields=['balance_cents', 'wins', 'total_games', 'status'])
            _edit_board(p2.chat_id, match.p2_msg_id, f"🎉 *بردید!*\n\n{board_visual}\n\n🏆 جایزه: *{_fmt(match.prize_cents)}*")
            send_message(p2.chat_id, "بازی تمام شد.", main_menu(_is_admin(p2.chat_id)))
            p1.losses += 1; p1.total_games += 1
            p1.status = 'idle'; p1.save(update_fields=['losses', 'total_games', 'status'])
            _edit_board(p1.chat_id, match.p1_msg_id, f"💀 *باختید!*\n\n{board_visual}")
            send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))

    match.status = 'finished'; match.finished_at = timezone.now(); match.save()


def _send_c4f_board(match: GameMatch):
    """Update both players' existing messages with the current board (no new messages)."""
    p1 = match.player1
    p2 = match.player2
    board = match.c4f_board
    kb = c4f_board_kb(board, match.pk)

    if match.c4f_turn == 1:
        _edit_board(p1.chat_id, match.p1_msg_id, "✅ *نوبت شماست!* 🔴 یک ستون انتخاب کنید:", kb)
        if p2:
            _edit_board(p2.chat_id, match.p2_msg_id, "⏳ نوبت حریف است... 🟡", kb)
    else:
        if p2:
            _edit_board(p2.chat_id, match.p2_msg_id, "✅ *نوبت شماست!* 🟡 یک ستون انتخاب کنید:", kb)
        _edit_board(p1.chat_id, match.p1_msg_id, "⏳ نوبت حریف است... 🔴", kb)


def _c4f_visual(board: str) -> str:
    """Render the 6×7 board as emoji text for result messages."""
    cell = {'.': '⬜', 'R': '🔴', 'Y': '🟡'}
    col_nums = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    rows = [col_nums]
    for row in range(5, -1, -1):  # top to bottom
        rows.append(''.join(cell[board[row * 7 + col]] for col in range(7)))
    return '\n'.join(rows)


# ─── Minesweeper (solo — callback handled in views.py) ────────────────────────

def _start_ms_game(chat_id, user, level, entry_cents, prize_cents):
    match = GameMatch.objects.create(
        game_type='ms',
        mode='solo',
        level=level,
        player1=user,
        is_offline=True,
        entry_fee_cents=entry_cents,
        prize_cents=prize_cents,
        status='active',
    )
    match.ms_generate(mines=MS_MINES)
    match.save(update_fields=['ms_board', 'ms_revealed', 'ms_mines'])

    user.status = f'playing_ms_{match.pk}'
    user.save(update_fields=['status'])

    level_label = LEVELS[level]['label']
    header = (
        f"💣 *ماین‌یاب – {level_label}*\n\n"
        f"💵 ورود: *{_fmt(entry_cents)}*  🏆 جایزه: *{_fmt(prize_cents)}*\n"
        f"💣 تعداد مین‌ها: *{MS_MINES}*\n\n"
        "یک خانه را باز کنید 👇"
    )
    r = send_message_direct(chat_id, header, ms_board_kb(match.ms_board, match.ms_revealed, match.pk))
    if r and r.get('ok'):
        match.p1_msg_id = r['result']['message_id']
        match.save(update_fields=['p1_msg_id'])


def handle_ms_move(match: GameMatch, player: BotUser, index: int) -> str:
    """
    Called from callback handler in views.py.
    Returns: 'ok' | 'mine' | 'win' | 'already' | 'already_done' | 'invalid'
    """
    if match.status != 'active':
        return 'already_done'
    if match.player1.chat_id != player.chat_id:
        return 'invalid'

    res = match.ms_reveal(index)
    if res == 'already':
        match.save(update_fields=['ms_revealed'])
        return 'already'

    if res == 'mine':
        # Reveal every mine for a satisfying "game over" board.
        revealed = list(match.ms_revealed)
        for i, cell in enumerate(match.ms_board):
            if cell == '*':
                revealed[i] = '1'
        match.ms_revealed = ''.join(revealed)
        match.save(update_fields=['ms_revealed'])
        _finish_ms(match, won=False)
        return 'mine'

    if match.ms_is_won():
        match.save(update_fields=['ms_revealed'])
        _finish_ms(match, won=True)
        return 'win'

    match.save(update_fields=['ms_revealed'])
    _edit_board(
        match.player1.chat_id, match.p1_msg_id,
        "💣 *ماین‌یاب*\n\nخانه بعدی را باز کنید 👇",
        ms_board_kb(match.ms_board, match.ms_revealed, match.pk),
    )
    return 'ok'


def _finish_ms(match: GameMatch, won: bool):
    p1 = match.player1
    kb = ms_board_kb(match.ms_board, match.ms_revealed, match.pk)

    if won:
        p1.balance_cents += match.prize_cents
        p1.wins += 1
        text = f"🎉 *بردید! همه خانه‌های امن باز شد.*\n\n💰 جایزه: *{_fmt(match.prize_cents)}*"
    else:
        p1.losses += 1
        text = "💥 *به مین خوردید!*\n\nهزینه ورود بازگردانده نمی‌شود. دوباره امتحان کنید!"

    p1.total_games += 1
    p1.status = 'idle'
    p1.save(update_fields=['balance_cents', 'wins', 'losses', 'total_games', 'status'])

    _edit_board(p1.chat_id, match.p1_msg_id, text, kb)
    send_message(p1.chat_id, "بازی تمام شد.", main_menu(_is_admin(p1.chat_id)))

    match.status = 'finished'
    match.finished_at = timezone.now()
    match.save(update_fields=['status', 'finished_at'])


# ─── Friends ─────────────────────────────────────────────────────────────────

def _friends_menu(chat_id, user):
    req_count = FriendRequest.objects.filter(receiver=user, status='pending').count()
    badge = f" ({req_count} 🔔)" if req_count else ""
    send_message(
        chat_id,
        f"👥 *دوستان*\n\n"
        f"📨 درخواست‌های دریافتی{badge}\n\n"
        "درخواست دوستی کاملاً *رایگان* است.\n"
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

    # Friend requests are free.
    user.status = 'idle'
    user.save(update_fields=['status'])

    req = FriendRequest.objects.create(sender=user, receiver=target)
    sender_name = user.full_name or user.username or str(user.chat_id)

    send_message(
        target.chat_id,
        f"📨 *درخواست دوستی جدید!*\n\n"
        f"👤 از: *{sender_name}*\n"
        f"🏆 برد: {user.wins} | نرخ: {user.win_rate}",
        friend_req_inline_kb(req.pk),
    )
    send_message(chat_id, f"✅ درخواست دوستی به *{target.full_name or target.username}* ارسال شد!", friends_menu_kb())


def _handle_friend_pick(chat_id, user, text, mk):
    game_type = user.status.split('_', 1)[1]

    if text == "🔙 بازگشت":
        _reset_user(user)
        return _game_menu(chat_id, user, game_type, mk)

    match = re.search(r'\((\-?\d+)\)\s*$', text or "")
    if not match:
        return send_message(chat_id, "⚠️ لطفاً یک دوست را از دکمه‌ها انتخاب کنید:")
    target_chat_id = int(match.group(1))

    try:
        target = BotUser.objects.get(chat_id=target_chat_id)
    except BotUser.DoesNotExist:
        return send_message(chat_id, "❌ این کاربر یافت نشد.")

    if not Friendship.are_friends(user, target):
        return send_message(chat_id, "⚠️ این کاربر در لیست دوستان شما نیست.")

    if user.balance_cents < FRIENDLY_ENTRY_FEE:
        _reset_user(user)
        return send_message(
            chat_id,
            f"❌ موجودی ناکافی!\n💵 هزینه ورود بازی دوستانه: *{_fmt(FRIENDLY_ENTRY_FEE)}*\n"
            f"💰 موجودی شما: *{_fmt(user.balance_cents)}*",
            mk,
        )

    user.balance_cents -= FRIENDLY_ENTRY_FEE
    user.status = 'idle'
    user.save(update_fields=['balance_cents', 'status'])

    match_obj = GameMatch.objects.create(
        game_type=game_type,
        mode='friendly',
        level='friendly',
        player1=user,
        entry_fee_cents=FRIENDLY_ENTRY_FEE,
        prize_cents=FRIENDLY_PRIZE,
        status='searching',
    )

    sender_name = _display_name(user)
    send_message(
        target.chat_id,
        f"🎮 *دعوت به بازی دوستانه!*\n\n"
        f"👤 از: *{sender_name}*\n"
        f"🕹 بازی: *{GAME_NAMES[game_type]}*\n"
        f"💵 هزینه ورود شما: *{_fmt(FRIENDLY_ENTRY_FEE)}*  🏆 جایزه برنده: *{_fmt(FRIENDLY_PRIZE)}*",
        game_invite_inline_kb(match_obj.pk),
    )
    send_message(chat_id, f"✅ دعوت بازی به *{target.full_name or target.username}* ارسال شد!", friends_menu_kb())


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
    level_lines = "\n".join(
        f"• {LEVELS[l]['label']}: ورود {_fmt(LEVELS[l]['entry'])} ← جایزه برنده {_fmt(LEVELS[l]['prize'])}"
        for l in LEVEL_ORDER
    )
    send_message(
        chat_id,
        "❓ *راهنمای ربات*\n\n"
        "🎮 *بازی‌ها:*\n"
        "• *سنگ کاغذ قیچی*: سه دور، اولین کسی که ۲ دور را ببرد برنده است.\n"
        "• *دوز*: روی جدول ۳ در ۳ کلاسیک.\n"
        "• *چهار در یک*: ۴ مهره پشت‌سرهم روی تخته ۶×۷.\n"
        "• *ماین‌یاب*: بازی تک‌نفره؛ خانه‌های امن را بدون برخورد به مین باز کنید.\n\n"
        "برای هر بازی، ابتدا یکی از حالت‌های زیر را انتخاب می‌کنید:\n"
        "🔎 جستجوی آنلاین (بازی با یک حریف تصادفی)\n"
        "🤖 بازی با ربات\n"
        "👥 بازی با دوستان\n\n"
        "🎚 *سطوح بازی آنلاین و بازی با ربات* (هزینه ورود ثابت → جایزه ثابت برنده):\n"
        f"{level_lines}\n\n"
        "👥 *بازی با دوستان:*\n"
        f"هزینه ورود هرکدام: *{_fmt(FRIENDLY_ENTRY_FEE)}* — جایزه برنده: *{_fmt(FRIENDLY_PRIZE)}*\n\n"
        "💰 *کیف پول:*\n"
        "• شارژ از طریق کارت یا کریپتو\n"
        f"• برداشت حداقل *{_fmt(MIN_WITHDRAWAL)}* به کیف پول ترون\n\n"
        "👥 *دوستان:*\n"
        "• ارسال درخواست دوستی کاملاً *رایگان* است.\n\n"
        "🔗 *دعوت:*\n"
        "• دعوت دوست + تکمیل پروفایل = $0.50 برای هر دو\n\n"
        "⏱ *جستجوی آنلاین:*\n"
        "• اگر در ۵ دقیقه حریف پیدا نشد، هزینه ورود بازگردانده می‌شود.",
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
        "برای ارسال پیام همگانی، پیام خود را تایپ کنید (یا «🔙 بازگشت» برای انصراف):",
        mk,
    )
    user.status = 'wait_broadcast'
    user.save(update_fields=['status'])


def _handle_broadcast(chat_id, user, text, mk):
    if text in ("🔙 بازگشت", "/start"):
        _reset_user(user)
        return send_message(chat_id, "❌ ارسال لغو شد.", mk)

    _reset_user(user)

    job = BroadcastJob.objects.create(admin_chat_id=chat_id, text=text)

    result = send_message_direct(
        chat_id,
        "⏳ *در حال ارسال پیام همگانی...*\n\nدر هر لحظه می‌توانید ارسال را لغو کنید.",
        broadcast_cancel_kb(job.pk),
    )
    if result and result.get('ok'):
        job.status_msg_id = result['result']['message_id']
        job.save(update_fields=['status_msg_id'])

    from rps.tasks import broadcast_task
    broadcast_task.delay(job.pk)