from django.db import models
from django.utils.safestring import mark_safe
from django.utils import timezone

class BotUser(models.Model):
    chat_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=255, null=True, blank=True) # یوزرنیم بله
    referred_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subordinates')
    balance = models.IntegerField(default=0)
    last_wheel_spin = models.DateTimeField(null=True, blank=True)
    wins = models.IntegerField(default=0)  # جدید
    losses = models.IntegerField(default=0) # جدید
    status = models.CharField(max_length=20, default='idle')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.chat_id} - {self.username}"

class GameMatch(models.Model):
    player1 = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='matches_as_p1')
    player2 = models.ForeignKey(BotUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='matches_as_p2')
    amount = models.IntegerField() # مبلغ شرط‌بندی
    p1_move = models.CharField(max_length=10, null=True, blank=True)
    p2_move = models.CharField(max_length=10, null=True, blank=True)
    is_with_bot = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default='waiting') # waiting, active, finished
    created_at = models.DateTimeField(auto_now_add=True)

class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'در انتظار'),
        ('paid', 'پرداخت شده'),
        ('rejected', 'رد شده'),
    ]
    user = models.ForeignKey(BotUser, on_delete=models.CASCADE)
    amount = models.IntegerField()
    card_number = models.CharField(max_length=16)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # بررسی می‌کنیم که آیا این یک آبجکت از قبل موجود است یا خیر
        if self.pk:
            # گرفتن وضعیت قبلی از دیتابیس
            old_status = WithdrawalRequest.objects.get(pk=self.pk).status
            
            # اگر وضعیت از 'در انتظار' به 'رد شده' تغییر کرد
            if old_status == 'pending' and self.status == 'rejected':
                self.user.balance += self.amount
                self.user.save()
                # اطلاع‌رسانی به کاربر (اختیاری - نیاز به import تابع send_message دارد)
                try:
                    from rps.bale_api import send_message
                    msg = f"❌ درخواست برداشت شما به مبلغ {self.amount:,} تومان رد شد و مبلغ به حساب شما بازگشت."
                    send_message(self.user.chat_id, msg)
                except:
                    pass
        
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.chat_id} - {self.amount}"

class CardNumber(models.Model):
    number = models.CharField(max_length=16, verbose_name="شماره کارت")
    owner_name = models.CharField(max_length=100, verbose_name="نام صاحب حساب")
    is_active = models.BooleanField(default=True, verbose_name="فعال باشد؟")

    def __str__(self):
        return f"{self.owner_name} - {self.number}"


class DepositRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', '⏳ در انتظار بررسی'),
        ('approved', '✅ تایید شده'),
        ('rejected', '❌ رد شده'),
    ]

    user = models.ForeignKey('BotUser', on_delete=models.CASCADE, verbose_name="کاربر")
    amount = models.BigIntegerField(verbose_name="مبلغ (تومان)")
    receipt_image = models.ImageField(
    upload_to='receipts/', 
    verbose_name="عکس رسید", 
    null=True,   # اجازه دادن به دیتابیس برای خالی بودن در رکوردهای قدیمی
    blank=True   # اجازه دادن به پنل ادمین برای خالی گذاشتن (اختیاری)
)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="وضعیت")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ارسال")

    def save(self, *args, **kwargs):
        # اگر وضعیت به تایید شده تغییر کرد، موجودی کاربر را شارژ کن
        if self.pk:
            old_status = DepositRequest.objects.get(pk=self.pk).status
            if old_status == 'pending' and self.status == 'approved':
                self.user.balance += self.amount
                self.user.save()
        super().save(*args, **kwargs)

def receipt_preview(self):
    if self.receipt_image:
        return mark_safe(f'<img src="{self.receipt_image.url}" width="150" />')
    return "بدون تصویر"

