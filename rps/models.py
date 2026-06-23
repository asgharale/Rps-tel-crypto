from django.db import models
from django.utils.safestring import mark_safe
from django.utils import timezone


class BotUser(models.Model):
    chat_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=255, null=True, blank=True)
    referred_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subordinates'
    )
    balance = models.IntegerField(default=0)
    last_wheel_spin = models.DateTimeField(null=True, blank=True)
    wins = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    status = models.CharField(max_length=50, default='idle')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.chat_id} - {self.username}"


class GameMatch(models.Model):
    player1 = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='matches_as_p1')
    player2 = models.ForeignKey(BotUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='matches_as_p2')
    amount = models.IntegerField()
    p1_move = models.CharField(max_length=20, null=True, blank=True)
    p2_move = models.CharField(max_length=20, null=True, blank=True)
    is_with_bot = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default='waiting')  # waiting, active, finished
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
        if self.pk:
            old_status = WithdrawalRequest.objects.get(pk=self.pk).status
            if old_status == 'pending' and self.status == 'rejected':
                self.user.balance += self.amount
                self.user.save()
                try:
                    from rps.bale_api import send_message
                    send_message(
                        self.user.chat_id,
                        f"❌ درخواست برداشت شما به مبلغ {self.amount:,} تومان رد شد و مبلغ به حساب شما بازگشت."
                    )
                except Exception:
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
    """Card/bank receipt deposit (existing flow)."""
    STATUS_CHOICES = [
        ('pending', '⏳ در انتظار بررسی'),
        ('approved', '✅ تایید شده'),
        ('rejected', '❌ رد شده'),
    ]
    user = models.ForeignKey(BotUser, on_delete=models.CASCADE, verbose_name="کاربر")
    amount = models.BigIntegerField(default=0, verbose_name="مبلغ (تومان)")
    receipt_image = models.ImageField(
        upload_to='receipts/',
        verbose_name="عکس رسید",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="وضعیت")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاریخ ارسال")

    def save(self, *args, **kwargs):
        if self.pk:
            old_status = DepositRequest.objects.get(pk=self.pk).status
            if old_status == 'pending' and self.status == 'approved':
                self.user.balance += self.amount
                self.user.save()
                try:
                    from rps.bale_api import send_message
                    send_message(
                        self.user.chat_id,
                        f"✅ واریز شما به مبلغ {self.amount:,} تومان تایید و به حساب‌تان اضافه شد."
                    )
                except Exception:
                    pass
        super().save(*args, **kwargs)

    def receipt_preview(self):
        if self.receipt_image:
            return mark_safe(f'<img src="{self.receipt_image.url}" width="150" />')
        return "بدون تصویر"

    def __str__(self):
        return f"{self.user.chat_id} – {self.amount} ({self.status})"


# ─── NEW: Crypto / Wallet Deposit ────────────────────────────────────────────

class CryptoDepositRequest(models.Model):
    """
    Deposit via crypto wallet.
    Proof is either a screenshot (photo_file_id from Bale) or a tracking/tx code.
    Admin reviews on Telegram via Verify / Unverify inline buttons.
    """
    PROOF_CHOICES = [
        ('screenshot', '📸 اسکرین‌شات'),
        ('tracking',   '🔢 کد پیگیری / TxHash'),
    ]
    STATUS_CHOICES = [
        ('pending',  '⏳ در انتظار'),
        ('verified', '✅ تایید شده'),
        ('rejected', '❌ رد شده'),
    ]

    user = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='crypto_deposits')
    coin = models.CharField(max_length=30, verbose_name="ارز دیجیتال")          # e.g. "USDT (TRC20)"
    amount = models.BigIntegerField(verbose_name="مبلغ درخواستی (تومان)")
    proof_type = models.CharField(max_length=15, choices=PROOF_CHOICES)
    # For screenshot: stores the Bale file_id
    # For tracking:   stores the tx hash / tracking code string
    proof_data = models.TextField(verbose_name="مدرک پرداخت")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def approve(self):
        """Credit user balance and mark verified."""
        self.status = 'verified'
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reviewed_at'])
        self.user.balance += self.amount
        self.user.save(update_fields=['balance'])

    def reject(self):
        self.status = 'rejected'
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reviewed_at'])

    def __str__(self):
        return f"CryptoDeposit #{self.pk} – {self.user.chat_id} – {self.coin} – {self.status}"