from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
DEBUG = os.getenv("DEBUG", "False") == "True"

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")

MEDIA_URL  = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rps",
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'rps_project.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ],
    },
}]

WSGI_APPLICATION = 'rps_project.wsgi.application'

# ─── Database (PostgreSQL) ─────────────────────────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE':   'django.db.backends.postgresql',
        'NAME':     os.getenv("DBNAME", "rpsbot"),
        'USER':     os.getenv("DBUSER", "rpsbot"),
        'PASSWORD': os.getenv("DBPASS", ""),
        'HOST':     os.getenv("DBHOST", "localhost"),
        'PORT':     os.getenv("DBPORT", "5432"),
        'CONN_MAX_AGE': 60,          # keep connections alive for 60 s
        'OPTIONS': {
            'connect_timeout': 10,
        },
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ─── Bot tokens / IDs ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "8093967783")
ADMIN_IDS              = os.getenv("ADMIN_IDS", "8093967783")
BOT_USERNAME           = os.getenv("BOT_USERNAME", "")

# Crypto wallet addresses
# Crypto wallet address (TRON / USDT-TRC20 only)
WALLET_USDT_TRC20 = os.getenv("WALLET_USDT_TRC20", "")

# ─── Celery / Redis ────────────────────────────────────────────────────────────
_redis = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_BROKER_URL                  = _redis
CELERY_RESULT_BACKEND              = _redis
CELERY_ACCEPT_CONTENT              = ["json"]
CELERY_TASK_SERIALIZER             = "json"
CELERY_RESULT_SERIALIZER           = "json"
CELERY_TASK_ACKS_LATE              = True
CELERY_WORKER_PREFETCH_MULTIPLIER  = 1
# For 5k concurrent users: run multiple workers
# e.g.  celery -A rps_project worker --concurrency=8 -Q default

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Asia/Tehran'
USE_I18N      = True
USE_TZ        = True

STATIC_URL  = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ─── Security ──────────────────────────────────────────────────────────────────
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if h not in ("127.0.0.1", "localhost")
]
USE_X_FORWARDED_HOST    = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')