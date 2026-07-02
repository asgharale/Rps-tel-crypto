# NOTE (unused/legacy): the active bot no longer runs through this
# long-polling management command. Message delivery now goes through the
# Telegram webhook (rps/views.py → tg_webhook) and Celery
# (rps/tasks.py → search_animation_task / expire_search_task /
# broadcast_task), so 5-minute search expiry and level-based entry fees are
# already handled there. This file also references fields that don't exist
# on the current models (match.amount, user.balance instead of
# entry_fee_cents / balance_cents) and predates the level/prize system —
# keeping it only for reference. Safe to delete once confirmed unused.

import time
import random
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from rps.models import GameMatch, BotUser
from rps.bale_api import get_updates, send_message
from rps.logic import handle_bot_logic

class Command(BaseCommand):
    def handle(self, *args, **options):
        offset = 0
        self.stdout.write("Bot is running...")
        
        while True:
            try:
                # چک کردن تایم‌اوت ۳۰ ثانیه‌ای
                self.check_for_bot_game()

                # دریافت پیام‌ها
                updates = get_updates(offset)
                if updates and "result" in updates:
                    for update in updates["result"]:
                        
                        offset = update["update_id"] + 1
                        message = update.get("message")
                        first_name = message.get("from", {}).get("first_name", "کاربر بله")

                        if message:
                            chat_id = message["chat"]["id"]
                            text = message.get("text", "")
                            photo = message.get("photo")
                            
                            photo_id = photo[-1]["file_id"] if photo else None
                            
                            # فراخوانی لاجیک در یک بلوک try دیگر برای جلوگیری از کراش کل ربات
                            try:
                                handle_bot_logic(chat_id, text, photo_id, first_name)
                            except Exception as e:
                                print(f"Logic Error: {e}")

            except Exception as e:
                print(f"Connection Error: {e}")
                time.sleep(5) # در صورت خطای شبکه، کمی صبر کنید

    def check_for_bot_game(self):
        """تبدیل بازی‌های منتظر به بازی با بات بعد از ۳۰ ثانیه"""
        threshold = timezone.now() - timedelta(seconds=30)
        expired_matches = GameMatch.objects.filter(status='waiting', created_at__lt=threshold)

        for match in expired_matches:
            user = match.player1
            # کسر مبلغ از کاربر (چون در حالت انتظار کسر نشده بود)
            if user.balance >= match.amount:
                user.balance -= match.amount
                user.status = 'playing'
                user.save()

                # تغییر وضعیت بازی به فعال با بات
                match.status = 'active'
                match.is_with_bot = True
                # انتخاب حرکت رندوم برای بات از همین الان
                match.p2_move = random.choice(["🪨 سنگ", "📄 کاغذ", "✂️ قیچی"])
                match.save()

                msg = (
                    "⏱ *زمان انتظار پایان یافت*\n"
                    "──────────────\n"
                    "رقیبی پیدا نشد. شما اکنون با *بات* بازی می‌کنید.\n"
                    "🕹 لطفاً حرکت خود را انتخاب کنید:"
                )
                game_kb = {"keyboard": [[{"text": "🪨 سنگ"}, {"text": "📄 کاغذ"}, {"text": "✂️ قیچی"}]], "resize_keyboard": True}
                send_message(user.chat_id, msg, game_kb)
            else:
                # اگر موجودی کاربر کم شده بود، بازی کنسل شود
                match.status = 'finished'
                match.save()
                send_message(user.chat_id, "❌ موجودی شما برای شروع بازی با بات کافی نبود.")