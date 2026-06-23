from django.contrib import admin
from django.utils.safestring import mark_safe
from .models import BotUser, WithdrawalRequest, CardNumber, DepositRequest, CryptoDepositRequest


@admin.register(BotUser)
class BotUserAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'username', 'balance', 'wins', 'losses', 'created_at')
    search_fields = ('chat_id', 'username')


@admin.register(WithdrawalRequest)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'card_number', 'status', 'created_at')
    list_filter = ('status',)
    actions = ['mark_as_paid']

    def mark_as_paid(self, request, queryset):
        queryset.update(status='paid')
    mark_as_paid.short_description = "تغییر وضعیت به 'پرداخت شده'"


@admin.register(CardNumber)
class CardNumberAdmin(admin.ModelAdmin):
    list_display = ('owner_name', 'number', 'is_active')


@admin.register(DepositRequest)
class DepositRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'status', 'receipt_preview_display', 'created_at')
    list_filter = ('status', 'created_at')
    readonly_fields = ('receipt_preview_display',)
    list_editable = ('status',)

    def receipt_preview_display(self, obj):
        if obj.receipt_image:
            return mark_safe(
                f'<a href="{obj.receipt_image.url}" target="_blank">'
                f'<img src="{obj.receipt_image.url}" width="150" style="border-radius:5px"/>'
                f'</a>'
            )
        return "بدون تصویر"
    receipt_preview_display.short_description = "پیش‌نمایش رسید"


@admin.register(CryptoDepositRequest)
class CryptoDepositRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'coin', 'amount', 'proof_type', 'status', 'created_at', 'reviewed_at')
    list_filter = ('status', 'coin', 'proof_type')
    readonly_fields = ('user', 'coin', 'amount', 'proof_type', 'proof_data', 'created_at')
    list_editable = ('status',)
    ordering = ('-created_at',)

    def save_model(self, request, obj, form, change):
        """Auto credit/reject when status changes via Django admin."""
        if change:
            old = CryptoDepositRequest.objects.get(pk=obj.pk)
            if old.status == 'pending' and obj.status == 'verified':
                obj.approve()
                return
            elif old.status == 'pending' and obj.status == 'rejected':
                obj.reject()
                return
        super().save_model(request, obj, form, change)