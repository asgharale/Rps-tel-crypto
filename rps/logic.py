import re
import time
import random
import requests 
from django.db.models import Q
from rps.models import BotUser, WithdrawalRequest, GameMatch, CardNumber, DepositRequest
from rps.bale_api import send_message
from django.core.files.base import ContentFile
from datetime import timedelta
from django.utils import timezone
import jdatetime

# --- Settings ---
ADMIN_IDS = [101632784]
REWARD_AMOUNT = 30000
MIN_WITHDRAWAL = 50000
MIN_BET = 10000
TAX_RATE = 0.10

def download_bale_file(file_id):
    token = "647645551:fgXDo-5aKwh9_lYVXJWHDlo-sAn6yy3kiQ4"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    get_file_url = f"https://api.bale.ai/bot{token}/getFile?file_id={file_id}"
    
    try:
        # استفاده از timeout برای جلوگیری از متوقف شدن طولانی برنامه
        response = requests.get(get_file_url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("ok"):
                file_path = res_json["result"]["file_path"]
                download_url = f"https://api.bale.ai/file/bot{token}/{file_path}"
                
                # Download file
                file_res = requests.getFile(download_url, headers=headers, timeout=20)
                if file_res.status_code == 200:
                    return file_res.content
                else:
                    print(f"Error downloading file data: {file_res.status_code}")
        elif response.status_code == 503:
            print("Bale Server Error (503): سرور بله موقتا در دسترس نیست. لطفا چند لحظه دیگر تلاش کنید.")
        else:
            print(f"Bale API Error: {response.status_code} - {response.text}")
            
    except requests.exceptions.Timeout:
        print("Timeout Error: زمان پاسخگویی سرور بله تمام شد.")
    except Exception as e:
        print(f"Download Exception: {e}")
    return None

def get_keyboards(chat_id):
    main_keyboard = {
        "keyboard": [
            [{"text": "🎮 شروع بازی (سنگ، کاغذ، قیچی)"}],
            [{"text": "👤 پروفایل من"}, {"text": "💰 موجودی و آمار"}],
            [{"text": "🔗 زیرمجموعه‌گیری"}, {"text": "🏆 برترین‌ها"}],
            [{"text": "🎡 گردونه شانس"}],
            [{"text": "💳 درخواست واریز"}, {"text": "➕ افزایش موجودی"}],
            [{"text": "❓ راهنما و قوانین"}]
        ], "resize_keyboard": True
    }
    if chat_id in ADMIN_IDS:
        main_keyboard["keyboard"].insert(0, [{"text": "⚙️ مدیریت پنل"}])

    bet_keyboard = {
        "keyboard": [
            [{"text": "💰 10,000 تومان"}, {"text": "💰 25,000 تومان"}],
            [{"text": "💰 50,000 تومان"}],
            [{"text": "🔙 بازگشت به منوی اصلی"}]
        ], "resize_keyboard": True
    }

    game_keyboard = {
        "keyboard": [[{"text": "🪨 سنگ"}, {"text": "📄 کاغذ"}, {"text": "✂️ قیچی"}]],
        "resize_keyboard": True
    }
    
    

    return main_keyboard, bet_keyboard, game_keyboard

def handle_bot_logic(chat_id, text, photo_id=None, current_username=None):
    global username_display
    top_users = BotUser.objects.order_by('-wins')[:5]
    try:
        user, created = BotUser.objects.get_or_create(chat_id=chat_id)
        main_kb, bet_kb, game_kb = get_keyboards(chat_id)

        # --- بخش پردازش رفرال در اولین ورود ---
        if created:
            if text.startswith("/start ") and len(text) > 7:
                try:
                    inviter_id = text.split(" ")[1] # استخراج آیدی دعوت‌کننده
                    inviter = BotUser.objects.get(chat_id=inviter_id)
                    
                    # جلوگیری از خود-دعوتی
                    if inviter.chat_id != user.chat_id:
                        user.referred_by = inviter
                        user.save()
                        
                        # (اختیاری) هدیه به دعوت‌کننده
                        inviter.balance += REWARD_AMOUNT
                        inviter.save()
                        
                        # اطلاع‌رسانی به دعوت‌کننده
                        send_message(inviter.chat_id, f"🎊 یک نفر با لینک شما عضو ربات شد!\n🎁 مبلغ {REWARD_AMOUNT:,} تومان به موجودی شما اضافه شد.")
                except:
                    pass

        if current_username and user.username != current_username:
            user.username = current_username
            user.save()

        # --- بخش مدیریت (فقط برای ADMIN_ID) ---
        if chat_id in ADMIN_IDS:
            if text == "⚙️ مدیریت پنل":
                print("hhhhh")
                user.status = 'wait_for_broadcast'
                print("sssss")
                user.save()
                return send_message(chat_id, "📝 لطفاً پیام خود را بنویسید تا برای تمام کاربران ارسال شود:\n(برای لغو، بنویسید: انصراف)", main_kb)
            
            elif user.status == 'wait_for_broadcast':
                if text == "انصراف":
                    user.status = 'idle'
                    user.save()
                    return send_message(chat_id, "❌ عملیات ارسال لغو شد.", main_kb)
                
                # شروع عملیات ارسال همگانی
                all_users = BotUser.objects.all()
                total_users = all_users.count()
                success_count = 0
                fail_count = 0

                send_message(chat_id, f"⏳ در حال ارسال به {total_users} کاربر... لطفاً صبور باشید.")

                for target_user in all_users:
                    try:
                        # ارسال پیام به هر کاربر
                        response = send_message(target_user.chat_id, text)
                        # بررسی اینکه آیا ارسال موفق بوده (بسته به خروجی تابع send_message شما)
                        success_count += 1
                        
                        # ایجاد وقفه بسیار کوتاه برای فشار نیامدن به سرور (اختیاری)
                        # time.sleep(0.05) 
                    except Exception as e:
                        fail_count += 1
                        print(f"Error sending to {target_user.chat_id}: {e}")

                # اتمام عملیات
                user.status = 'idle'
                user.save()
                
                report = (
                    "✅ *ارسال همگانی پایان یافت*\n\n"
                    f"👤 کل کاربران: {total_users}\n"
                    f"✔️ موفق: {success_count}\n"
                    f"❌ ناموفق: {fail_count}"
                )
                return send_message(chat_id, report, main_kb)
        # مدیریت پیام‌های متفرقه
        elif user.status == 'wait_for_receipt' and not photo_id:
            send_message(chat_id, "⚠️ لطفا فقط *عکس* فیش را ارسال کنید.")
            user.status = 'idle'

        # Main Menu & Start 
        if text == "/start" or text == "🔙 بازگشت به منوی اصلی":
            user.status = 'idle'
            user.save()
            send_message(chat_id, "👋 به ربات بازی خوش آمدید!\nلطفاً یک گزینه را انتخاب کنید:", main_kb)

        # Start Game
        elif text == "🎮 شروع بازی (سنگ، کاغذ، قیچی)":
            send_message(chat_id, "🕹 انتخاب مبلغ شرط‌بندی\n────────────────\nلطفاً مبلغ مورد نظر را انتخاب کنید:", bet_kb)

        # Choosing Bet Amount
        elif "تومان" in text and "💰" in text:
            nums = re.findall(r'\d+', text.replace(',', ''))
            if nums:
                amount_toman = int(nums[0])
                if user.balance < amount_toman:
                    send_message(chat_id, f"❌ موجودی ناکافی!\n💰 موجودی: {user.balance:,} تومان", main_kb)
                    return

                waiting_match = GameMatch.objects.filter(amount=amount_toman, status='waiting').exclude(player1=user).first()
                if waiting_match:
                    waiting_match.player2 = user
                    waiting_match.status = 'active'
                    waiting_match.save()
                    
                    user.status = 'playing'; user.balance -= amount_toman; user.save()
                    waiting_match.player1.status = 'playing'; waiting_match.player1.balance -= amount_toman; waiting_match.player1.save()

                    msg = f"🎮 رقیب پیدا شد!\n💰 مبلغ: {amount_toman:,} تومان\n🏁 حرکت خود را انتخاب کنید:"
                    send_message(waiting_match.player1.chat_id, msg, game_kb)
                    send_message(waiting_match.player2.chat_id, msg, game_kb)
                else:
                    GameMatch.objects.create(player1=user, amount=amount_toman, status='waiting')
                    user.status = 'waiting'; user.save()
                    send_message(chat_id, "⏳ در حال جستجوی رقیب (۳۰ ثانیه)...", {"keyboard": [[{"text": "🔙 بازگشت به منوی اصلی"}]], "resize_keyboard": True})

        # Saving Player Movement
        elif text in ["🪨 سنگ", "📄 کاغذ", "✂️ قیچی"]:
            match = GameMatch.objects.filter(status='active').filter(Q(player1=user) | Q(player2=user)).first()
            if not match:
                send_message(chat_id, "⚠️ بازی فعالی یافت نشد.", main_kb)
                return
            
            if match.player1 == user: match.p1_move = text
            else: match.p2_move = text
            match.save()

            if match.is_with_bot or (match.p1_move and match.p2_move):
                process_match_result(match)
            else:
                send_message(chat_id, "✅ حرکت ثبت شد. منتظر رقیب...")

        # Profile
        elif text == "👤 پروفایل من":
            # ۱. محاسبه تعداد زیرمجموعه‌ها
            sub_count = BotUser.objects.filter(referred_by=user).count()
            
            shamsi_date = jdatetime.datetime.fromgregorian(datetime=user.created_at)
            formatted_date = shamsi_date.strftime("%Y/%m/%d - %H:%M")

            
            # ۳. آماده‌سازی متن پروفایل
            username_display = f"{user.username}" if user.username else "تنظیم نشده"
            
            profile_text = (
                "👤 *اطلاعات پروفایل شما*\n\n"
                f"🆔 یوزرنیم: {username_display}\n"
                f"🔢 شماره کاربری: `{user.chat_id}`\n"
                "────────────────\n"
                f"👥 تعداد زیرمجموعه: {sub_count} نفر\n"
                f"💰 موجودی حساب: {user.balance:,} تومان\n"
                f"📅 تاریخ عضویت: {formatted_date}"
            )
            
            send_message(chat_id, profile_text, main_kb)

        if text == "🔗 زیرمجموعه‌گیری":
            show_referral_menu(chat_id, user)

        elif text == "💰 موجودی و آمار":
            msg = f"🏦 *وضعیت حساب*\n────────────────\n💰 موجودی: {user.balance:,} تومان\n✅ برد: {user.wins}\n❌ باخت: {user.losses}"
            send_message(chat_id, msg, main_kb)

        elif text == "🏆 برترین‌ها":
            leaderboard = "🏆 *برترین‌های بازی*\n────────────────\n"
            for i, top_user in enumerate(top_users, 1):
                leaderboard += f"{i}. کاربر `{str(top_user.username)[:5]}...` | برد: *{top_user.wins}*\n"
            send_message(chat_id, leaderboard, main_kb)

        # ۶. افزایش موجودی (بخش مورد نظر شما)
        elif text == "➕ افزایش موجودی":
            cards = CardNumber.objects.filter(is_active=True)
            if not cards.exists():
                send_message(chat_id, "⚠️ حسابی برای واریز در دسترس نیست.", main_kb)
                return
            
            card = random.choice(cards)
            user.status = 'wait_for_receipt'
            user.save()
            
            msg = (
                "💳 *اطلاعات کارت*\n"
                f"👤 نام: {card.owner_name}\n"
                f"🔢 شماره: `{card.number}`\n"
                "────────────────\n"
                "📸 لطفاً *عکس فیش* واریزی خود را ارسال کنید:\n"
                "در مرحله بعد از شما مبلغ انتقال داده شده پرسیده میشود"

            )
            send_message(chat_id, msg, {"keyboard": [[{"text": "🔙 بازگشت به منوی اصلی"}]], "resize_keyboard": True})

        # ۷. دریافت عکس رسید
        elif photo_id and user.status == 'wait_for_receipt':
            # ابتدا به کاربر اطلاع دهید
            send_message(chat_id, "⏳ در حال دریافت تصویر از سرور بله... لطفاً صبور باشید.")
            
            image_data = download_bale_file(photo_id)
            
            if image_data:
                # ذخیره در دیتابیس
                request_obj = DepositRequest(user=user, amount=0) # مبلغ را بعدا آپدیت می‌کنیم
                request_obj.receipt_image.save(f"{chat_id}_{int(time.time())}.jpg", ContentFile(image_data))
                request_obj.save()
                
                # تغییر وضعیت برای مرحله بعد (دریافت مبلغ)
                user.status = f'set_amount_{request_obj.id}' # آی‌دی رکورد را ذخیره می‌کنیم
                user.save()
                
                send_message(chat_id, "✅ تصویر با موفقیت دریافت شد.\n💰 حالا مبلغ واریزی را به *تومان* وارد کنید:")
            else:
                # اگر ۵۰۳ داد و دانلود نشد
                send_message(chat_id, "❌ متأسفانه سرور فایل بله موقتاً در دسترس نیست.\nلطفاً چند لحظه دیگر دوباره عکس را ارسال کنید یا از منوی اصلی اقدام کنید.")
        # ۸. دریافت مبلغ و ذخیره نهایی
        elif user.status.startswith('set_amount_') and text.isdigit():
            p_id = user.status.replace('set_amount_', '')
            amount_val = int(text)
            
            send_message(chat_id, "⏳ در حال پردازش و ذخیره...")
            
            image_data = download_bale_file(p_id)
            if image_data:
                request_obj = DepositRequest(user=user, amount=amount_val)
                request_obj.receipt_image.save(f"{chat_id}_{int(time.time())}.jpg", ContentFile(image_data))
                request_obj.save()
                
                user.status = 'idle'
                user.save()
                send_message(chat_id, "🚀 رسید شما با موفقیت ثبت شد.\nپس از تایید ادمین، حساب شما شارژ می‌شود.", main_kb)
            else:
                send_message(chat_id, "❌ خطا در دریافت فایل از سرور بله. لطفاً دوباره عکس را بفرستید.")
                user.status = 'wait_for_receipt'
                user.save()

        # ۹. درخواست واریز (برداشت وجه)
        elif text == "💳 درخواست واریز":
            if user.balance < MIN_WITHDRAWAL:
                send_message(chat_id, f"⚠️ حداقل برداشت: {MIN_WITHDRAWAL:,} تومان", main_kb)
            else:
                sample = f"واریز\nمبلغ: {user.balance}\nکارت: 6037000000000000"
                send_message(chat_id, f"💳 لطفاً طبق فرمت زیر بفرستید:\n\n`{sample}`", main_kb)

        elif text.startswith("واریز"):
            lines = text.split('\n')
            if len(lines) >= 3:
                try:
                    amount = int(re.findall(r'\d+', lines[1])[0])
                    card = re.findall(r'\d+', lines[2])[0]
                    if len(card) == 16 and amount <= user.balance:
                        WithdrawalRequest.objects.create(user=user, amount=amount, card_number=card)
                        user.balance -= amount
                        user.save()
                        send_message(chat_id, "✅ درخواست برداشت ثبت شد.", main_kb)
                    else:
                        send_message(chat_id, "❌ موجودی ناکافی یا شماره کارت غلط است.")
                except:
                    send_message(chat_id, "❌ فرمت ارسال اشتباه است.")            


                # --- بخش گردونه شانس ---
        elif text == "🎡 گردونه شانس":
            success, message = spin_the_wheel(user)
            send_message(chat_id, message, main_kb)

        elif text == "❓ راهنما و قوانین":
            msg = """ 🎮 بخش بازی (سنگ، کاغذ، قیچی):۱. با انتخاب گزینه «شروع بازی»، سیستم به دنبال یک حریف واقعی برای شما می‌گردد.۲. طبق قوانین، اگر تا ۳۰ ثانیه حریفی پیدا نشود، سیستم به صورت خودکار یک هوش مصنوعی (Bot) را به عنوان رقیب شما انتخاب می‌کند تا بازی شروع شود.۳. نتیجه هر بازی بلافاصله بر موجودی شما تأثیر خواهد گذاشت.

💰 امور مالی و افزایش موجودی:۱. برای شرکت در بازی‌های سطح بالاتر، می‌توانید از بخش «افزایش موجودی» اقدام کنید.۲. در صورت ارسال رسید یا تصویر پرداخت، عکس شما مستقیماً توسط مدیریت بررسی و تأیید می‌شود. لطفاً تصاویر واضح ارسال فرمایید.

🔗 سیستم زیرمجموعه‌گیری:

با دعوت از دوستان خود از طریق لینک اختصاصی، در سود بازی‌های آن‌ها شریک شوید. پاداش شما بلافاصله به کیف پولتان در ربات اضافه می‌گردد.

⚖️ قوانین و مقررات:

تعدد اکانت: ایجاد چند حساب کاربری برای سوءاستفاده از سیستم زیرمجموعه‌گیری ممنوع است و منجر به مسدود شدن حساب می‌گردد.

توهین و ادب: هرگونه ارسال پیام نامناسب یا سوءاستفاده از بخش ارسال عکس برای ادمین، باعث محرومیت دائم شما خواهد شد.

تراکنش‌ها: مسئولیت وارد کردن اطلاعات صحیح در بخش واریز و برداشت بر عهده کاربر است.

پشتیبانی: در صورت بروز هرگونه مشکل فنی، اسکرین‌شات خطا را برای ما ارسال کنید تا در اسرع وقت بررسی شود."""

            send_message(chat_id, msg, main_kb)


    except Exception as e:
        print(f"Logic Error: {e}")

def process_match_result(match):
    """محاسبه برنده و بازگشت به منوی اصلی"""
    try:
        p1 = match.player1
        p2 = match.player2
        m1 = match.p1_move
        m2 = match.p2_move
        
        # دریافت کیبورد اصلی برای هر دو بازیکن
        main_kb_p1, _, _ = get_keyboards(p1.chat_id)
        main_kb_p2 = None
        if p2: main_kb_p2, _, _ = get_keyboards(p2.chat_id)

        # منطق برنده
        winner = None
        result_type = "draw"
        if m1 == m2: result_type = "draw"
        elif (m1=="🪨 سنگ" and m2=="✂️ قیچی") or (m1=="📄 کاغذ" and m2=="🪨 سنگ") or (m1=="✂️ قیچی" and m2=="📄 کاغذ"):
            winner = p1; result_type = "p1"
        else:
            winner = p2; result_type = "p2"

        tax = int(match.amount * TAX_RATE)
        net_prize = match.amount - tax
        total_prize = match.amount + net_prize

        # اطلاع‌رسانی و بازگرداندن کیبورد اصلی
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
        
        else: # p2 wins
            p1.losses += 1
            send_message(p1.chat_id, f"💀 باختید!\n🤖 رقیب: {m2}", main_kb_p1)
            if p2 and not match.is_with_bot:
                p2.balance += total_prize; p2.wins += 1
                send_message(p2.chat_id, f"🎉 بردید!\n🤖 رقیب: {m1}\n💰 سود: {net_prize:,} تومان", main_kb_p2)

        # آزاد کردن وضعیت کاربران
        p1.status = 'idle'; p1.save()
        if p2: p2.status = 'idle'; p2.save()
        match.status = 'finished'; match.save()

    except Exception as e:
        print(f"Result Error: {e}")

def spin_the_wheel(user):
    now = timezone.now()
    
    # ۱. بررسی محدودیت ۲۴ ساعت
    if user.last_wheel_spin:
        time_passed = now - user.last_wheel_spin
        if time_passed < timedelta(hours=24):
            remaining_time = timedelta(hours=24) - time_passed
            hours = remaining_time.seconds // 3600
            minutes = (remaining_time.seconds // 60) % 60
            return False, f"⚠️ شما در ۲۴ ساعت گذشته از گردونه استفاده کرده‌اید.\n⏳ زمان باقی‌مانده: {hours} ساعت و {minutes} دقیقه"

    # ۲. تعریف جوایز و شانس‌ها (وزن‌ها)
    # گزینه‌ها: 0 (پوچ)، 500 تومان، 1000 تومان، 2000 تومان، 5000 تومان
    prizes = [0, 5000, 10000, 20000, 50000]
    weights = [50, 25, 15, 7, 3] # شانس پوچ ۵۰٪، شانس ۵ هزار تومان ۳٪ است.

    result = random.choices(prizes, weights=weights, k=1)[0]

    # ۳. به‌روزرسانی وضعیت کاربر
    user.last_wheel_spin = now
    if result > 0:
        user.balance += result
        user.save()
        return True, f"🎊 تبریک! گردونه ایستاد و شما برنده *{result:,} تومان* هدیه نقدی شدید.\n💰 موجودی جدید: {user.balance:,} تومان"
    else:
        user.save()
        return True, "😔 متأسفانه این بار گردونه روی پوچ ایستاد! فردا دوباره شانس خودت رو امتحان کن."

def show_referral_menu(chat_id, user):
    bot_username = "rps_1v1_bot" # آیدی ربات خود را اینجا بنویسید (بدون @)
    referral_link = f"https://ble.ir/{bot_username}?start={user.chat_id}"
    
    sub_count = BotUser.objects.filter(referred_by=user).count()
    
    msg = (
        "👥 *لینک کسب درآمد از دعوت* \n\n"
        "با دعوت دوستان خود به ربات، برای هر عضویت هدیه نقدی دریافت کنید!\n\n"
        f"📊 آمار شما: {sub_count} زیرمجموعه\n"
        f"🎁 پاداش هر دعوت: *{REWARD_AMOUNT}* تومان\n"
        "────────────────\n"
        "🔗 *لینک دعوت اختصاصی شما:* \n"
        f"`{referral_link}`\n\n"
        "متن بالا را کپی کرده و برای دوستان خود بفرستید."
    )
    
    # دکمه بازگشت
    kb = {"keyboard": [[{"text": "🔙 بازگشت به منوی اصلی"}]], "resize_keyboard": True}
    send_message(chat_id, msg, kb)