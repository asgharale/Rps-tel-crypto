import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rps.logic import handle_bot_logic

def extract_message_data(update):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    first_name = message.get("from", {}).get("first_name", "کاربر")
    
    photo_id = None
    text = ""

    if "photo" in message:
        # دریافت باکیفیت‌ترین سایز عکس
        photo_id = message["photo"][-1]["file_id"]
        text = message.get("caption", "") # متن همراه عکس
    else:
        text = message.get("text", "")

    return chat_id, text, first_name, photo_id

@csrf_exempt
def bale_webhook(request):
    if request.method == 'POST':
        try:
            update = json.loads(request.body.decode('utf-8'))
            
            # استخراج داده‌ها (با همان تابعی که در مراحل قبل اصلاح کردیم)
            chat_id, text, first_name, photo_id = extract_message_data(update)
            
            if chat_id:
                # اجرای منطق ربات
                handle_bot_logic(chat_id, text, photo_id, first_name)
                
            return HttpResponse(status=200)
        except Exception as e:
            print(f"Webhook Error: {e}")
            return HttpResponse(status=500)
    return HttpResponse(status=400)