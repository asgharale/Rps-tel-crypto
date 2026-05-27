from celery import shared_task
from rps.bale_api import send_message_direct


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5}
)
def send_message_task(self, chat_id, text, reply_markup=None):
    return send_message_direct(chat_id, text, reply_markup)