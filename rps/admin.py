from django.contrib import admin
from django.utils.safestring import mark_safe
from .models import (
    BotUser, Province, GameMatch, FriendRequest, Friendship,
    WithdrawalRequest, DepositRequest, CryptoDepositRequest, Report,
    BroadcastJob,
)


@admin.register(Province)
class ProvinceAdmin(admin.ModelAdmin):
    # Every field on the model is optional (null=True, blank=True) so you can
    # fill this in gradually from the admin panel without hitting validation errors.
    list_display = ('id', 'name', 'code', 'is_active', 'order', 'created_at')
    list_editable = ('is_active', 'order')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(BotUser)
class BotUserAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'full_name', 'username', 'age', 'province', 'balance_display', 'wins', 'losses', 'is_banned', 'created_at')
    search_fields = ('chat_id', 'username', 'full_name', 'phone', 'tron_wallet')
    list_filter = ('is_banned', 'profile_complete', 'province')
    readonly_fields = ('created_at',)

    def balance_display(self, obj):
        return f"${obj.balance_cents/100:.2f}"
    balance_display.short_description = "موجودی"


@admin.register(GameMatch)
class GameMatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'game_type', 'mode', 'level', 'player1', 'player2', 'entry_fee_display', 'prize_display', 'status', 'is_offline', 'created_at')
    list_filter = ('game_type', 'mode', 'level', 'status', 'is_offline')

    def entry_fee_display(self, obj):
        return f"${obj.entry_fee_cents/100:.2f}"
    entry_fee_display.short_description = "هزینه ورود"

    def prize_display(self, obj):
        return f"${obj.prize_cents/100:.2f}"
    prize_display.short_description = "جایزه"


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ('sender', 'receiver', 'status', 'created_at')
    list_filter = ('status',)


@admin.register(Friendship)
class FriendshipAdmin(admin.ModelAdmin):
    list_display = ('user1', 'user2', 'created_at')


@admin.register(WithdrawalRequest)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'amount_display', 'tron_wallet', 'status', 'created_at')
    list_filter = ('status',)
    actions = ['mark_as_paid', 'mark_as_rejected']

    def amount_display(self, obj):
        return f"${obj.amount_cents/100:.2f}"
    amount_display.short_description = "مبلغ"

    def mark_as_paid(self, request, queryset):
        for obj in queryset.filter(status='pending'):
            obj.status = 'paid'
            obj.save()
    mark_as_paid.short_description = "تغییر به پرداخت‌شده"

    def mark_as_rejected(self, request, queryset):
        for obj in queryset.filter(status='pending'):
            obj.status = 'rejected'
            obj.save()
    mark_as_rejected.short_description = "رد کردن"


@admin.register(DepositRequest)
class DepositRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'amount_display', 'status', 'receipt_thumb', 'created_at')
    list_filter = ('status',)
    list_editable = ('status',)
    readonly_fields = ('receipt_thumb',)

    def amount_display(self, obj):
        return f"${obj.amount_cents/100:.2f}"
    amount_display.short_description = "مبلغ"

    def receipt_thumb(self, obj):
        if obj.receipt_image:
            return mark_safe(
                f'<a href="{obj.receipt_image.url}" target="_blank">'
                f'<img src="{obj.receipt_image.url}" width="100"/></a>'
            )
        return "—"
    receipt_thumb.short_description = "رسید"


@admin.register(CryptoDepositRequest)
class CryptoDepositAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'coin', 'amount_display', 'proof_type', 'status', 'created_at')
    list_filter = ('status', 'coin', 'proof_type')
    readonly_fields = ('user', 'coin', 'amount_cents', 'proof_type', 'proof_data', 'created_at')
    list_editable = ('status',)

    def amount_display(self, obj):
        return f"${obj.amount_cents/100:.2f}"
    amount_display.short_description = "مبلغ"

    def save_model(self, request, obj, form, change):
        if change:
            old = CryptoDepositRequest.objects.get(pk=obj.pk)
            if old.status == 'pending' and obj.status == 'verified':
                obj.approve(); return
            elif old.status == 'pending' and obj.status == 'rejected':
                obj.reject(); return
        super().save_model(request, obj, form, change)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'reporter', 'reported', 'status', 'created_at')
    list_filter = ('status',)
    actions = ['ignore_reports', 'ban_reported']

    def ignore_reports(self, request, queryset):
        queryset.update(status='ignored')
    ignore_reports.short_description = "نادیده گرفتن"

    def ban_reported(self, request, queryset):
        for rep in queryset.filter(status='pending'):
            rep.reported.is_banned = True
            rep.reported.save(update_fields=['is_banned'])
            rep.status = 'banned'
            rep.save()
    ban_reported.short_description = "مسدود کردن"


@admin.register(BroadcastJob)
class BroadcastJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'admin_chat_id', 'status', 'sent', 'failed', 'total', 'created_at')
    list_filter = ('status',)
    readonly_fields = ('admin_chat_id', 'text', 'total', 'sent', 'failed', 'status_msg_id', 'created_at')