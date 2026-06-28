# 🤖 Telegram Game Bot – Setup & Deployment Guide

## Project Structure

```
rps_project/           ← Django project root
├── rps_project/
│   ├── settings.py    ← Use the provided settings.py
│   ├── urls.py        ← Use urls_project.py (rename)
│   ├── celery.py      ← Celery app
│   └── wsgi.py
├── rps/               ← Main app
│   ├── models.py
│   ├── views.py
│   ├── logic.py
│   ├── keyboards.py
│   ├── tg_api.py
│   ├── tasks.py
│   ├── admin.py
│   ├── apps.py
│   └── urls.py        ← Use urls_app.py (rename)
├── media/
├── staticfiles/
├── .env               ← Copy from .env.example and fill in
└── manage.py
```

---

## 1. Server Requirements

- Ubuntu 22.04 VPS (minimum 2 GB RAM for 5k users)
- Python 3.11+
- PostgreSQL 15+
- Redis 7+
- Nginx
- Gunicorn

---

## 2. PostgreSQL Setup

```bash
sudo apt install postgresql postgresql-contrib -y
sudo -u postgres psql

# Inside psql:
CREATE DATABASE rpsbot;
CREATE USER rpsbot WITH PASSWORD 'your_strong_password';
GRANT ALL PRIVILEGES ON DATABASE rpsbot TO rpsbot;
ALTER DATABASE rpsbot OWNER TO rpsbot;
\q
```

---

## 3. Redis Setup

```bash
sudo apt install redis-server -y
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Test:
redis-cli ping   # should return PONG
```

---

## 4. Python & Virtual Environment

```bash
cd /var/www/
git clone <your-repo> rpsbot
cd rpsbot

python3 -m venv venv
source venv/bin/activate

pip install django djangorestframework celery redis \
    psycopg2-binary python-dotenv requests Pillow gunicorn
```

---

## 5. File Setup

```bash
# Copy all provided files into their correct locations:
cp models.py    rps/models.py
cp views.py     rps/views.py
cp logic.py     rps/logic.py
cp keyboards.py rps/keyboards.py
cp tg_api.py    rps/tg_api.py
cp tasks.py     rps/tasks.py
cp admin.py     rps/admin.py
cp apps.py      rps/apps.py
cp urls_app.py  rps/urls.py
cp settings.py  rps_project/settings.py
cp urls_project.py rps_project/urls.py
cp celery.py    rps_project/celery.py

# Add celery to Django __init__.py:
# rps_project/__init__.py
echo "from .celery import app as celery_app
__all__ = ('celery_app',)" > rps_project/__init__.py

# Copy env:
cp .env.example .env
nano .env   # fill in your values
```

---

## 6. Django Setup

```bash
source venv/bin/activate

python manage.py makemigrations rps
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

---

## 7. Register Telegram Webhook

```bash
# Replace TOKEN and DOMAIN:
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{"url":"https://yourdomain.com/bot/webhook/","allowed_updates":["message","callback_query"],"drop_pending_updates":true}'

# Verify:
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo"
```

---

## 8. Gunicorn Service

Create `/etc/systemd/system/rpsbot.service`:

```ini
[Unit]
Description=RPS Bot Gunicorn
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/rpsbot
Environment="PATH=/var/www/rpsbot/venv/bin"
ExecStart=/var/www/rpsbot/venv/bin/gunicorn \
    --workers 4 \
    --worker-class sync \
    --worker-connections 1000 \
    --bind unix:/run/rpsbot.sock \
    --timeout 30 \
    rps_project.wsgi:application
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable rpsbot
sudo systemctl start rpsbot
```

---

## 9. Celery Worker Service

Create `/etc/systemd/system/rpsbot-celery.service`:

```ini
[Unit]
Description=RPS Bot Celery Worker
After=network.target redis.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/rpsbot
Environment="PATH=/var/www/rpsbot/venv/bin"
ExecStart=/var/www/rpsbot/venv/bin/celery \
    -A rps_project worker \
    --loglevel=info \
    --concurrency=8 \
    -Q celery
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable rpsbot-celery
sudo systemctl start rpsbot-celery
```

> **For 5k concurrent users:** Run 2–4 Celery worker processes with `--concurrency=8` each.
> Monitor with `celery -A rps_project inspect active`.

---

## 10. Nginx Config

Create `/etc/nginx/sites-available/rpsbot`:

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    client_max_body_size 10M;

    location /bot/webhook/ {
        proxy_pass         http://unix:/run/rpsbot.sock;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 10s;
    }

    location /admin/ {
        proxy_pass         http://unix:/run/rpsbot.sock;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }

    location /media/ {
        alias /var/www/rpsbot/media/;
    }

    location /static/ {
        alias /var/www/rpsbot/staticfiles/;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/rpsbot /etc/nginx/sites-enabled/
sudo certbot --nginx -d yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
```

---

## 11. Feature Summary

| Feature | Implementation |
|---|---|
| Registration (name + age + optional phone) | FSM: `reg_name` → `reg_age` → `idle` |
| Profile with avatar, TRON wallet, referral link | `profile_menu_kb()` + `edit_*` states |
| Referral bonus ($0.50 each on profile complete) | `check_and_grant_profile_bonus()` |
| RPS game (online + offline) | `_handle_bet_selected()`, `_finish_rps()` |
| Tic-Tac-Toe (online + offline, minimax bot) | `handle_ttt_move()`, `_finish_ttt()` |
| 5-min animated search with cancel/offline | `search_animation_task`, `expire_search_task` |
| Fee refund on search timeout | `expire_search_task` |
| Friends system (bi-directional, $0.10 fee) | `FriendRequest`, `Friendship` models |
| Game invite to friends ($0.20 fee) | `gameinv_*` callbacks |
| Wallet with $1/$5/$10/$20 deposit options | `deposit_amount_kb()` |
| Card receipt deposit | `DepositRequest` model |
| Crypto deposit (screenshot or TxHash) | `CryptoDepositRequest` model |
| Withdrawal (min $15, TRON wallet) | `WithdrawalRequest` model |
| Admin inline buttons (verify/reject/pay/ban) | All `_handle_*_admin()` in views.py |
| Report system (ignore/ban) | `Report` model + admin callbacks |
| Leaderboard with win rate % | `_show_leaderboard()` |
| War game placeholder | "Coming soon" message |
| Balance in integer cents (no float bugs) | `balance_cents` field throughout |
| Celery + Redis for all messages | `send_message_task`, `edit_message_task` |
| PostgreSQL with connection pooling | `CONN_MAX_AGE=60` in settings |

---

## 12. Performance Notes for 5k Concurrent Users

- **Gunicorn**: 4 workers × sync = handles ~2k req/s on a 4-core VPS
- **Celery**: 8 concurrent tasks per worker; run 2 workers = 16 parallel message sends
- **PostgreSQL**: `CONN_MAX_AGE=60` keeps connections warm, reducing overhead
- **Redis**: single-threaded but fast; all Celery brokering stays under 1ms latency
- **Webhook**: Telegram sends one POST per update; Gunicorn handles them in parallel
- **DB indexes**: `chat_id` is `unique=True` (auto-indexed), `status` on `GameMatch`
  → add `db_index=True` to `GameMatch.status` if you have millions of rows

---

## 13. Useful Commands

```bash
# Watch logs:
sudo journalctl -u rpsbot -f
sudo journalctl -u rpsbot-celery -f

# Restart after code changes:
sudo systemctl restart rpsbot rpsbot-celery

# Check active Celery tasks:
celery -A rps_project inspect active

# Django shell:
source venv/bin/activate && python manage.py shell
```