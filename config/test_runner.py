"""Test runner.

Three adjustments the whole suite needs, none of which is a weakening of anything in
production: all three apply to the throwaway test process only.

Its original job is to stop the suite spending its time on PBKDF2. Django's default hasher
is deliberately slow -- that is the whole point of it -- and the auth tests create accounts
and check passwords constantly, which took the suite from a few seconds to half a minute.
A suite that slow stops being run. MD5 here applies to the test database only; the
production hasher is exercised by Django's own suite.

The second is the auth throttle rates (accounts/throttling.py). Those are a per-IP limit
meant for the real world, and the suite calls the endpoints they guard from one address --
the test client's -- far more than the production rate allows within the minute a full run
takes, which would throttle tests that have nothing to do with rate limiting. Turned off by
default here, the same way DRF itself spells "unlimited" (a rate of None short-circuits
SimpleRateThrottle.allow_request); tests that exercise throttling turn it back on with
override_settings, at a rate low enough to reach in a handful of requests -- see
ThrottlingTests in tests/test_auth.py.

The third keeps the suite from writing into the repository. A test that creates a Study
writes a real file under MEDIA_ROOT, and one that runs the pipeline writes a per-study
directory under WORK_ROOT; either setting left pointed at the checkout means a run leaves
files behind that nothing deletes, because nothing owns them -- not a fixture, not a Study
row, nothing a later `git status` or a cleanup pass has a hook to find. Individual test
classes used to override MEDIA_ROOT for themselves, and it is easy to add a new one that
creates a Study and forgets to, which is exactly what happened. Redirecting both roots here
instead, for the whole run, means forgetting is no longer possible: the checkout's media/
and work/ are not on the path a test could reach even if it tried.
"""

from __future__ import annotations

import shutil
import tempfile
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

        # One directory for the whole run rather than one per class: cheap to make, and it
        # means a test that forgets to isolate itself still cannot reach the checkout.
        self._media_root = tempfile.mkdtemp(prefix="api-ekg-test-media-")
        self._work_root = tempfile.mkdtemp(prefix="api-ekg-test-work-")
        settings.MEDIA_ROOT = self._media_root
        settings.WORK_ROOT = self._work_root

        super().setup_test_environment(**kwargs)

    def teardown_test_environment(self, **kwargs: Any) -> None:
        import logging

        logging.disable(logging.NOTSET)

        # ignore_errors: a test may have already removed files or the directory itself
        # (WorkdirCleanupTests does exactly that), and a leftover temp directory the OS
        # will eventually reclaim is not worth failing a passing run over.
        shutil.rmtree(self._media_root, ignore_errors=True)
        shutil.rmtree(self._work_root, ignore_errors=True)

        super().teardown_test_environment(**kwargs)
