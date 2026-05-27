from django.contrib import admin
from django.utils.safestring import mark_safe
from .models import BotUser, WithdrawalRequest, CardNumber, DepositRequest

@admin.register(BotUser)
class BotUserAdmin(admin.ModelAdmin):
    list_display = ('chat_id', 'balance', 'created_at')
    search_fields = ('chat_id',)

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
    readonly_fields = ('receipt_preview_display',) # فقط دیدن پیش‌نمایش در صفحه ویرایش
    
    # برای اینکه ادمین بتواند سریع تایید یا رد کند
    list_editable = ('status',) 

    def receipt_preview_display(self, obj):
        if obj.receipt_image:
            return mark_safe(f'<a href="{obj.receipt_image.url}" target="_blank">'
                             f'<img src="{obj.receipt_image.url}" width="150" style="border-radius: 5px;" />'
                             f'</a>')
        return "بدون تصویر"

    receipt_preview_display.short_description = "پیش‌نمایش رسید"
