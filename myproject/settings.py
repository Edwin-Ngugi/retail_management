import os
from datetime import timedelta
from pathlib import Path
import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables early
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get('SECRET_KEY', 'LHi48kwW-AUcTjgstdxSJt2896Mea1qPO9lMMKe24jpNr4oYGKeW6EUtSi4uFUyNKrY')

DEBUG = os.environ.get('DEBUG', 'True').lower() in ('true', '1', 't')

# Host Security
ALLOWED_HOSTS_ENV = os.environ.get(
    'ALLOWED_HOSTS',
    'api.melvexbusinesssolutions.com,melvexbusinesssolutions.com,www.melvexbusinesssolutions.com,.onrender.com,localhost,127.0.0.1,0.0.0.0'
)
ALLOWED_HOSTS = [host.strip() for host in ALLOWED_HOSTS_ENV.split(',') if host.strip()]


INSTALLED_APPS = [
    'daphne',  # Must be first for Channels/ASGI
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt',
    'stocks',
    'analytics',
    'apis',
    'channels',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'myproject.urls'


# CORS & CSRF Settings
CORS_ALLOWED_ORIGINS_ENV = os.environ.get(
    'CORS_ALLOWED_ORIGINS',
    'https://melvexbusinesssolutions.com,https://www.melvexbusinesssolutions.com,http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000,http://127.0.0.1:8000'
)
CORS_ALLOWED_ORIGINS = [origin.strip() for origin in CORS_ALLOWED_ORIGINS_ENV.split(',') if origin.strip()]

CSRF_TRUSTED_ORIGINS_ENV = os.environ.get(
    'CSRF_TRUSTED_ORIGINS',
    'https://melvexbusinesssolutions.com,https://www.melvexbusinesssolutions.com,http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000,http://127.0.0.1:8000'
)
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in CSRF_TRUSTED_ORIGINS_ENV.split(',') if origin.strip()]

# Allows any local development server port or ngrok tunnel
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^http://localhost:\d+$",
    r"^http://127\.0\.0\.1:\d+$",
    r"^https://.*\.ngrok-free\.dev$",
    r"^https://.*\.ngrok-free\.app$",
]

CORS_ALLOW_CREDENTIALS = True


# Celery Configuration
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "generate-monthly-stock-snapshot": {
        "task": "analytics.tasks.create_monthly_stock_snapshot",
        "schedule": crontab(0, 0, day_of_month="1"),  
    },
}

TEMPLATES = [
    {
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
    },
]

WSGI_APPLICATION = 'myproject.wsgi.application'
ASGI_APPLICATION = 'myproject.asgi.application'


# Database Configuration
# Database Configuration
DATABASE_URL_VAL = os.getenv(
    'DATABASE_URL',
    'postgres://postgres:Thaguru%408876@localhost:5432/retail_management'
)

DATABASES = {
    'default': dj_database_url.config(
        default=DATABASE_URL_VAL,
        conn_max_age=0 if DEBUG else 600,
        ssl_require=False
    )
}

# Grant SQLite extra busy timeout to resolve concurrent read/write locks under ASGI
if DATABASE_URL_VAL.startswith("sqlite"):
    DATABASES['default']['OPTIONS'] = {
        'timeout': 20,
    }


# Channels & Redis Configuration
REDIS_URL = os.environ.get('REDIS_URL')

if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [REDIS_URL],
            },
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer"
        }
    }

# Daphne Response Timeout Adjustment
DAPHNE_SERVER_RESPONSE_TIMEOUT = 120


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True


STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=24),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Application Host URLs
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:5173' if DEBUG else 'https://melvexbusinesssolutions.com')
BACKEND_URL = os.environ.get('BACKEND_URL', 'http://127.0.0.1:8000' if DEBUG else 'https://api.melvexbusinesssolutions.com')


# Email Configuration
MANAGEMENT_EMAILS = [
    email.strip() for email in os.environ.get('MANAGEMENT_EMAILS', 'engugi922@gmail.com').split(',') if email.strip()
]

DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'engugi922@gmail.com')
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True').lower() in ('true', '1', 't')
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', 'engugi922@gmail.com')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_TIMEOUT = 10


# Production HTTPS Security Enhancements
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'True').lower() in ('true', '1', 't')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    
    SECURE_HSTS_SECONDS = 31536000  # 1 Year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True