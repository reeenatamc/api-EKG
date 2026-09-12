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

from django.core.exceptions import ImproperlyConfigured

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


# False by default: a server started without deciding is a server that is not accidentally
# handing out tracebacks. The local workflow this repository is cloned for sets DEBUG=1 in
# its own .env (see .env.example), so nothing changes for it.
DEBUG = _flag("DEBUG", default=False)

if DEBUG:
    # The dev fallback is deliberately an obvious placeholder rather than a plausible-looking
    # random string: a key that looks generated is a key someone ships.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")
else:
    # Outside DEBUG there is no fallback at all. A production server that boots on a
    # missing key is a server signing sessions and tokens with whatever the placeholder
    # above happens to be, which is public because this file is.
    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY:
        raise ImproperlyConfigured("SECRET_KEY is required outside DEBUG. Set it in the environment.")

# A phone on the LAN reaches this server at whatever address the router handed the laptop
# today, and Expo's own tunnel adds another. Enumerating those in DEBUG means editing a
# file every time the network changes, so DEBUG accepts any host. Outside DEBUG the list
# is honoured strictly and an empty one refuses every request, which is the safe way to
# fail when someone deploys without configuring it.
ALLOWED_HOSTS = ["*"] if DEBUG else [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

# The admin's login form and every other session-cookie POST carry CSRF, checked against
# the Origin/Referer of the request. Caddy terminates TLS in front of this service (see
# docker-compose.yml), so the domain the browser actually used has to be listed here or its
# own admin login refuses itself. Empty by default, like ALLOWED_HOSTS: DEBUG never needs
# it because nothing behind runserver serves the admin over HTTPS to a browser that checks.
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

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

# SQLite is the development default -- one file, nothing to run alongside runserver. The
# piloto's Postgres is a separate opt-in rather than the default so that cloning this repo
# and running it locally needs no database server: set DATABASE_ENGINE=postgresql and the
# POSTGRES_* variables below to point it at one (see docker-compose.yml, service `db`).
# Analysis.claim_next's compare-and-swap already works the same way against both engines.
if os.environ.get("DATABASE_ENGINE") == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "ekg"),
            "USER": os.environ.get("POSTGRES_USER", "ekg"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "db"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
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

LANGUAGE_CODE = "es"
TIME_ZONE = "America/Guayaquil"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Only unfold's own assets land here (collectstatic, run once at container start -- see
# docker/entrypoint-api.sh); nothing in this project ships its own static files. Configurable
# because the piloto's Caddy serves this directory straight off a volume rather than through
# Django, and the volume has to be mounted at whatever path this setting names.
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "media/"
# Configurable for the same reason WORK_ROOT below is: the piloto mounts this as a Docker
# volume so the original images survive a container being recreated.
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))

# Where the pipeline writes its CSVs and interpretation JSON, one directory per study.
# Separate from MEDIA_ROOT because these are derived artifacts: deleting the whole tree
# costs a re-run, while deleting MEDIA_ROOT loses the only copy of the original image.
WORK_ROOT = Path(os.environ.get("ECG_WORK_ROOT", BASE_DIR / "work"))

# --- Running behind Caddy ----------------------------------------------------------
#
# TLS terminates at Caddy (docker-compose.yml); this service only ever sees plain HTTP on
# the Docker network between them. Without SECURE_PROXY_SSL_HEADER, Django believes every
# request is insecure and request.is_secure() -- which the cookie flags below and the admin
# both read -- is wrong for all of them. Caddy sets X-Forwarded-Proto itself and this
# service is never reachable except through it (see Caddyfile), so the header cannot be
# forged by anything outside the compose network.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # HSTS opt-in rather than always-on: it is a promise a domain has to be able to keep
    # (every future deploy on it must serve HTTPS), and the piloto's domain is not settled
    # until week 1 (see the deployment report). 0 -- the header is omitted -- until then.
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))

# A photo of an ECG from a modern phone is 4-15 MB, and the app deliberately sends the
# native-resolution crop rather than a resampled one, because resampling is where a
# millimetre-wide trace is lost. Django's 2.5 MB default would reject exactly the images
# this service exists to read.
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 40 * 1024 * 1024

# The limits studies/serializers.py actually enforces. The setting above only decides
# memory vs a temp file, it is not a cap: on its own, serializers.ImageField accepts a
# 300 MB PNG or a small file that decompresses to gigabytes ("decompression bomb"). Phone
# photos are 4-15 MB, so 25 MB leaves headroom without accepting an unbounded upload.
STUDY_MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("STUDY_MAX_UPLOAD_SIZE_BYTES", str(25 * 1024 * 1024)))

# Pillow refuses to open an image above roughly twice Image.MAX_IMAGE_PIXELS (about 178
# megapixels) with its own DecompressionBombError, but that guard is a blunt backstop
# meant for arbitrary Pillow callers, not this API's contract -- it would surface as an
# unhandled exception rather than 'payload-rejected', and it says nothing between its
# warning threshold (~89 MP) and that error threshold. This is the limit that is actually
# checked, and checked first, so it is the one that speaks.
STUDY_MAX_UPLOAD_PIXELS = int(os.environ.get("STUDY_MAX_UPLOAD_PIXELS", str(50_000_000)))

# DRF's per-IP throttling (accounts/throttling.py) keeps its request history in this cache.
# LocMemCache is per-process, which is exactly right for runserver and for the test suite,
# but gunicorn with more than one worker (docker-compose.yml) would then enforce each rate
# against only whatever share of traffic that one process happened to see. DatabaseCache
# needs no extra service to run -- only `manage.py createcachetable` once, for the table
# CACHE_LOCATION names (docker/entrypoint-api.sh does this on every start; it is a no-op
# once the table exists).
CACHES = {
    "default": {
        "BACKEND": os.environ.get("CACHE_BACKEND", "django.core.cache.backends.locmem.LocMemCache"),
        "LOCATION": os.environ.get("CACHE_LOCATION", "django_cache"),
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "UNAUTHENTICATED_USER": None,
    # Throttled renders as {"detail": "..."} by default, which is exactly the free text
    # the causes-not-messages contract forbids. See accounts/exceptions.py.
    "EXCEPTION_HANDLER": "accounts.exceptions.custom_exception_handler",
    # Only accounts/throttling.py's two scopes are used, applied per-view with
    # @throttle_classes on the four unauthenticated auth endpoints -- nothing else here
    # takes unauthenticated traffic worth limiting. Rates are overridable per deployment;
    # 'auth' (sign_in, verify_code) tolerates more traffic than 'auth-email' (register,
    # request_password_reset), which each send an email. DRF keeps throttle history in
    # the default cache: LocMemCache is fine for one process, but a multi-process
    # deployment needs CACHES pointed at something shared (Redis, Memcached) or each
    # process only ever sees its own share of the traffic.
    "DEFAULT_THROTTLE_RATES": {
        "auth": os.environ.get("AUTH_THROTTLE_RATE", "10/min"),
        "auth-email": os.environ.get("AUTH_EMAIL_THROTTLE_RATE", "5/min"),
    },
}

# Verification codes go to the console in development. Wiring real SMTP is a deployment
# decision, not a code change: set EMAIL_BACKEND and the EMAIL_HOST_* variables. Written
# for Resend (see .env.example), but any SMTP provider that speaks TLS on 587 works the
# same way -- Brevo is the plan B the deployment report names if Resend's mail is rejected.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@ekg.local")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _flag("EMAIL_USE_TLS", default=True)

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

# ecg-pipeline resolves its per-class thresholds from $ECGFOUNDER_THRESHOLDS_DIR, read once
# at import time by interpret_ecg.py -- the same reason ECG_PIPELINE_HOME is handled above,
# before anything else here imports ecg_pipeline. Unset by default: interpret_csv then falls
# back to ecg-pipeline's own configs/thresholds/, which is the normal case (see that
# repository's README). This is a directory override only, never an opt-in: whether
# thresholds are applied at all is decided in ecg-pipeline, not here.
ECG_THRESHOLDS_DIR = os.environ.get("ECG_THRESHOLDS_DIR")
if ECG_THRESHOLDS_DIR:
    os.environ["ECGFOUNDER_THRESHOLDS_DIR"] = ECG_THRESHOLDS_DIR

# Seconds a worker waits between polls of an empty queue.
ECG_WORKER_POLL_SECONDS = float(os.environ.get("ECG_WORKER_POLL_SECONDS", "2.0"))

# Keep every study's intermediate files under WORK_ROOT after a successful run. Off by
# default: a ready analysis carries all it serves in its own row, and the files are about
# 9 MB per study. Failed runs keep theirs regardless, for inspection.
ECG_KEEP_WORK = _flag("ECG_KEEP_WORK")

# --- Logging -------------------------------------------------------------------------
#
# To stdout, always: a container has no log file to rotate, and `docker compose logs` (or
# whatever the server's log collector reads) is stdout. INFO is what run_worker and the
# views already log at (a study's outcome, a rejected upload); nothing here logs a request
# body, which is where a health datum -- or a password -- would otherwise end up.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        # Django's own request log already includes the method, path and status line;
        # nothing here duplicates it or adds the body.
        "django": {
            "handlers": ["console"],
            "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
