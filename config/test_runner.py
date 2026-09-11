"""Test runner.

Two adjustments the whole suite needs, neither of which is a weakening of anything in
production: both apply to the throwaway test process only.

Its original job is to stop the suite spending its time on PBKDF2. Django's default hasher
is deliberately slow -- that is the whole point of it -- and the auth tests create accounts
and check passwords constantly, which took the suite from a few seconds to half a minute.
A suite that slow stops being run. MD5 here applies to the test database only; the
production hasher is exercised by Django's own suite.

The other is the auth throttle rates (accounts/throttling.py). Those are a per-IP limit
meant for the real world, and the suite calls the endpoints they guard from one address --
the test client's -- far more than the production rate allows within the minute a full run
takes, which would throttle tests that have nothing to do with rate limiting. Turned off by
default here, the same way DRF itself spells "unlimited" (a rate of None short-circuits
SimpleRateThrottle.allow_request); tests that exercise throttling turn it back on with
override_settings, at a rate low enough to reach in a handful of requests -- see
ThrottlingTests in tests/test_auth.py.
"""

from __future__ import annotations

from typing import Any

from django.test.runner import DiscoverRunner


class FastPasswordHasherRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs: Any) -> None:
        import logging

        from django.conf import settings

        settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

        settings.REST_FRAMEWORK = {
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {"auth": None, "auth-email": None},
        }
        # A plain assignment does not fire the setting_changed signal that override_settings
        # sends, and DRF's api_settings caches DEFAULT_THROTTLE_RATES the first time anything
        # reads it -- which checks/urls.py's import chain does well before this point. Without
        # the explicit reload, every test would still see the un-overridden production rate.
        from rest_framework.settings import api_settings

        api_settings.reload()

        # Several tests deliberately provoke a rejected upload or a crashed
        # interpretation, and the handlers for those log loudly on purpose. Printed
        # tracebacks in a passing suite train you to ignore tracebacks.
        logging.disable(logging.CRITICAL)

        super().setup_test_environment(**kwargs)

    def teardown_test_environment(self, **kwargs: Any) -> None:
        import logging

        logging.disable(logging.NOTSET)
        super().teardown_test_environment(**kwargs)
