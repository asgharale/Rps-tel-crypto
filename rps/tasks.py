"""
tasks.py  –  Celery tasks.

Tasks:
  send_message_task          – queue a bot message (retry-safe)
  edit_message_task          – edit an already-sent message
  search_animation_task      – animated "searching for opponent" updater
  expire_search_task         – refund entry fee if no opponent found in 5 min
  broadcast_task              – admin "message everyone", checks for cancellation
"""

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

    names = {'rps': 'سنگ کاغذ قیچی', 'ttt': 'دوز', 'c4f': 'چهار در یک', 'ms': 'ماین‌یاب'}
    game_name = names.get(match.game_type, match.game_type)

    text = (
        f"{frame}\n\n"
        f"🎮 بازی: *{game_name}*\n"
        f"💵 هزینه ورود: *${match.entry_fee_cents/100:.2f}*\n"
        f"🏆 جایزه برنده: *${match.prize_cents/100:.2f}*\n"
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
    Refunds the entry fee and sends a friendly sorry message.
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

    # Refund entry fee
    player = match.player1
    if match.entry_fee_cents > 0:
        player.balance_cents += match.entry_fee_cents
        player.save(update_fields=['balance_cents'])

    match.status = 'cancelled'
    match.save(update_fields=['status'])
    player.status = 'idle'
    player.save(update_fields=['status'])

    is_admin = _is_admin(player.chat_id)
    send_message_direct(
        player.chat_id,
        "😔 *متأسفانه حریفی یافت نشد!*\n\n"
        "⏱ بعد از ۵ دقیقه جستجو، هزینه ورود به کیف پول شما بازگشت.\n"
        "🔄 می‌توانید دوباره تلاش کنید یا یک بازی با ربات شروع کنید.",
        main_menu(is_admin),
    )


def _is_admin(chat_id):
    import os
    admin_ids = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    return chat_id in admin_ids


# ─── Broadcast ("message everyone", cancellable) ───────────────────────────────

@shared_task
def broadcast_task(job_id: int):
    """
    Sends job.text to every non-banned user, in small batches, re-checking
    after every batch whether the admin pressed "❌ لغو ارسال همگانی".
    """
    from rps.models import BotUser, BroadcastJob
    from rps.tg_api import send_message_direct

    try:
        job = BroadcastJob.objects.get(pk=job_id)
    except BroadcastJob.DoesNotExist:
        return

    user_ids = list(BotUser.objects.filter(is_banned=False).values_list('chat_id', flat=True))
    job.total = len(user_ids)
    job.save(update_fields=['total'])

    BATCH = 20
    for i in range(0, len(user_ids), BATCH):
        # Re-fetch so we see a cancellation made mid-flight from the callback handler.
        job.refresh_from_db(fields=['status'])
        if job.status == 'cancelled':
            break

        for chat_id in user_ids[i:i + BATCH]:
            try:
                result = send_message_direct(chat_id, job.text)
                if result and result.get('ok'):
                    job.sent += 1
                else:
                    job.failed += 1
            except Exception:
                job.failed += 1
        job.save(update_fields=['sent', 'failed'])

    if job.status != 'cancelled':
        job.status = 'done'
        job.save(update_fields=['status'])

    summary = (
        f"✅ *ارسال همگانی پایان یافت*\n\n"
        f"📨 موفق: *{job.sent}*\n"
        f"❌ ناموفق: *{job.failed}*\n"
        f"👥 مجموع: *{job.total}*"
        if job.status == 'done' else
        f"⛔️ *ارسال همگانی لغو شد*\n\n"
        f"📨 قبل از لغو ارسال شد: *{job.sent}* از *{job.total}*"
    )
    if job.status_msg_id:
        from rps.tg_api import edit_message_direct
        edit_message_direct(job.admin_chat_id, job.status_msg_id, summary)
    else:
        send_message_direct(job.admin_chat_id, summary)