"""GET /healthz/: what Docker's own healthcheck and an external uptime monitor poll.

No credential and no throttle -- see config/views.py for why -- so these tests hit it with
a bare, unauthenticated client and nothing else.
"""

from __future__ import annotations

from unittest.mock import patch

from django.db.utils import OperationalError
from django.test import TestCase
from rest_framework.test import APIClient


class HealthzTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()

    def test_healthy_when_the_database_answers(self) -> None:
        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_no_credential_is_needed(self) -> None:
        # Every other endpoint in this project defaults to IsAuthenticated (see
        # REST_FRAMEWORK in config/settings.py); this is the one a health monitor with no
        # token can still call.
        response = self.client.get("/healthz/")

        self.assertNotEqual(response.status_code, 401)

    def test_unhealthy_when_the_database_does_not_answer(self) -> None:
        with patch("config.views.connection") as mock_connection:
            mock_connection.cursor.side_effect = OperationalError("no connection")
            response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error"})
