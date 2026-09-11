"""Rate limits on the four auth endpoints an unauthenticated caller can hit directly.

register, verify_code, sign_in and request_password_reset take no credential to prove who
is asking. verify_code already caps how many wrong codes one record tolerates
(MAX_VERIFICATION_ATTEMPTS in accounts/models.py), but nothing stops a client from calling
any of the four thousands of times a minute: guessing passwords against sign_in, or making
register and request_password_reset send unlimited email. Two scopes rather than one --
sign_in and verify_code are guesses against a secret and can stand a slightly higher rate;
register and request_password_reset each cost an email and are throttled tighter.

DRF keeps throttle history in the default cache (SimpleRateThrottle.cache). The default
LocMemCache is fine for one process, which is this service's deployment today, but it is
per process: a multi-process deployment needs CACHES pointed at something shared, Redis or
Memcached, or each process enforces the limit on its own share of the traffic rather than
on the whole of it.
"""

from __future__ import annotations

from rest_framework.settings import api_settings
from rest_framework.throttling import AnonRateThrottle


class _EnvRateThrottle(AnonRateThrottle):
    """An AnonRateThrottle that re-reads its rate from settings on every request.

    SimpleRateThrottle.THROTTLE_RATES is a class attribute bound to
    api_settings.DEFAULT_THROTTLE_RATES once, at import time, so a rate changed afterwards
    -- an environment variable read into settings before this module ever imports, a
    test's override_settings -- is invisible to it. Looking DEFAULT_THROTTLE_RATES up
    fresh in get_rate keeps both working.
    """

    def get_rate(self) -> str | None:
        return api_settings.DEFAULT_THROTTLE_RATES[self.scope]


class AuthRateThrottle(_EnvRateThrottle):
    """sign_in and verify_code: repeated guesses against a password or a code."""

    scope = "auth"


class AuthEmailRateThrottle(_EnvRateThrottle):
    """register and request_password_reset: each accepted request sends an email."""

    scope = "auth-email"
