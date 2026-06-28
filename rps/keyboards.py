"""
keyboards.py  –  All reply/inline keyboard factories.
"""

import os

BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot")

# ─── Reply keyboards ──────────────────────────────────────────────────────────

def main_menu(is_admin=False):
    rows = [
        [{"text": "🎯 دوز (Tic-Tac-Toe)"}, {"text": "✊ سنگ کاغذ قیچی"}],
        [{"text": "⚔️ بازی جنگ (به زودی)"}],
        [{"text": "👤 پروفایل من"}, {"text": "👥 دوستان"}],
        [{"text": "💰 کیف پول"}],
        [{"text": "🏆 رتبه‌بندی"}, {"text": "❓ راهنما"}],
    ]
    if is_admin:
        rows.insert(0, [{"text": "⚙️ پنل مدیریت"}])
    return {"keyboard": rows, "resize_keyboard": True}


def back_kb():
    return {
        "keyboard": [[{"text": "🔙 بازگشت"}]],
        "resize_keyboard": True,
    }


def cancel_search_kb():
    return {
        "keyboard": [
            [{"text": "🤖 بازی آفلاین (با ربات)"}],
            [{"text": "❌ انصراف از جستجو"}],
        ],
        "resize_keyboard": True,
    }


# RPS game keyboards

def rps_bet_kb(game='rps'):
    if game == 'rps':
        bets = ["0.30$", "0.50$", "1.00$", "2.00$", "5.00$"]
    else:
        bets = ["0.50$", "0.70$", "1.00$", "1.50$", "2.00$"]
    rows = [[{"text": f"💰 {b}"}] for b in bets]
    rows.append([{"text": "🔙 بازگشت"}])
    return {"keyboard": rows, "resize_keyboard": True}


def rps_move_kb():
    return {
        "keyboard": [[
            {"text": "🪨 سنگ"},
            {"text": "📄 کاغذ"},
            {"text": "✂️ قیچی"},
        ]],
        "resize_keyboard": True,
    }


# Tic-Tac-Toe inline keyboard from board state

def ttt_board_kb(board: str, match_id: int):
    """
    Board: 9-char string ('.' / 'X' / 'O').
    Returns an inline_keyboard with 3×3 grid.
    """
    emojis = {'.': '⬜', 'X': '❌', 'O': '⭕'}
    rows = []
    for row in range(3):
        r = []
        for col in range(3):
            pos = row * 3 + col
            cell = board[pos]
            r.append({
                "text": emojis[cell],
                "callback_data": f"ttt_{match_id}_{pos}",
            })
        rows.append(r)
    return {"inline_keyboard": rows}


# Profile keyboards

def profile_menu_kb():
    return {
        "keyboard": [
            [{"text": "✏️ ویرایش نام"}, {"text": "✏️ ویرایش سن"}],
            [{"text": "📱 ویرایش شماره"}, {"text": "💳 ویرایش کیف پول ترون"}],
            [{"text": "🖼 تغییر آواتار"}, {"text": "🔗 لینک دعوت"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


# Wallet keyboards

def wallet_menu_kb():
    return {
        "keyboard": [
            [{"text": "➕ شارژ کیف پول"}, {"text": "💸 درخواست برداشت"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


def deposit_amount_kb():
    amounts = ["1$", "5$", "10$", "20$"]
    rows = [[{"text": f"💵 {a}"}] for a in amounts]
    rows.append([{"text": "🔙 بازگشت"}])
    return {"keyboard": rows, "resize_keyboard": True}


def deposit_method_kb():
    return {
        "keyboard": [
            [{"text": "💳 پرداخت کارتی (رسید)"}],
            [{"text": "🪙 واریز کریپتو"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


def crypto_proof_kb():
    return {
        "keyboard": [
            [{"text": "📸 ارسال اسکرین‌شات"}, {"text": "🔢 کد پیگیری / TxHash"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


# Friends keyboards

def friends_menu_kb():
    return {
        "keyboard": [
            [{"text": "👥 لیست دوستان"}, {"text": "📨 درخواست‌های دریافتی"}],
            [{"text": "🔍 افزودن دوست"}],
            [{"text": "🎮 دعوت به بازی"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


# Admin keyboards

def admin_report_inline_kb(report_id: int):
    return {
        "inline_keyboard": [[
            {"text": "🙈 نادیده گرفتن", "callback_data": f"report_ignore_{report_id}"},
            {"text": "🚫 مسدود کردن",   "callback_data": f"report_ban_{report_id}"},
        ]]
    }


def admin_withdrawal_inline_kb(req_id: int):
    return {
        "inline_keyboard": [[
            {"text": "✅ پرداخت شد",  "callback_data": f"withdraw_paid_{req_id}"},
            {"text": "❌ رد کردن",     "callback_data": f"withdraw_reject_{req_id}"},
        ]]
    }


def admin_deposit_inline_kb(req_id: int, crypto=False):
    prefix = "crypto" if crypto else "deposit"
    return {
        "inline_keyboard": [[
            {"text": "✅ تایید",  "callback_data": f"{prefix}_verify_{req_id}"},
            {"text": "❌ رد",     "callback_data": f"{prefix}_reject_{req_id}"},
        ]]
    }


def game_invite_inline_kb(match_id: int):
    return {
        "inline_keyboard": [[
            {"text": "✅ قبول",   "callback_data": f"gameinv_accept_{match_id}"},
            {"text": "❌ رد",     "callback_data": f"gameinv_reject_{match_id}"},
        ]]
    }


def friend_req_inline_kb(req_id: int):
    return {
        "inline_keyboard": [[
            {"text": "✅ قبول",   "callback_data": f"freq_accept_{req_id}"},
            {"text": "❌ رد",     "callback_data": f"freq_reject_{req_id}"},
        ]]
    }