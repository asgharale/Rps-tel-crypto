"""
tasks.py  –  Celery tasks.

Tasks:
  send_message_task          – queue a bot message (retry-safe)
  edit_message_task          – edit an already-sent message
  search_animation_task      – animated "searching for opponent" updater
  expire_search_task         – refund fee if no opponent found in 5 min
"""

import random
from celery import shared_task
from rps.tg_api import send_message_direct, edit_message_direct, answer_callback_direct


# ─── Message sending ──────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def send_message_task(self, chat_id, text, reply_markup=None):
    return send_message_direct(chat_id, text, reply_markup)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def edit_message_task(self, chat_id, message_id, new_text, reply_markup=None):
    return edit_message_direct(chat_id, message_id, new_text, reply_markup)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def answer_callback_task(self, callback_id, text, show_alert=False):
    return answer_callback_direct(callback_id, text, show_alert)


# ─── Search animation ─────────────────────────────────────────────────────────

SEARCH_FRAMES = [
    "🔍 در حال جستجوی حریف",
    "🔎 در حال جستجوی حریف ·",
    "🔍 در حال جستجوی حریف ··",
    "🔎 در حال جستجوی حریف ···",
]

SEARCH_TIPS = [
    "💡 نکته: موجودی کافی داشته باشید تا در بازی‌های آنلاین شرکت کنید.",
    "🏆 نکته: با برد بیشتر، در رتبه‌بندی بالاتر می‌روید!",
    "👥 نکته: دوستان خود را دعوت کنید و پاداش بگیرید.",
    "⚡ نکته: واکنش سریع در بازی دوز مهم است!",
    "🎯 نکته: در بازی سنگ کاغذ قیچی عجله نکنید.",
]


@shared_task
def search_animation_task(match_id: int, tick: int = 0):
    """
    Called every ~3 seconds to animate the searching message.
    Stops itself when match is no longer 'searching'.
    """
    from rps.models import GameMatch
    try:
        match = GameMatch.objects.get(pk=match_id)
    except GameMatch.DoesNotExist:
        return

    if match.status != 'searching':
        return

    frame = SEARCH_FRAMES[tick % len(SEARCH_FRAMES)]
    tip = SEARCH_TIPS[tick % len(SEARCH_TIPS)]

    # Calculate elapsed time
    from django.utils import timezone
    elapsed_secs = 0
    if match.search_started_at:
        elapsed_secs = int((timezone.now() - match.search_started_at).total_seconds())

    mins = elapsed_secs // 60
    secs = elapsed_secs % 60
    time_str = f"{mins}:{secs:02d}"

    names = {'rps': 'سنگ کاغذ قیچی', 'ttt': 'دوز', 'c4f': 'چهار در یک'}
    game_name = names.get(match.game_type, match.game_type)
    bet_str = f"${match.bet_cents/100:.2f}" if match.bet_cents else "آفلاین"

    text = (
        f"{frame}\n\n"
        f"🎮 بازی: *{game_name}*\n"
        f"💰 شرط: *{bet_str}*\n"
        f"⏱ زمان جستجو: `{time_str}`\n\n"
        f"{tip}\n\n"
        f"_در حال بررسی بازیکنان آنلاین..._"
    )

    if match.p1_search_msg_id:
        from rps.tg_api import edit_message_direct
        from rps.keyboards import cancel_search_kb
        edit_message_direct(
            match.player1.chat_id,
            match.p1_search_msg_id,
            text,
            cancel_search_kb(),
        )

    # Schedule next tick (stops at 100 ticks = ~5 min)
    if tick < 100:
        search_animation_task.apply_async(
            args=[match_id, tick + 1],
            countdown=3,
        )


@shared_task
def expire_search_task(match_id: int):
    """
    Called after 5 minutes (300 seconds) to expire a search.
    Refunds the search fee and sends a friendly sorry message.
    """
    from rps.models import GameMatch
    from rps.tg_api import send_message_direct
    from rps.keyboards import main_menu

    try:
        match = GameMatch.objects.select_related('player1').get(pk=match_id)
    except GameMatch.DoesNotExist:
        return

    if match.status != 'searching':
        return  # Already matched or cancelled

    # Refund search fee
    player = match.player1
    if match.search_fee_cents > 0:
        player.balance_cents += match.search_fee_cents
        player.save(update_fields=['balance_cents'])

    match.status = 'cancelled'
    match.save(update_fields=['status'])
    player.status = 'idle'
    player.save(update_fields=['status'])

    is_admin = _is_admin(player.chat_id)
    send_message_direct(
        player.chat_id,
        "😔 *متأسفانه حریفی یافت نشد!*\n\n"
        "⏱ بعد از ۵ دقیقه جستجو، هزینه جستجو به کیف پول شما بازگشت.\n"
        "🔄 می‌توانید دوباره تلاش کنید یا یک بازی آفلاین شروع کنید.",
        main_menu(is_admin),
    )


def _is_admin(chat_id):
    import os
    admin_ids = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    return chat_id in admin_idsSS