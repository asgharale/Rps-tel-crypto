"""
keyboards.py  –  All reply/inline keyboard factories.
"""

import os

BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot")

# ─── Reply keyboards ──────────────────────────────────────────────────────────

def main_menu(is_admin=False):
    rows = [
        [{"text": "🎯 دوز (Tic-Tac-Toe)"}, {"text": "✊ سنگ کاغذ قیچی"}],
        [{"text": "🔴 چهار در یک (Connect Four)"}, {"text": "💣 ماین‌یاب"}],
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
            [{"text": "❌ انصراف از جستجو"}],
        ],
        "resize_keyboard": True,
    }


# ─── Game mode selection (search online / play with bot / play with friends) ──

def game_mode_kb():
    """Shown right after a game's description. No search/bot mode makes sense
    for a solo game (Minesweeper) — that one skips straight to level_kb()."""
    return {
        "keyboard": [
            [{"text": "🔎 جستجوی آنلاین"}],
            [{"text": "🤖 بازی با ربات"}],
            [{"text": "👥 بازی با دوستان"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


# ─── Level selection (fixed entry fee → fixed prize, no open wagering) ────────

LEVEL_BUTTON_LABELS = {
    'intermediate': "🟢 متوسط  |  ورودی $0.30 → جایزه $0.50",
    'master':       "🟠 حرفه‌ای  |  ورودی $0.60 → جایزه $1.00",
    'gods':         "🔴 خدایان  |  ورودی $1.00 → جایزه $1.80",
}


def level_kb():
    rows = [[{"text": LEVEL_BUTTON_LABELS[lvl]}] for lvl in ('intermediate', 'master', 'gods')]
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


def c4f_board_kb(board: str, match_id: int):
    """
    board: 42-char string, row 0 = bottom, row 5 = top.
    Renders 6 rows top→bottom with a drop-arrow row on top.
    Tapping a column arrow drops into that column.
    """
    cell = {'.': '⬜', 'R': '🔴', 'Y': '🟡'}
    rows = []

    # Arrow row: one button per column to drop a piece
    arrow_row = []
    for col in range(7):
        # Check if column is full (top row = row 5)
        top_idx = 5 * 7 + col
        if board[top_idx] != '.':
            arrow_row.append({"text": "🚫", "callback_data": f"c4f_{match_id}_full"})
        else:
            arrow_row.append({"text": f"⬇️", "callback_data": f"c4f_{match_id}_{col}"})
    rows.append(arrow_row)

    # Board rows top→bottom (row 5 first visually)
    for row in range(5, -1, -1):
        r = []
        for col in range(7):
            idx = row * 7 + col
            r.append({
                "text": cell[board[idx]],
                "callback_data": f"c4f_{match_id}_{col}",  # clicking cell = drop in that col
            })
        rows.append(r)

    return {"inline_keyboard": rows}


# Tic-Tac-Toe inline keyboard from board state

def ttt_board_kb(board: str, match_id: int):
    """
    Board: 9-char string ('.' / 'X' / 'O'). Always the classic 3×3 grid.
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


# ─── Minesweeper inline keyboard (5×6 solo board) ──────────────────────────────

def ms_board_kb(board: str, revealed: str, match_id: int, rows=5, cols=6):
    """
    board:    30-char '*' (mine) / '0'-'8' (safe, neighbour-mine count)
    revealed: 30-char '0' hidden / '1' revealed
    """
    number_emoji = {
        '0': '▪️', '1': '1️⃣', '2': '2️⃣', '3': '3️⃣', '4': '4️⃣',
        '5': '5️⃣', '6': '6️⃣', '7': '7️⃣', '8': '8️⃣',
    }
    kb_rows = []
    for r in range(rows):
        row_buttons = []
        for c in range(cols):
            idx = r * cols + c
            if revealed[idx] == '1':
                cell = board[idx]
                text = "💥" if cell == '*' else number_emoji.get(cell, '▪️')
            else:
                text = "⬜"
            row_buttons.append({"text": text, "callback_data": f"ms_{match_id}_{idx}"})
        kb_rows.append(row_buttons)
    return {"inline_keyboard": kb_rows}


# ─── Profile keyboards ─────────────────────────────────────────────────────────

def profile_menu_kb():
    return {
        "keyboard": [
            [{"text": "✏️ ویرایش نام"}, {"text": "✏️ ویرایش سن"}],
            [{"text": "📱 ویرایش شماره"}, {"text": "🏙 ویرایش استان"}],
            [{"text": "💳 ویرایش کیف پول ترون"}, {"text": "🖼 تغییر آواتار"}],
            [{"text": "🔗 لینک دعوت"}],
            [{"text": "🔙 بازگشت"}],
        ],
        "resize_keyboard": True,
    }


def province_kb(provinces):
    """provinces: iterable of Province objects with a usable .name"""
    rows = []
    row = []
    for p in provinces:
        name = p.name or f"استان #{p.pk}"
        row.append({"text": name})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not rows:
        rows.append([{"text": "— هنوز استانی ثبت نشده —"}])
    rows.append([{"text": "🔙 بازگشت"}])
    return {"keyboard": rows, "resize_keyboard": True}


# ─── Wallet keyboards ───────────────────────────────────────────────────────────

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


# ─── Friends keyboards ──────────────────────────────────────────────────────────

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


def friend_pick_kb(friends):
    rows = []
    for f in friends[:20]:
        name = f.full_name or f.username or str(f.chat_id)
        rows.append([{"text": f"{name} ({f.chat_id})"}])
    rows.append([{"text": "🔙 بازگشت"}])
    return {"keyboard": rows, "resize_keyboard": True}


# ─── Admin keyboards ─────────────────────────────────────────────────────────────

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


def broadcast_cancel_kb(job_id: int):
    return {
        "inline_keyboard": [[
            {"text": "❌ لغو ارسال همگانی", "callback_data": f"bcast_cancel_{job_id}"},
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