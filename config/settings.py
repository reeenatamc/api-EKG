"""Django settings for api-EKG.

This service is the HTTP layer in front of ``ecg-pipeline``. It owns accounts, uploads and
the job queue; it owns no clinical logic. Everything that decides what an ECG means lives
in the pipeline, and everything that decides how a result is worded lives in the app.

Two settings below are load-bearing rather than boilerplate, and both are about the phone:
``ALLOWED_HOSTS`` in DEBUG, and the upload size limits. See their comments.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read a .env file into ``os.environ`` without taking a dependency for it.

    Values already in the environment win: an explicit ``SECRET_KEY=... python manage.py``
    has to beat a stale line in a file nobody remembers editing.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(BASE_DIR / ".env")


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


DEBUG = _flag("DEBUG", default=True)

# The dev fallback is deliberately an obvious placeholder rather than a plausible-looking
# random string: a key that looks generated is a key someone ships.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")

# A phone on the LAN reaches this server at whatever address the router handed the laptop
# today, and Expo's own tunnel adds another. Enumerating those in DEBUG means editing a
# file every time the network changes, so DEBUG accepts any host. Outside DEBUG the list
# is honoured strictly and an empty one refuses every request, which is the safe way to
# fail when someone deploys without configuring it.
ALLOWED_HOSTS = ["*"] if DEBUG else [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

INSTALLED_APPS = [
    # Before django.contrib.admin, and the order is load-bearing rather than tidy:
    # unfold works by overriding the admin's own templates, and Django resolves a
    # template by walking INSTALLED_APPS in order and taking the first match. Listed
    # after the admin, unfold is never reached and the admin renders stock -- with no
    # error to explain why.
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "accounts",
    "studies",
    "analysis",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_USER_MODEL = "accounts.User"

# Password strength is enforced in the serialiser, not here. Django's validators return
# prose, and this API returns causes: see accounts/failures.py.
AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []

# --- Admin theme ------------------------------------------------------------------
#
# The shades are RGB triplets as space-separated strings, not hex. That is unfold's
# format because it composes them as `rgb(var(--color-primary-500) / <alpha>)` in CSS,
# which needs the channels unpacked; a hex value there yields an invalid colour and the
# element renders transparent rather than failing.
#
# The scale is Tailwind's `red`, kept whole. Unfold uses the light end for backgrounds
# and the dark end for text on them, so supplying only the mid shades gives an admin
# with unreadable contrast in exactly the places that matter -- and 950 is what the dark
# theme leans on.
UNFOLD = {
    "SITE_TITLE": "api-EKG",
    "SITE_HEADER": "api-EKG",
    "SITE_SUBHEADER": "Digitización e interpretación de ECG",
    # Material Symbols name, shown beside the header.
    "SITE_SYMBOL": "monitor_heart",
    "SHOW_HISTORY": True,
    "COLORS": {
        "primary": {
            "50": "254 242 242",
            "100": "254 226 226",
            "200": "254 202 202",
            "300": "252 165 165",
            "400": "248 113 113",
            "500": "239 68 68",
            "600": "220 38 38",
            "700": "185 28 28",
            "800": "153 27 27",
            "900": "127 29 29",
            "950": "69 10 10",
        },
    },
}

TEST_RUNNER = "config.test_runner.FastPasswordHasherRunner"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Where the pipeline writes its CSVs and interpretation JSON, one directory per study.
# Separate from MEDIA_ROOT because these are derived artifacts: deleting the whole tree
# costs a re-run, while deleting MEDIA_ROOT loses the only copy of the original image.
WORK_ROOT = Path(os.environ.get("ECG_WORK_ROOT", BASE_DIR / "work"))

# A photo of an ECG from a modern phone is 4-15 MB, and the app deliberately sends the
# native-resolution crop rather than a resampled one, because resampling is where a
# millimetre-wide trace is lost. Django's 2.5 MB default would reject exactly the images
# this service exists to read.
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 40 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "UNAUTHENTICATED_USER": None,
}

# Verification codes go to the console in development. Wiring real SMTP is a deployment
# decision, not a code change: set EMAIL_BACKEND and the EMAIL_HOST_* variables.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@ekg.local")

# React Native on a device sends no Origin header and is unaffected by any of this; the
# `expo start --web` target runs in a browser and is not. Allowing every origin is
# acceptable while DEBUG because the alternative is chasing Expo's rotating dev ports.
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# --- Pipeline configuration -------------------------------------------------------
#
# Defaults mirror the ecg-pipeline CLI's own. 'rhythm' is the pathway to trust for rhythm
# and rate; see that repository's README before changing it.

# ecg-pipeline is normally installed into this interpreter (`pip install -e ../ecg-pipeline
# --no-deps`; see README). Setting ECG_PIPELINE_HOME puts its checkout on sys.path instead,
# which is the escape hatch for a machine where installing it is not an option. It is a
# fallback and not the recommended route: an installed package is found the same way from
# every working directory, and a sys.path entry is one more thing that can point at the
# wrong checkout.
_pipeline_home = os.environ.get("ECG_PIPELINE_HOME")
if _pipeline_home:
    import sys

    if _pipeline_home not in sys.path:
        sys.path.insert(0, _pipeline_home)

ECG_PATHWAY = os.environ.get("ECG_PATHWAY", "rhythm")
ECG_DEVICE = os.environ.get("ECG_DEVICE", "cpu")
ECG_TOP_K = int(os.environ.get("ECG_TOP_K", "10"))

# Seconds a worker waits between polls of an empty queue.
ECG_WORKER_POLL_SECONDS = float(os.environ.get("ECG_WORKER_POLL_SECONDS", "2.0"))

# Keep every study's intermediate files under WORK_ROOT after a successful run. Off by
# default: a ready analysis carries all it serves in its own row, and the files are about
# 9 MB per study. Failed runs keep theirs regardless, for inspection.
ECG_KEEP_WORK = _flag("ECG_KEEP_WORK")
