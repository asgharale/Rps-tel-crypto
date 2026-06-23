"""
logic.py  –  Main bot logic for Bale RPS bot
Adds: crypto wallet deposit flow + Telegram admin notification with Verify/Unverify buttons
"""

import re
import os
import time
import random
import requests
from django.db.models import Q
from django.utils import timezone
from django.core.files.base import ContentFile
from datetime import timedelta

from rps.models import BotUser, WithdrawalRequest, GameMatch, CardNumber, DepositRequest, CryptoDepositRequest
from rps.bale_api import send_message, download_bale_file
import jdatetime

# ─── Config ──────────────────────────────────────────────────────────────────
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "101632784")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()]

REWARD_AMOUNT   = 30_000
MIN_WITHDRAWAL  = 50_000
MIN_BET         = 10_000
TAX_RATE        = 0.10

# Telegram admin that receives crypto deposit alerts
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
TELEGRAM_TOKEN         = os.getenv("TELEGRAM_TOKEN", "")

# Crypto wallets shown to users  (coin label → wallet address)
CRYPTO_WALLETS = {
    "USDT (TRC20)": "TYourTRC20AddressHere",
    "USDT (ERC20)": "0xYourERC20AddressHere",
    "BTC":          "bc1YourBTCAddressHere",
    "ETH":          "0xYourETHAddressHere",
}

# ─── Keyboards ────────────────────────────────────────────────────────────────

def get_keyboards(chat_id):
    main_keyboard = {
        "keyboard": [
            [{"text": "🎮 شروع بازی (سنگ، کاغذ، قیچی)"}],
            [{"text": "👤 پروفایل من"}, {"text": "💰 موجودی و آمار"}],
            [{"text": "🔗 زیرمجموعه‌گیری"}, {"text": "🏆 برترین‌ها"}],
            [{"text": "🎡 گردونه شانس"}],
            [{"text": "💳 درخواست واریز"}, {"text": "➕ افزایش موجودی"}],
            [{"text": "💎 واریز کریپتو"}],
            [{"text": "❓ راهنما و قوانین"}],
        ],
        "resize_keyboard": True,
    }
    if chat_id in ADMIN_IDS:
        main_keyboard["keyboard"].insert(0, [{"text": "⚙️ مدیریت پنل"}])

    bet_keyboard = {
        "keyboard": [
            [{"text": "💰 10,000 تومان"}, {"text": "💰 25,000 تومان"}],
            [{"text": "💰 50,000 تومان"}],
            [{"text": "🔙 بازگشت به منوی اصلی"}],
        ],
        "resize_keyboard": True,
    }
    game_keyboard = {
        "keyboard": [[{"text": "🪨 سنگ"}, {"text": "📄 کاغذ"}, {"text": "✂️ قیچی"}]],
        "resize_keyboard": True,
    }
    return main_keyboard, bet_keyboard, game_keyboard


def back_kb():
    return {"keyboard": [[{"text": "🔙 بازگشت به منوی اصلی"}]], "resize_keyboard": True}


# ─── Telegram helpers (for admin notifications) ───────────────────────────────

def _tg_post(method: str, **kwargs):
    """Fire-and-forget POST to Telegram API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, timeout=10, **kwargs)
        return r.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return None


def notify_admin_crypto_deposit(deposit: CryptoDepositRequest):
    """
    Send a Telegram message to admin with two inline buttons:
    ✅ Verify   ❌ Unverify
    If proof is a screenshot (Bale file_id), send as photo caption.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        print("Telegram token or admin ID not set – skipping admin notification.")
        return

    user = deposit.user
    username_str = f"@{user.username}" if user.username else f"ID:{user.chat_id}"

    caption = (
        f"💎 *درخواست واریز کریپتو* `#{deposit.pk}`\n\n"
        f"👤 کاربر: {username_str}\n"
        f"🆔 Chat ID: `{user.chat_id}`\n"
        f"🪙 ارز: {deposit.coin}\n"
        f"💵 مبلغ: *{deposit.amount:,} تومان*\n"
        f"📋 نوع مدرک: {'📸 اسکرین‌شات' if deposit.proof_type == 'screenshot' else '🔢 کد پیگیری'}\n"
    )
    if deposit.proof_type == "tracking":
        caption += f"🔢 کد: `{deposit.proof_data}`\n"

    inline_kb = {
        "inline_keyboard": [[
            {"text": "✅ تایید (Verify)",   "callback_data": f"crypto_verify_{deposit.pk}"},
            {"text": "❌ رد (Unverify)", "callback_data": f"crypto_unverify_{deposit.pk}"},
        ]]
    }

    if deposit.proof_type == "screenshot":
        # Download screenshot bytes from Bale, then send to Telegram admin
        image_bytes = download_bale_file(deposit.proof_data)
        if image_bytes:
            _tg_post(
                "sendPhoto",
                data={
                    "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                    "caption": caption,
                    "parse_mode": "Markdown",
                    "reply_markup": __import__("json").dumps(inline_kb),
                },
                files={"photo": ("receipt.jpg", image_bytes, "image/jpeg")},
            )
            return
        # Fall back to text if download failed
        caption += "\n⚠️ دریافت تصویر از بله ناموفق بود."

    _tg_post(
        "sendMessage",
        json={
            "chat_id": TELEGRAM_ADMIN_CHAT_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "reply_markup": inline_kb,
        },
    )


# ─── Main handler ─────────────────────────────────────────────────────────────

def handle_bot_logic(chat_id, text, photo_id=None, current_username=None):
    top_users = BotUser.objects.order_by('-wins')[:5]

    try:
        user, created = BotUser.objects.get_or_create(chat_id=chat_id)
        main_kb, bet_kb, game_kb = get_keyboards(chat_id)

        # ── Referral on first join ────────────────────────────────────────────
        if created:
            if text and text.startswith("/start ") and len(text) > 7:
                try:
                    inviter_id = int(text.split(" ")[1])
                    inviter = BotUser.objects.get(chat_id=inviter_id)
                    if inviter.chat_id != user.chat_id:
                        user.referred_by = inviter
                        user.save()
                        inviter.balance += REWARD_AMOUNT
                        inviter.save()
                        send_message(
                            inviter.chat_id,
                            f"🎊 یک نفر با لینک شما عضو ربات شد!\n🎁 {REWARD_AMOUNT:,} تومان به موجودی شما اضافه شد."
                        )
                except Exception:
                    pass

        # ── Keep username fresh ───────────────────────────────────────────────
        if current_username and user.username != current_username:
            user.username = current_username
            user.save()

        # ── Admin panel ───────────────────────────────────────────────────────
        if chat_id in ADMIN_IDS:
            if text == "⚙️ مدیریت پنل":
                user.status = 'wait_for_broadcast'
                user.save()
                return send_message(
                    chat_id,
                    "📝 پیام خود را بنویسید تا برای همه ارسال شود:\n(برای لغو بنویسید: انصراف)",
                    main_kb,
                )

            elif user.status == 'wait_for_broadcast':
                if text == "انصراف":
                    user.status = 'idle'; user.save()
                    return send_message(chat_id, "❌ ارسال لغو شد.", main_kb)

                all_users = BotUser.objects.all()
                total = all_users.count()
                success = fail = 0
                send_message(chat_id, f"⏳ در حال ارسال به {total} کاربر...")
                for u in all_users:
                    try:
                        send_message(u.chat_id, text)
                        success += 1
                    except Exception as e:
                        fail += 1
                        print(f"Broadcast error {u.chat_id}: {e}")

                user.status = 'idle'; user.save()
                return send_message(
                    chat_id,
                    f"✅ *ارسال همگانی پایان یافت*\n\n👤 کل: {total}\n✔️ موفق: {success}\n❌ ناموفق: {fail}",
                    main_kb,
                )

        # ── Guard: waiting for receipt photo but received text ────────────────
        if user.status == 'wait_for_receipt' and not photo_id:
            send_message(chat_id, "⚠️ لطفاً فقط *عکس* فیش را ارسال کنید.")
            return

        # ── Back / Start ──────────────────────────────────────────────────────
        if text in ("/start", "🔙 بازگشت به منوی اصلی"):
            user.status = 'idle'; user.save()
            return send_message(chat_id, "👋 به ربات بازی خوش آمدید!\nلطفاً یک گزینه را انتخاب کنید:", main_kb)

        # ── Start Game ────────────────────────────────────────────────────────
        elif text == "🎮 شروع بازی (سنگ، کاغذ، قیچی)":
            send_message(chat_id, "🕹 انتخاب مبلغ شرط‌بندی\n────────────────\nلطفاً مبلغ مورد نظر را انتخاب کنید:", bet_kb)

        # ── Bet amount selection ──────────────────────────────────────────────
        elif text and "تومان" in text and "💰" in text:
            nums = re.findall(r'\d+', text.replace(',', ''))
            if nums:
                amount_toman = int(nums[0])
                if user.balance < amount_toman:
                    send_message(chat_id, f"❌ موجودی ناکافی!\n💰 موجودی: {user.balance:,} تومان", main_kb)
                    return

                waiting_match = (
                    GameMatch.objects
                    .filter(amount=amount_toman, status='waiting')
                    .exclude(player1=user)
                    .first()
                )
                if waiting_match:
                    waiting_match.player2 = user
                    waiting_match.status = 'active'
                    waiting_match.save()

                    user.status = 'playing'; user.balance -= amount_toman; user.save()
                    waiting_match.player1.status = 'playing'
                    waiting_match.player1.balance -= amount_toman
                    waiting_match.player1.save()

                    msg = f"🎮 رقیب پیدا شد!\n💰 مبلغ: {amount_toman:,} تومان\n🏁 حرکت خود را انتخاب کنید:"
                    send_message(waiting_match.player1.chat_id, msg, game_kb)
                    send_message(user.chat_id, msg, game_kb)
                else:
                    GameMatch.objects.create(player1=user, amount=amount_toman, status='waiting')
                    user.status = 'waiting'; user.save()
                    send_message(chat_id, "⏳ در حال جستجوی رقیب (۳۰ ثانیه)...", back_kb())

        # ── Game move ─────────────────────────────────────────────────────────
        elif text in ("🪨 سنگ", "📄 کاغذ", "✂️ قیچی"):
            match = (
                GameMatch.objects
                .filter(status='active')
                .filter(Q(player1=user) | Q(player2=user))
                .first()
            )
            if not match:
                send_message(chat_id, "⚠️ بازی فعالی یافت نشد.", main_kb)
                return

            if match.player1 == user:
                match.p1_move = text
            else:
                match.p2_move = text
            match.save()

            if match.is_with_bot or (match.p1_move and match.p2_move):
                process_match_result(match)
            else:
                send_message(chat_id, "✅ حرکت ثبت شد. منتظر رقیب...")

        # ── Profile ───────────────────────────────────────────────────────────
        elif text == "👤 پروفایل من":
            sub_count = BotUser.objects.filter(referred_by=user).count()
            shamsi = jdatetime.datetime.fromgregorian(datetime=user.created_at)
            formatted = shamsi.strftime("%Y/%m/%d - %H:%M")
            uname = user.username if user.username else "تنظیم نشده"
            profile_text = (
                "👤 *اطلاعات پروفایل شما*\n\n"
                f"🆔 یوزرنیم: {uname}\n"
                f"🔢 شماره کاربری: `{user.chat_id}`\n"
                "────────────────\n"
                f"👥 تعداد زیرمجموعه: {sub_count} نفر\n"
                f"💰 موجودی حساب: {user.balance:,} تومان\n"
                f"📅 تاریخ عضویت: {formatted}"
            )
            send_message(chat_id, profile_text, main_kb)

        elif text == "🔗 زیرمجموعه‌گیری":
            show_referral_menu(chat_id, user)

        elif text == "💰 موجودی و آمار":
            msg = (
                f"🏦 *وضعیت حساب*\n────────────────\n"
                f"💰 موجودی: {user.balance:,} تومان\n"
                f"✅ برد: {user.wins}\n❌ باخت: {user.losses}"
            )
            send_message(chat_id, msg, main_kb)

        elif text == "🏆 برترین‌ها":
            board = "🏆 *برترین‌های بازی*\n────────────────\n"
            for i, tu in enumerate(top_users, 1):
                board += f"{i}. کاربر `{str(tu.username or tu.chat_id)[:8]}` | برد: *{tu.wins}*\n"
            send_message(chat_id, board, main_kb)

        # ── Card deposit (existing flow) ──────────────────────────────────────
        elif text == "➕ افزایش موجودی":
            cards = CardNumber.objects.filter(is_active=True)
            if not cards.exists():
                send_message(chat_id, "⚠️ حسابی برای واریز در دسترس نیست.", main_kb)
                return
            card = random.choice(list(cards))
            user.status = 'wait_for_receipt'; user.save()
            msg = (
                "💳 *اطلاعات کارت*\n"
                f"👤 نام: {card.owner_name}\n"
                f"🔢 شماره: `{card.number}`\n"
                "────────────────\n"
                "📸 لطفاً *عکس فیش* واریزی خود را ارسال کنید:\n"
                "در مرحله بعد از شما مبلغ انتقال داده شده پرسیده می‌شود"
            )
            send_message(chat_id, msg, back_kb())

        # ── Receive receipt photo ─────────────────────────────────────────────
        elif photo_id and user.status == 'wait_for_receipt':
            send_message(chat_id, "⏳ در حال دریافت تصویر از سرور بله... لطفاً صبور باشید.")
            image_data = download_bale_file(photo_id)
            if image_data:
                req_obj = DepositRequest(user=user, amount=0)
                req_obj.receipt_image.save(
                    f"{chat_id}_{int(time.time())}.jpg", ContentFile(image_data)
                )
                req_obj.save()
                user.status = f'set_amount_{req_obj.id}'; user.save()
                send_message(chat_id, "✅ تصویر دریافت شد.\n💰 حالا مبلغ واریزی را به *تومان* وارد کنید:")
            else:
                send_message(
                    chat_id,
                    "❌ سرور فایل بله موقتاً در دسترس نیست.\nلطفاً چند لحظه دیگر دوباره عکس را ارسال کنید."
                )

        # ── Receive amount for card deposit ───────────────────────────────────
        elif user.status.startswith('set_amount_') and text and text.isdigit():
            req_id = user.status.replace('set_amount_', '')
            amount_val = int(text)
            try:
                req_obj = DepositRequest.objects.get(pk=req_id, user=user)
                req_obj.amount = amount_val
                req_obj.save()
                user.status = 'idle'; user.save()
                send_message(
                    chat_id,
                    "🚀 رسید شما با موفقیت ثبت شد.\nپس از تایید ادمین، حساب شما شارژ می‌شود.",
                    main_kb,
                )
            except DepositRequest.DoesNotExist:
                send_message(chat_id, "❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.", main_kb)
                user.status = 'idle'; user.save()

        # ── Withdrawal request ────────────────────────────────────────────────
        elif text == "💳 درخواست واریز":
            if user.balance < MIN_WITHDRAWAL:
                send_message(chat_id, f"⚠️ حداقل برداشت: {MIN_WITHDRAWAL:,} تومان", main_kb)
            else:
                sample = f"واریز\nمبلغ: {user.balance}\nکارت: 6037000000000000"
                send_message(chat_id, f"💳 لطفاً طبق فرمت زیر بفرستید:\n\n`{sample}`", main_kb)

        elif text and text.startswith("واریز"):
            lines = text.split('\n')
            if len(lines) >= 3:
                try:
                    amount = int(re.findall(r'\d+', lines[1])[0])
                    card = re.findall(r'\d+', lines[2])[0]
                    if len(card) == 16 and amount <= user.balance:
                        WithdrawalRequest.objects.create(user=user, amount=amount, card_number=card)
                        user.balance -= amount; user.save()
                        send_message(chat_id, "✅ درخواست برداشت ثبت شد.", main_kb)
                    else:
                        send_message(chat_id, "❌ موجودی ناکافی یا شماره کارت غلط است.")
                except Exception:
                    send_message(chat_id, "❌ فرمت ارسال اشتباه است.")

        # ── Lucky wheel ───────────────────────────────────────────────────────
        elif text == "🎡 گردونه شانس":
            success, message = spin_the_wheel(user)
            send_message(chat_id, message, main_kb)

        # ── Help ──────────────────────────────────────────────────────────────
        elif text == "❓ راهنما و قوانین":
            msg = (
                "🎮 *راهنمای بازی*\n\n"
                "۱. شروع بازی → سیستم حریف واقعی پیدا می‌کند.\n"
                "۲. اگر ۳۰ ثانیه کسی نباشد، با بات بازی می‌کنید.\n"
                "۳. برد ۱۰٪ کارمزد دارد.\n\n"
                "💰 *افزایش موجودی*\n"
                "کارت: رسید عکس ارسال کنید.\n"
                "کریپتو: از «💎 واریز کریپتو» استفاده کنید.\n\n"
                "🔗 *زیرمجموعه‌گیری*\n"
                "برای هر دعوت موفق پاداش نقدی دریافت کنید.\n\n"
                "⚖️ *قوانین*\n"
                "تعدد اکانت ممنوع است و منجر به مسدودی می‌شود."
            )
            send_message(chat_id, msg, main_kb)

        # ══════════════════════════════════════════════════════════════════════
        # ─── CRYPTO DEPOSIT FLOW ─────────────────────────────────────────────
        # ══════════════════════════════════════════════════════════════════════

        elif text == "💎 واریز کریپتو":
            _handle_crypto_start(chat_id, user)

        elif user.status == 'crypto_select_coin' and text in CRYPTO_WALLETS:
            _handle_crypto_coin_selected(chat_id, user, text)

        elif user.status == 'crypto_select_proof':
            if text == "📸 ارسال اسکرین‌شات":
                user.status = 'crypto_wait_screenshot'; user.save()
                send_message(chat_id, "📸 لطفاً تصویر اسکرین‌شات پرداخت خود را ارسال کنید:", back_kb())
            elif text == "🔢 ارسال کد پیگیری / TxHash":
                user.status = 'crypto_wait_tracking'; user.save()
                send_message(chat_id, "🔢 لطفاً کد پیگیری یا TxHash تراکنش را وارد کنید:", back_kb())

        elif user.status == 'crypto_wait_screenshot' and photo_id:
            # Store file_id temporarily in status
            user.status = f'crypto_got_screenshot_{photo_id}'; user.save()
            send_message(chat_id, "✅ تصویر دریافت شد.\n💰 مبلغ واریزی را به *تومان* وارد کنید:")

        elif user.status == 'crypto_wait_screenshot' and not photo_id:
            send_message(chat_id, "⚠️ لطفاً فقط *عکس* ارسال کنید.")

        elif user.status == 'crypto_wait_tracking' and text and not text.startswith("/"):
            # Save tracking code temporarily
            user.status = f'crypto_got_tracking_{text}'; user.save()
            send_message(chat_id, "✅ کد پیگیری دریافت شد.\n💰 مبلغ واریزی را به *تومان* وارد کنید:")

        elif user.status.startswith('crypto_got_') and text and text.isdigit():
            _handle_crypto_amount(chat_id, user, int(text), main_kb)

    except Exception as e:
        print(f"Logic Error: {e}")


# ─── Crypto deposit sub-handlers ──────────────────────────────────────────────

def _handle_crypto_start(chat_id, user):
    coin_kb = {
        "keyboard": [[{"text": c}] for c in CRYPTO_WALLETS] + [[{"text": "🔙 بازگشت به منوی اصلی"}]],
        "resize_keyboard": True,
    }
    user.status = 'crypto_select_coin'; user.save()
    send_message(chat_id, "💎 *واریز کریپتو*\n\nلطفاً ارز دیجیتال مورد نظر را انتخاب کنید:", coin_kb)


def _handle_crypto_coin_selected(chat_id, user, coin_name):
    address = CRYPTO_WALLETS[coin_name]
    # Persist chosen coin in a temp field: status encodes it
    user.status = f'crypto_select_proof'
    user.save()
    # Store coin name in a transient way via status detail
    # We re-use status as: crypto_select_proof:{coin_name} (URL-safe)
    safe_coin = coin_name.replace(" ", "_").replace("(", "").replace(")", "")
    user.status = f'crypto_select_proof:{safe_coin}'
    user.save()

    proof_kb = {
        "keyboard": [
            [{"text": "📸 ارسال اسکرین‌شات"}, {"text": "🔢 ارسال کد پیگیری / TxHash"}],
            [{"text": "🔙 بازگشت به منوی اصلی"}],
        ],
        "resize_keyboard": True,
    }
    msg = (
        f"💳 *{coin_name}*\n\n"
        f"آدرس کیف پول:\n`{address}`\n\n"
        "────────────────\n"
        "پس از واریز، مدرک پرداخت خود را ارسال کنید:"
    )
    send_message(chat_id, msg, proof_kb)


def _handle_crypto_amount(chat_id, user, amount: int, main_kb):
    status = user.status  # e.g. crypto_got_screenshot_<file_id>  or  crypto_got_tracking_<code>

    # Recover coin name from earlier status — it was stored before proof step
    # We need to look it up; simplest: store it in user.status chain
    # Since status chain: crypto_select_proof:{coin} → crypto_wait_* → crypto_got_*:{data}
    # Coin was lost after proof step. Use a small helper: re-read from last CryptoDepositRequest or default.
    coin = _recover_coin(user)

    if status.startswith('crypto_got_screenshot_'):
        file_id = status.replace('crypto_got_screenshot_', '')
        proof_type = 'screenshot'
        proof_data = file_id
    else:  # crypto_got_tracking_
        tracking_code = status.replace('crypto_got_tracking_', '')
        proof_type = 'tracking'
        proof_data = tracking_code

    deposit = CryptoDepositRequest.objects.create(
        user=user,
        coin=coin,
        amount=amount,
        proof_type=proof_type,
        proof_data=proof_data,
    )

    user.status = 'idle'; user.save()

    send_message(
        chat_id,
        f"✅ *درخواست واریز کریپتو ثبت شد!*\n\n"
        f"🪙 ارز: {coin}\n"
        f"💵 مبلغ: *{amount:,} تومان*\n"
        f"🔖 شماره پیگیری: `#{deposit.pk}`\n\n"
        "پس از تایید ادمین، موجودی شما شارژ می‌شود.",
        main_kb,
    )

    # Notify Telegram admin
    notify_admin_crypto_deposit(deposit)


def _recover_coin(user) -> str:
    """Try to find the coin the user selected; fall back to 'نامشخص'."""
    last = CryptoDepositRequest.objects.filter(user=user).order_by('-created_at').first()
    if last:
        return last.coin
    return "نامشخص"


# ─── handle_bot_logic needs to also catch the crypto_select_proof state ───────
# Patch: override the status check for proof selection (coin is encoded in status)
# We monkey-patch by overriding in main handler — see the elif branches above.
# The state 'crypto_select_proof:{coin}' needs special handling:

_ORIGINAL_HANDLE = handle_bot_logic


def handle_bot_logic(chat_id, text, photo_id=None, current_username=None):
    """Wrapper that normalises the crypto_select_proof:{coin} status before dispatch."""
    try:
        user = BotUser.objects.get(chat_id=chat_id)
        if user.status.startswith('crypto_select_proof:'):
            # Extract coin, map button text to next state
            coin_safe = user.status.split(':', 1)[1]
            coin_name = coin_safe.replace('_', ' ')
            # Reconstruct proper coin name
            for c in CRYPTO_WALLETS:
                safe = c.replace(" ", "_").replace("(", "").replace(")", "")
                if safe == coin_safe:
                    coin_name = c
                    break

            if text == "📸 ارسال اسکرین‌شات":
                user.status = f'crypto_wait_screenshot:{coin_safe}'; user.save()
                main_kb, _, _ = get_keyboards(chat_id)
                return send_message(chat_id, "📸 لطفاً تصویر اسکرین‌شات پرداخت را ارسال کنید:", back_kb())
            elif text == "🔢 ارسال کد پیگیری / TxHash":
                user.status = f'crypto_wait_tracking:{coin_safe}'; user.save()
                return send_message(chat_id, "🔢 لطفاً کد پیگیری یا TxHash را وارد کنید:", back_kb())
            elif text == "🔙 بازگشت به منوی اصلی":
                user.status = 'idle'; user.save()

        elif user.status.startswith('crypto_wait_screenshot:') and photo_id:
            coin_safe = user.status.split(':', 1)[1]
            user.status = f'crypto_got_screenshot_{photo_id}:{coin_safe}'; user.save()
            return send_message(chat_id, "✅ تصویر دریافت شد.\n💰 مبلغ واریزی را به *تومان* وارد کنید:")

        elif user.status.startswith('crypto_wait_tracking:') and text and not text.startswith("/"):
            coin_safe = user.status.split(':', 1)[1]
            user.status = f'crypto_got_tracking_{text}:{coin_safe}'; user.save()
            return send_message(chat_id, "✅ کد پیگیری ثبت شد.\n💰 مبلغ واریزی را به *تومان* وارد کنید:")

        elif (
            user.status.startswith('crypto_got_screenshot_') or
            user.status.startswith('crypto_got_tracking_')
        ) and text and text.isdigit():
            main_kb, _, _ = get_keyboards(chat_id)
            return _handle_crypto_amount_v2(chat_id, user, int(text), main_kb)

    except BotUser.DoesNotExist:
        pass  # new user, will be created inside original handler

    return _ORIGINAL_HANDLE(chat_id, text, photo_id, current_username)


def _handle_crypto_amount_v2(chat_id, user, amount: int, main_kb):
    """Handles amount step when coin is encoded in the status string."""
    status = user.status  # crypto_got_screenshot_<file_id>:<coin_safe>
                          # or crypto_got_tracking_<code>:<coin_safe>

    # Split off coin
    if ':' in status:
        data_part, coin_safe = status.rsplit(':', 1)
        # Resolve coin name
        coin_name = coin_safe.replace('_', ' ')
        for c in CRYPTO_WALLETS:
            safe = c.replace(" ", "_").replace("(", "").replace(")", "")
            if safe == coin_safe:
                coin_name = c
                break
    else:
        data_part = status
        coin_name = "نامشخص"

    if data_part.startswith('crypto_got_screenshot_'):
        file_id = data_part.replace('crypto_got_screenshot_', '')
        proof_type = 'screenshot'
        proof_data = file_id
    else:
        tracking_code = data_part.replace('crypto_got_tracking_', '')
        proof_type = 'tracking'
        proof_data = tracking_code

    deposit = CryptoDepositRequest.objects.create(
        user=user,
        coin=coin_name,
        amount=amount,
        proof_type=proof_type,
        proof_data=proof_data,
    )

    user.status = 'idle'; user.save()

    send_message(
        chat_id,
        f"✅ *درخواست واریز کریپتو ثبت شد!*\n\n"
        f"🪙 ارز: {coin_name}\n"
        f"💵 مبلغ: *{amount:,} تومان*\n"
        f"🔖 شماره پیگیری: `#{deposit.pk}`\n\n"
        "پس از تایید ادمین، موجودی شما شارژ می‌شود.",
        main_kb,
    )
    notify_admin_crypto_deposit(deposit)


# ─── Game result ──────────────────────────────────────────────────────────────

def process_match_result(match):
    try:
        p1, p2 = match.player1, match.player2
        m1, m2 = match.p1_move, match.p2_move

        main_kb_p1, _, _ = get_keyboards(p1.chat_id)
        main_kb_p2 = None
        if p2:
            main_kb_p2, _, _ = get_keyboards(p2.chat_id)

        winner = None
        result_type = "draw"
        if m1 == m2:
            result_type = "draw"
        elif (
            (m1 == "🪨 سنگ" and m2 == "✂️ قیچی") or
            (m1 == "📄 کاغذ" and m2 == "🪨 سنگ") or
            (m1 == "✂️ قیچی" and m2 == "📄 کاغذ")
        ):
            winner, result_type = p1, "p1"
        else:
            winner, result_type = p2, "p2"

        tax = int(match.amount * TAX_RATE)
        net_prize = match.amount - tax
        total_prize = match.amount + net_prize

        if result_type == "draw":
            p1.balance += match.amount
            send_message(p1.chat_id, f"🤝 مساوی!\n🤖 رقیب: {m2}\n💰 مبلغ برگشت خورد.", main_kb_p1)
            if p2 and not match.is_with_bot:
                p2.balance += match.amount
                send_message(p2.chat_id, f"🤝 مساوی!\n🤖 رقیب: {m1}\n💰 مبلغ برگشت خورد.", main_kb_p2)

        elif result_type == "p1":
            p1.balance += total_prize; p1.wins += 1
            send_message(p1.chat_id, f"🎉 بردید!\n🤖 رقیب: {m2}\n💰 سود: {net_prize:,} تومان", main_kb_p1)
            if p2 and not match.is_with_bot:
                p2.losses += 1
                send_message(p2.chat_id, f"💀 باختید!\n🤖 رقیب: {m1}", main_kb_p2)

        else:
            p1.losses += 1
            send_message(p1.chat_id, f"💀 باختید!\n🤖 رقیب: {m2}", main_kb_p1)
            if p2 and not match.is_with_bot:
                p2.balance += total_prize; p2.wins += 1
                send_message(p2.chat_id, f"🎉 بردید!\n🤖 رقیب: {m1}\n💰 سود: {net_prize:,} تومان", main_kb_p2)

        p1.status = 'idle'; p1.save()
        if p2: p2.status = 'idle'; p2.save()
        match.status = 'finished'; match.save()

    except Exception as e:
        print(f"Result Error: {e}")


# ─── Lucky wheel ──────────────────────────────────────────────────────────────

def spin_the_wheel(user):
    now = timezone.now()
    if user.last_wheel_spin:
        elapsed = now - user.last_wheel_spin
        if elapsed < timedelta(hours=24):
            remaining = timedelta(hours=24) - elapsed
            h = remaining.seconds // 3600
            m = (remaining.seconds // 60) % 60
            return False, f"⚠️ شما در ۲۴ ساعت گذشته چرخش داشتید.\n⏳ زمان باقی‌مانده: {h} ساعت و {m} دقیقه"

    prizes  = [0, 5_000, 10_000, 20_000, 50_000]
    weights = [50, 25, 15, 7, 3]
    result = random.choices(prizes, weights=weights, k=1)[0]

    user.last_wheel_spin = now
    if result > 0:
        user.balance += result
        user.save()
        return True, f"🎊 تبریک! برنده *{result:,} تومان* شدید!\n💰 موجودی جدید: {user.balance:,} تومان"
    else:
        user.save()
        return True, "😔 این بار پوچ بود! فردا دوباره امتحان کن."


# ─── Referral menu ────────────────────────────────────────────────────────────

def show_referral_menu(chat_id, user):
    bot_username = "rps_1v1_bot"
    link = f"https://ble.ir/{bot_username}?start={user.chat_id}"
    sub_count = BotUser.objects.filter(referred_by=user).count()
    msg = (
        "👥 *لینک کسب درآمد از دعوت*\n\n"
        "با دعوت دوستان خود به ربات، برای هر عضویت هدیه نقدی دریافت کنید!\n\n"
        f"📊 آمار شما: {sub_count} زیرمجموعه\n"
        f"🎁 پاداش هر دعوت: *{REWARD_AMOUNT:,}* تومان\n"
        "────────────────\n"
        f"🔗 *لینک دعوت اختصاصی شما:*\n`{link}`\n\n"
        "متن بالا را کپی کرده و برای دوستان بفرستید."
    )
    send_message(chat_id, msg, back_kb())