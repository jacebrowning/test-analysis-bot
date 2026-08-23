import os
from pathlib import Path

from django.contrib import messages

import dj_database_url

BASE_DIR = Path(__file__).resolve().parents[2]


###############################################################################
# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # 3rd-party
    "corsheaders",
    "crispy_bootstrap5",
    "crispy_forms",
    "django_extensions",
    "django_tables2",
    "django_user_agents",
    "mozilla_django_oidc",
    # First-party
    "tab.api",
    "tab.core",
    "tab.projects",
    "tab.metrics",
    "tab.releases",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "tab.core.middleware.AuthentikSessionRefresh",
    "django_user_agents.middleware.UserAgentMiddleware",
    "tab.core.middleware.CrawlerPreviewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "tab.core.middleware.ExceptionLoggingMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

###############################################################################
# Logging

LOGGING: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(levelname)s: %(message)s",
        },
        "json": {
            "()": "tab.core.logging.JSONFormatter",
        },
    },
    "filters": {
        "empty_log_filter": {
            "()": "tab.core.logging.EmptyLogFilter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["empty_log_filter"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

###############################################################################
# Databases

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DATABASES = {}
DATABASES["default"] = dj_database_url.config()

###############################################################################
# Caches

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": BASE_DIR / ".cache" / "django",
    }
}

###############################################################################
# Sessions

SESSION_COOKIE_AGE = 30 * 24 * 60 * 60  # 30 days

###############################################################################
# Authentication

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "tab.core.auth.AuthentikOIDCBackend",
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGIN_REDIRECT_URL_FAILURE = "/accounts/login/"

AUTHENTIK_BASE_URL = "https://auth.corp.zoo.dev"
AUTHENTIK_PROVIDER_SLUG = "test-analysis-bot"
OIDC_OP_ISSUER = f"{AUTHENTIK_BASE_URL}/application/o/{AUTHENTIK_PROVIDER_SLUG}/"
OIDC_OP_AUTHORIZATION_ENDPOINT = f"{AUTHENTIK_BASE_URL}/application/o/authorize/"
OIDC_OP_TOKEN_ENDPOINT = f"{AUTHENTIK_BASE_URL}/application/o/token/"
OIDC_OP_USER_ENDPOINT = f"{AUTHENTIK_BASE_URL}/application/o/userinfo/"
OIDC_OP_JWKS_ENDPOINT = (
    f"{AUTHENTIK_BASE_URL}/application/o/{AUTHENTIK_PROVIDER_SLUG}/jwks/"
)
OIDC_RP_CLIENT_ID = os.getenv("OIDC_RP_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = os.getenv("OIDC_RP_CLIENT_SECRET", "")
OIDC_RP_SCOPES = "openid email"
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_TIMEOUT = 10
OIDC_USE_PKCE = True
OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS = 15 * 60
OIDC_EXEMPT_URLS = ["logout"]
OIDC_CALLBACK_CLASS = "tab.core.oidc.AuthentikOIDCCallbackView"

###############################################################################
# Internationalization

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

###############################################################################
# Static files

STATIC_ROOT = BASE_DIR / "staticfiles"

STATIC_URL = "static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

###############################################################################
# Crispy Forms

CRISPY_TEMPLATE_PACK = "bootstrap5"

MESSAGE_TAGS = {
    messages.DEBUG: "alert-secondary",
    messages.INFO: "alert-info",
    messages.SUCCESS: "alert-success",
    messages.WARNING: "alert-warning",
    messages.ERROR: "alert-danger",
}

###############################################################################
# Email

POSTMARK_API_KEY = os.getenv("POSTMARK_API_KEY")

EMAIL_HOST = "smtp.postmarkapp.com"
EMAIL_HOST_USER = POSTMARK_API_KEY
EMAIL_HOST_PASSWORD = POSTMARK_API_KEY
EMAIL_PORT = 587
EMAIL_USE_TLS = True

DEFAULT_FROM_EMAIL = "Test Analysis Bot <no-reply@zoo-corp.dev>"
SERVER_EMAIL = DEFAULT_FROM_EMAIL

###############################################################################
# API Limits

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
