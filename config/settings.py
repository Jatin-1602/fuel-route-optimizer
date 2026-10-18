"""Django settings for the Fuel Route Optimizer project.

All environment-specific values are read from environment variables (or a local
``.env`` file) via django-environ. See ``.env.example`` for the full list.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    environ.Env.read_env(_env_file)

# --- Core ---------------------------------------------------------------------

DEBUG: bool = env.bool("DEBUG", default=False)

# A throwaway key is acceptable for local development only; production must set one.
SECRET_KEY: str = (
    env.str("SECRET_KEY", default="django-insecure-local-dev-only-change-me")
    if DEBUG
    else env.str("SECRET_KEY")
)

ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "drf_spectacular",
    # Local
    "apps.stations",
    "apps.routing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
ASGI_APPLICATION = "config.asgi.application"

# --- Database -----------------------------------------------------------------

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}

# --- Cache --------------------------------------------------------------------
# File-based by default so cached routes survive `runserver` restarts (handy for demos).
# Override with e.g. CACHE_URL=locmemcache:// or redis://localhost:6379/1

CACHES = {
    "default": env.cache("CACHE_URL", default=f"filecache://{BASE_DIR / '.cache'}"),
}

ROUTE_CACHE_TTL_SECONDS: int = env.int("ROUTE_CACHE_TTL_SECONDS", default=60 * 60 * 24)
GEOCODE_CACHE_TTL_SECONDS: int = env.int("GEOCODE_CACHE_TTL_SECONDS", default=60 * 60 * 24 * 30)

# --- OpenRouteService ---------------------------------------------------------

ORS_API_KEY: str = env.str("ORS_API_KEY", default="")
# The exact base URL is confirmed in Phase 3 (api.openrouteservice.org -> api.heigit.org).
ORS_BASE_URL: str = env.str("ORS_BASE_URL", default="https://api.openrouteservice.org")
ORS_CONNECT_TIMEOUT_SECONDS: float = env.float("ORS_CONNECT_TIMEOUT_SECONDS", default=5.0)
ORS_READ_TIMEOUT_SECONDS: float = env.float("ORS_READ_TIMEOUT_SECONDS", default=20.0)

# --- Django REST Framework / OpenAPI ------------------------------------------

REST_FRAMEWORK = {
    # Public, read-only API: no authentication, so no session/CSRF handling either.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fuel Route Optimizer API",
    "DESCRIPTION": (
        "Plans a driving route between two US locations and picks the most cost-effective "
        "fuel stops given a maximum vehicle range and fuel efficiency."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Auth / i18n / static -----------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Logging ------------------------------------------------------------------

LOG_LEVEL: str = env.str("LOG_LEVEL", default="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "apps": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
