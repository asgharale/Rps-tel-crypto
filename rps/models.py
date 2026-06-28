"""
models.py  –  Full data model for the Telegram game bot.

Tables:
  BotUser               – registered users (name, age, phone, wallet, avatar)
  Friendship            – bi-directional friend links (pending / accepted)
  FriendRequest         – one-directional incoming request (0.1$ fee)
  GameMatch             – RPS & Tic-Tac-Toe matches (online & offline)
  WithdrawalRequest     – withdrawal queue (min $15 / TRON wallet)
  DepositRequest        – card/receipt deposits
  CryptoDepositRequest  – crypto wallet deposits
  Report                – user reports (admin can ignore/ban)
"""

from django.db import models
from django.utils import timezone
from django.utils.safestring import mark_safe


# ─── User ─────────────────────────────────────────────────────────────────────

class BotUser(models.Model):
    chat_id    = models.BigIntegerField(unique=True)
    username   = models.CharField(max_length=255, null=True, blank=True)
    full_name  = models.CharField(max_length=255, null=True, blank=True, verbose_name="نام")
    age        = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="سن")
    phone      = models.CharField(max_length=20, null=True, blank=True, verbose_name="شماره تلفن")
    tron_wallet = models.CharField(max_length=100, null=True, blank=True, verbose_name="آدرس کیف پول ترون")
    avatar_file_id = models.CharField(max_length=255, null=True, blank=True, verbose_name="آواتار")

    # Referral
    referred_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subordinates'
    )
    referral_bonus_paid = models.BooleanField(default=False)  # bonus after profile completion

    # Balance in US cents (integer math, no float)
    # e.g. 100 = $1.00
    balance_cents = models.IntegerField(default=0, verbose_name="موجودی (سنت)")

    # Stats
    wins   = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    total_games = models.IntegerField(default=0)

    # Wheel
    last_wheel_spin = models.DateTimeField(null=True, blank=True)

    # FSM status for conversation flow
    status = models.CharField(max_length=200, default='idle')

    is_banned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Profile completion flag
    profile_complete = models.BooleanField(default=False)

    class Meta:
        ordering = ['-wins']

    def __str__(self):
        return f"{self.chat_id} – {self.full_name or self.username or '?'}"

    @property
    def balance_dollars(self) -> float:
        return self.balance_cents / 100

    def add_dollars(self, amount: float):
        self.balance_cents += round(amount * 100)
        self.save(update_fields=['balance_cents'])

    def deduct_dollars(self, amount: float) -> bool:
        cost = round(amount * 100)
        if self.balance_cents < cost:
            return False
        self.balance_cents -= cost
        self.save(update_fields=['balance_cents'])
        return True

    @property
    def win_rate(self) -> str:
        if self.total_games == 0:
            return "—"
        return f"{round(self.wins / self.total_games * 100)}٪"

    def check_and_grant_profile_bonus(self):
        """Grant $0.5 bonus once when profile is first completed."""
        if not self.profile_complete and self.full_name and self.age:
            self.profile_complete = True
            self.add_dollars(0.5)
            # Also grant the referrer $0.5 if they haven't been paid yet
            if self.referred_by and not self.referral_bonus_paid:
                self.referral_bonus_paid = True
                self.save(update_fields=['profile_complete', 'referral_bonus_paid'])
                self.referred_by.add_dollars(0.5)
                try:
                    from rps.tg_api import send_message
                    send_message(
                        self.referred_by.chat_id,
                        "🎊 یکی از دوستان دعوت‌شده‌ی شما پروفایلش را تکمیل کرد!\n"
                        "💵 *0.50 دلار* به کیف پول شما اضافه شد."
                    )
                except Exception:
                    pass
            else:
                self.save(update_fields=['profile_complete', 'referral_bonus_paid'])
            return True
        return False


# ─── Friendship ───────────────────────────────────────────────────────────────

class FriendRequest(models.Model):
    """One directional request. Fee of $0.10 deducted on send."""
    STATUS_CHOICES = [
        ('pending',  'در انتظار'),
        ('accepted', 'پذیرفته شده'),
        ('rejected', 'رد شده'),
    ]
    sender   = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='sent_requests')
    receiver = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='received_requests')
    status   = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    fee_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('sender', 'receiver')

    def __str__(self):
        return f"{self.sender} → {self.receiver} [{self.status}]"


class Friendship(models.Model):
    """Bi-directional friendship (created when FriendRequest is accepted)."""
    user1 = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='friendships_as_1')
    user2 = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='friendships_as_2')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user1', 'user2')

    def __str__(self):
        return f"{self.user1} ↔ {self.user2}"

    @classmethod
    def are_friends(cls, a: BotUser, b: BotUser) -> bool:
        return cls.objects.filter(
            models.Q(user1=a, user2=b) | models.Q(user1=b, user2=a)
        ).exists()

    @classmethod
    def get_friends(cls, user: BotUser):
        from django.db.models import Q
        qs = cls.objects.filter(Q(user1=user) | Q(user2=user)).select_related('user1', 'user2')
        friends = []
        for f in qs:
            friends.append(f.user2 if f.user1 == user else f.user1)
        return friends


# ─── Game Matches ─────────────────────────────────────────────────────────────

class GameMatch(models.Model):
    GAME_CHOICES = [('rps', 'سنگ کاغذ قیچی'), ('ttt', 'دوز')]
    STATUS_CHOICES = [
        ('searching', 'جستجو'),
        ('active',    'در حال بازی'),
        ('finished',  'پایان یافته'),
        ('cancelled', 'لغو شده'),
    ]

    game_type  = models.CharField(max_length=5, choices=GAME_CHOICES, default='rps')
    player1    = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='matches_as_p1')
    player2    = models.ForeignKey(BotUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='matches_as_p2')
    is_offline = models.BooleanField(default=False)   # vs bot

    # Bet in cents ($0 for offline)
    bet_cents      = models.IntegerField(default=0)
    search_fee_cents = models.IntegerField(default=0)  # fee paid to search ($0.20 for RPS, $0.30 for TTT)

    # RPS moves
    p1_move = models.CharField(max_length=20, null=True, blank=True)
    p2_move = models.CharField(max_length=20, null=True, blank=True)

    # Tic-Tac-Toe board: 9-char string, '.' = empty, 'X' = p1, 'O' = p2
    ttt_board      = models.CharField(max_length=9, default='.' * 9)
    ttt_turn       = models.SmallIntegerField(default=1)   # 1 = p1, 2 = p2
    ttt_winner     = models.SmallIntegerField(null=True, blank=True)  # 1, 2, or 0 for draw

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='searching')

    # For search animation tracking
    search_started_at = models.DateTimeField(null=True, blank=True)

    # Message IDs for editing the search animation message
    p1_search_msg_id = models.BigIntegerField(null=True, blank=True)

    created_at  = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Match#{self.pk} {self.game_type} {self.status}"

    # ── TTT helpers ───────────────────────────────────────────────────────────

    def ttt_get_board(self):
        return list(self.ttt_board)

    def ttt_make_move(self, position: int, symbol: str) -> bool:
        """Place symbol at position (0-8). Returns True if move was valid."""
        board = list(self.ttt_board)
        if board[position] != '.':
            return False
        board[position] = symbol
        self.ttt_board = ''.join(board)
        return True

    def ttt_check_winner(self):
        """Returns 'X', 'O', 'draw', or None."""
        b = self.ttt_board
        wins = [
            (0,1,2),(3,4,5),(6,7,8),  # rows
            (0,3,6),(1,4,7),(2,5,8),  # cols
            (0,4,8),(2,4,6),           # diagonals
        ]
        for a,c,d in wins:
            if b[a] == b[c] == b[d] != '.':
                return b[a]
        if '.' not in b:
            return 'draw'
        return None

    def ttt_bot_move(self):
        """Simple minimax bot move. Returns chosen position."""
        board = list(self.ttt_board)

        def minimax(b, is_max):
            w = _ttt_check(b)
            if w == 'O': return 10
            if w == 'X': return -10
            if '.' not in b: return 0
            if is_max:
                best = -100
                for i in range(9):
                    if b[i] == '.':
                        b[i] = 'O'
                        best = max(best, minimax(b, False))
                        b[i] = '.'
                return best
            else:
                best = 100
                for i in range(9):
                    if b[i] == '.':
                        b[i] = 'X'
                        best = min(best, minimax(b, True))
                        b[i] = '.'
                return best

        def _ttt_check(b):
            wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
            for a,c,d in wins:
                if b[a]==b[c]==b[d] != '.': return b[a]
            return None

        best_val, best_move = -100, -1
        for i in range(9):
            if board[i] == '.':
                board[i] = 'O'
                val = minimax(board, False)
                board[i] = '.'
                if val > best_val:
                    best_val, best_move = val, i
        return best_move


# ─── Wallet / Deposits / Withdrawals ─────────────────────────────────────────

class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [
        ('pending',  'در انتظار'),
        ('paid',     'پرداخت شده'),
        ('rejected', 'رد شده'),
    ]
    user         = models.ForeignKey(BotUser, on_delete=models.CASCADE)
    amount_cents = models.IntegerField(verbose_name="مبلغ (سنت)")
    tron_wallet  = models.CharField(max_length=100, verbose_name="آدرس ترون")
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at   = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            old_status = WithdrawalRequest.objects.get(pk=self.pk).status
            if old_status == 'pending' and self.status == 'rejected':
                # Refund balance
                self.user.balance_cents += self.amount_cents
                self.user.save(update_fields=['balance_cents'])
                try:
                    from rps.tg_api import send_message
                    send_message(
                        self.user.chat_id,
                        f"❌ درخواست برداشت شما به مبلغ "
                        f"*${self.amount_cents/100:.2f}* رد شد و مبلغ به کیف پول شما بازگشت."
                    )
                except Exception:
                    pass
            elif old_status == 'pending' and self.status == 'paid':
                try:
                    from rps.tg_api import send_message
                    send_message(
                        self.user.chat_id,
                        f"✅ برداشت *${self.amount_cents/100:.2f}* با موفقیت پردازش شد!\n"
                        f"💳 به آدرس `{self.tron_wallet}` ارسال شد."
                    )
                except Exception:
                    pass
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Withdrawal#{self.pk} {self.user.chat_id} ${self.amount_cents/100:.2f}"


class DepositRequest(models.Model):
    """Card/bank receipt deposit."""
    STATUS_CHOICES = [
        ('pending',  '⏳ در انتظار'),
        ('approved', '✅ تایید شده'),
        ('rejected', '❌ رد شده'),
    ]
    AMOUNT_CHOICES_CENTS = [100, 500, 1000, 2000]  # $1, $5, $10, $20

    user         = models.ForeignKey(BotUser, on_delete=models.CASCADE)
    amount_cents = models.IntegerField(default=0)
    receipt_image = models.ImageField(upload_to='receipts/', null=True, blank=True)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at   = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            old_status = DepositRequest.objects.get(pk=self.pk).status
            if old_status == 'pending' and self.status == 'approved':
                self.user.balance_cents += self.amount_cents
                self.user.save(update_fields=['balance_cents'])
                try:
                    from rps.tg_api import send_message
                    send_message(
                        self.user.chat_id,
                        f"✅ واریز *${self.amount_cents/100:.2f}* تایید شد و به کیف پول شما اضافه شد!"
                    )
                except Exception:
                    pass
            elif old_status == 'pending' and self.status == 'rejected':
                try:
                    from rps.tg_api import send_message
                    send_message(
                        self.user.chat_id,
                        "❌ رسید واریزی شما تایید نشد. لطفاً با پشتیبانی تماس بگیرید."
                    )
                except Exception:
                    pass
        super().save(*args, **kwargs)

    def receipt_preview(self):
        if self.receipt_image:
            return mark_safe(f'<img src="{self.receipt_image.url}" width="150"/>')
        return "بدون تصویر"

    def __str__(self):
        return f"Deposit#{self.pk} {self.user.chat_id} ${self.amount_cents/100:.2f}"


class CryptoDepositRequest(models.Model):
    PROOF_CHOICES = [
        ('screenshot', '📸 اسکرین‌شات'),
        ('tracking',   '🔢 کد پیگیری / TxHash'),
    ]
    STATUS_CHOICES = [
        ('pending',  '⏳ در انتظار'),
        ('verified', '✅ تایید شده'),
        ('rejected', '❌ رد شده'),
    ]

    user         = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='crypto_deposits')
    coin         = models.CharField(max_length=30)
    amount_cents = models.IntegerField(verbose_name="مبلغ (سنت)")
    proof_type   = models.CharField(max_length=15, choices=PROOF_CHOICES)
    proof_data   = models.TextField()
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at   = models.DateTimeField(auto_now_add=True)
    reviewed_at  = models.DateTimeField(null=True, blank=True)

    def approve(self):
        self.status = 'verified'
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reviewed_at'])
        self.user.balance_cents += self.amount_cents
        self.user.save(update_fields=['balance_cents'])
        try:
            from rps.tg_api import send_message
            send_message(
                self.user.chat_id,
                f"✅ *واریز کریپتو تایید شد!*\n\n"
                f"🪙 ارز: {self.coin}\n"
                f"💵 مبلغ: *${self.amount_cents/100:.2f}* به کیف پول شما اضافه شد.\n"
                f"🔖 شماره پیگیری: `#{self.pk}`"
            )
        except Exception:
            pass

    def reject(self):
        self.status = 'rejected'
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reviewed_at'])
        try:
            from rps.tg_api import send_message
            send_message(
                self.user.chat_id,
                f"❌ *واریز کریپتو رد شد*\n\n"
                f"🔖 شماره پیگیری: `#{self.pk}`\n"
                "در صورت نیاز با پشتیبانی تماس بگیرید."
            )
        except Exception:
            pass

    def __str__(self):
        return f"CryptoDeposit#{self.pk} {self.user.chat_id} {self.coin} {self.status}"


# ─── Reports ──────────────────────────────────────────────────────────────────

class Report(models.Model):
    STATUS_CHOICES = [
        ('pending',  'در انتظار'),
        ('ignored',  'نادیده گرفته'),
        ('banned',   'مسدود شده'),
    ]
    reporter   = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='reports_sent')
    reported   = models.ForeignKey(BotUser, on_delete=models.CASCADE, related_name='reports_received')
    reason     = models.TextField(blank=True)
    status     = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Report#{self.pk}: {self.reporter} → {self.reported} [{self.status}]"