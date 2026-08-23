import os

from .default import *  # pylint: disable=wildcard-import,unused-wildcard-import

BASE_URL = "https://test-analysis-bot.com"

###############################################################################
# Core

SECRET_KEY = os.environ["SECRET_KEY"]

ALLOWED_HOSTS = [
    "0.0.0.0",
    "localhost",
    BASE_URL.removeprefix("https://"),
]

CSRF_TRUSTED_ORIGINS = [BASE_URL]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

###############################################################################
# Caches

REDIS_URL = os.environ["REDIS_URL"]

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

if REDIS_URL.startswith("rediss://"):
    # Heroku Key-Value Store terminates TLS with a self-signed certificate
    CACHES["default"]["OPTIONS"] = {"ssl_cert_reqs": None}

###############################################################################
# Authentication

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]
