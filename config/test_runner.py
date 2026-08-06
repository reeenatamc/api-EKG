"""Test runner.

Its only job is to stop the suite spending its time on PBKDF2. Django's default hasher is
deliberately slow -- that is the whole point of it -- and the auth tests create accounts
and check passwords constantly, which took the suite from a few seconds to half a minute.
A suite that slow stops being run.

MD5 here is not a weakening of anything: it applies to the throwaway test database only,
and the production hasher is exercised by Django's own suite.
"""

from __future__ import annotations

from typing import Any

from django.test.runner import DiscoverRunner


class FastPasswordHasherRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs: Any) -> None:
        import logging

        from django.conf import settings

        settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

        # Several tests deliberately provoke a rejected upload or a crashed
        # interpretation, and the handlers for those log loudly on purpose. Printed
        # tracebacks in a passing suite train you to ignore tracebacks.
        logging.disable(logging.CRITICAL)

        super().setup_test_environment(**kwargs)

    def teardown_test_environment(self, **kwargs: Any) -> None:
        import logging

        logging.disable(logging.NOTSET)
        super().teardown_test_environment(**kwargs)
