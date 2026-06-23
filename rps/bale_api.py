import requests
from django.conf import settings


# ─── Low-level helpers ───────────────────────────────────────────────────────

def _bale_url(method: str) -> str:
    return f"https://tapi.bale.ai/bot{settings.BALE_TOKEN}/{method}"


def send_message_direct(chat_id, text, reply_markup=None):
    """Blocking HTTP call — used only by the Celery worker."""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(_bale_url("sendMessage"), json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"send_message_direct error: {e}")
        return None


def send_message(chat_id, text, reply_markup=None):
    """Queue via Celery/Redis — use this everywhere in logic.py."""
    from rps.tasks import send_message_task
    send_message_task.delay(chat_id, text, reply_markup)


def get_updates(offset=0):
    params = {"offset": offset, "limit": 100, "timeout": 20}
    try:
        r = requests.get(_bale_url("getUpdates"), params=params, timeout=25)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"get_updates error: {e}")
    return None


def download_bale_file(file_id: str) -> bytes | None:
    """Download a file from Bale by file_id. Returns raw bytes or None."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    try:
        # Step 1 – resolve file path
        res = requests.get(
            _bale_url(f"getFile?file_id={file_id}"),
            headers=headers,
            timeout=10,
        )
        if res.status_code != 200:
            print(f"getFile HTTP {res.status_code}")
            return None
        data = res.json()
        if not data.get("ok"):
            print(f"getFile not ok: {data}")
            return None
        file_path = data["result"]["file_path"]

        # Step 2 – download content
        download_url = f"https://tapi.bale.ai/file/bot{settings.BALE_TOKEN}/{file_path}"
        file_res = requests.get(download_url, headers=headers, timeout=20)
        if file_res.status_code == 200:
            return file_res.content
        print(f"File download HTTP {file_res.status_code}")
    except requests.exceptions.Timeout:
        print("download_bale_file: Timeout")
    except Exception as e:
        print(f"download_bale_file exception: {e}")
    return None