"""Rate limits on the four auth endpoints an unauthenticated caller can hit directly.

register, verify_code, sign_in and request_password_reset take no credential to prove who
is asking. verify_code already caps how many wrong codes one record tolerates
(MAX_VERIFICATION_ATTEMPTS in accounts/models.py), but nothing stops a client from calling
any of the four thousands of times a minute: guessing passwords against sign_in, or making
register and request_password_reset send unlimited email. Two scopes rather than one --
sign_in and verify_code are guesses against a secret and can stand a slightly higher rate;
register and request_password_reset each cost an email and are throttled tighter.

Both scopes key their main limit on the *target* of the call -- the email in the request
body -- rather than the caller's IP. A classroom piloto puts ten-plus students behind one
university wifi's single public IP: an IP-keyed limit of "10/min" throttles the eleventh
different student to try to sign in in the same minute, which is exactly the traffic this
is meant to let through. Keying on the email instead means each student's own account gets
its own budget, so one slow or confused student mashing sign_in does not spend the budget
of the other nine.

An IP-keyed limit still runs alongside it (AuthIPRateThrottle, scope "auth-ip"), at a much
higher rate: it is the backstop against the traffic this service should never see at all --
one client hammering many different email addresses from the same address, which the
per-email limit alone would not catch since each address gets its own fresh budget. Both
throttles must pass for a request through; whichever is tighter for a given pattern of
traffic is the one that fires.

DRF keeps throttle history in the default cache (SimpleRateThrottle.cache). The default
LocMemCache is fine for one process, but this deployment runs gunicorn with more than one
worker process (see docker-compose.yml, GUNICORN_WORKERS): a limit enforced by a
per-process cache only ever sees the share of traffic that landed on that one process, so
docker-compose.yml points CACHE_BACKEND at DatabaseCache, shared by every process (see
.env.example).
"""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.settings import api_settings
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle


class _EnvRateThrottle(SimpleRateThrottle):
    """A throttle that re-reads its rate from settings on every request.

    SimpleRateThrottle.THROTTLE_RATES is a class attribute bound to
    api_settings.DEFAULT_THROTTLE_RATES once, at import time, so a rate changed afterwards
    -- an environment variable read into settings before this module ever imports, a
    test's override_settings -- is invisible to it. Looking DEFAULT_THROTTLE_RATES up
    fresh in get_rate keeps both working.
    """

    def get_rate(self) -> str | None:
        return api_settings.DEFAULT_THROTTLE_RATES[self.scope]


class _TargetRateThrottle(_EnvRateThrottle):
    """Keyed on the email in the request body, not the caller's IP. See module docstring."""

    def get_cache_key(self, request: Request, view: object) -> str | None:
        ident = self._target_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}

    def _target_ident(self, request: Request) -> str:
        # Every one of the four views this is used on takes "email" (accounts/serializers.py's
        # EmailSerializer, the common base of all four request shapes). Normalized the same
        # way validated_email() does, so "A@x.com" and "a@x.com " share one budget rather than
        # two. A body with no usable email -- malformed JSON, a missing field -- still has to
        # be throttled by something, so it falls back to the IP: that traffic is refused by
        # the serializer anyway, but refusing it is itself work an attacker can spam for free
        # otherwise.
        data = getattr(request, "data", None)
        email = data.get("email") if isinstance(data, dict) else None
        if isinstance(email, str) and email.strip():
            return f"email:{email.strip().lower()}"
        return f"ip:{self.get_ident(request)}"


class AuthRateThrottle(_TargetRateThrottle):
    """sign_in and verify_code: repeated guesses against a password or a code."""

    scope = "auth"


class AuthEmailRateThrottle(_TargetRateThrottle):
    """register and request_password_reset: each accepted request sends an email."""

    scope = "auth-email"


class AuthIPRateThrottle(AnonRateThrottle):
    """Backstop shared by all four endpoints, keyed on IP. See module docstring.

    Deliberately one scope for all four rather than mirroring 'auth' / 'auth-email': its
    job is to cap how much total auth traffic one address can generate, not to distinguish
    which endpoint it hit.
    """

    scope = "auth-ip"

    def get_rate(self) -> str | None:
        return api_settings.DEFAULT_THROTTLE_RATES[self.scope]
