"""The auth endpoints, checked against the app's ``AuthService``.

The assertions are mostly about the *reason* returned rather than the status code, because
the reason is the contract: the app switches on it and renders copy from it. A refusal
with the right status and the wrong reason is a bug that shows the user the wrong sentence.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts import failures
from accounts.models import (
    MAX_VERIFICATION_ATTEMPTS,
    PURPOSE_PASSWORD_RESET,
    PURPOSE_REGISTRATION,
    VERIFICATION_WINDOW_SECONDS,
    VerificationCode,
)
from studies.models import Study

User = get_user_model()

# A quad shape is enough to satisfy the JSONField at the ORM level; only the upload
# serializer validates its geometry, and these tests create studies directly.
SQUARE_QUAD = [{"x": 0.0, "y": 0.0}, {"x": 200.0, "y": 0.0}, {"x": 200.0, "y": 150.0}, {"x": 0.0, "y": 150.0}]


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (200, 150), (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


GOOD_PASSWORD = "correct horse battery"


def with_throttle_rate(rate: str, ip_rate: str = "10000/min") -> dict:
    """A REST_FRAMEWORK override that carries every other key as-is and replaces the two
    per-account auth scopes, so a test does not have to fire off a real minute of traffic
    to reach the limit it wants to check. ``ip_rate`` defaults high enough that the IP
    backstop (accounts/throttling.py's AuthIPRateThrottle, scope 'auth-ip') never fires by
    accident in a test aimed at the per-account scopes -- pass it explicitly to test the
    backstop itself.
    """
    return {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {"auth": rate, "auth-email": rate, "auth-ip": ip_rate},
    }


def code_from_outbox() -> str:
    """The six digits that were emailed. The plaintext exists nowhere else by design."""
    body = mail.outbox[-1].body
    return next(word for word in body.replace(".", " ").split() if word.isdigit() and len(word) == 6)


class RegistrationTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()

    def register(self, email: str = "a@example.com", password: str = GOOD_PASSWORD, role: str = "professional"):
        return self.client.post("/auth/register/", {"email": email, "password": password, "role": role}, format="json")

    def test_registration_returns_a_pending_verification(self) -> None:
        response = self.register()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"email": "a@example.com", "expiresInSeconds": VERIFICATION_WINDOW_SECONDS})

    def test_the_window_matches_the_one_the_app_counts_down(self) -> None:
        # The registration screen shows a countdown seeded from this number. A server
        # window shorter than the app's would expire codes while the screen still showed
        # time on the clock.
        self.assertEqual(VERIFICATION_WINDOW_SECONDS, 600)

    def test_registration_emails_a_code(self) -> None:
        self.register()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(len(code_from_outbox()), 6)

    def test_the_account_cannot_sign_in_before_it_is_verified(self) -> None:
        self.register()

        response = self.client.post(
            "/auth/sign-in/", {"email": "a@example.com", "password": GOOD_PASSWORD}, format="json"
        )

        self.assertEqual(response.json()["reason"], failures.CODE_EXPIRED)

    def test_a_short_password_is_weak_not_a_mismatch(self) -> None:
        response = self.register(password="short")

        self.assertEqual(response.json()["reason"], failures.WEAK_PASSWORD)

    def test_a_verified_address_cannot_be_registered_twice(self) -> None:
        self.register()
        User.objects.filter(email="a@example.com").update(is_verified=True)

        response = self.register()

        self.assertEqual(response.json()["reason"], failures.EMAIL_ALREADY_REGISTERED)

    def test_an_unverified_address_gets_a_fresh_code_instead_of_a_dead_end(self) -> None:
        # Somebody who closed the app before typing the code. Refusing would leave them
        # with an address they can neither use nor re-register.
        self.register()
        first = code_from_outbox()

        response = self.register()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertNotEqual(code_from_outbox(), first)

    def test_the_role_chosen_at_registration_survives_into_the_session(self) -> None:
        self.register(role="student")

        response = self.client.post(
            "/auth/verify/", {"email": "a@example.com", "code": code_from_outbox()}, format="json"
        )

        self.assertEqual(response.json()["session"]["role"], "student")


class VerificationTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.client.post(
            "/auth/register/",
            {"email": "a@example.com", "password": GOOD_PASSWORD, "role": "professional"},
            format="json",
        )
        self.user = User.objects.get(email="a@example.com")

    def verify(self, code: str):
        return self.client.post("/auth/verify/", {"email": "a@example.com", "code": code}, format="json")

    def test_the_right_code_opens_a_session(self) -> None:
        response = self.verify(code_from_outbox())

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["session"]["email"], "a@example.com")
        self.assertEqual(body["session"]["userId"], str(self.user.id))
        self.assertTrue(body["token"])

    def test_the_session_body_carries_exactly_the_apps_session_fields(self) -> None:
        # Session in AuthService.ts is {userId, email, role}. An extra field here would be
        # a field the app's type does not have and its storage would silently drop.
        session = self.verify(code_from_outbox()).json()["session"]

        self.assertEqual(set(session), {"userId", "email", "role"})

    def test_a_wrong_code_is_a_mismatch(self) -> None:
        self.assertEqual(self.verify("000000").json()["reason"], failures.CODE_MISMATCH)

    def test_an_expired_code_is_expired_not_mismatched(self) -> None:
        VerificationCode.objects.filter(user=self.user).update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertEqual(self.verify(code_from_outbox()).json()["reason"], failures.CODE_EXPIRED)

    def test_a_code_dies_after_too_many_guesses(self) -> None:
        for _ in range(MAX_VERIFICATION_ATTEMPTS - 1):
            self.assertEqual(self.verify("000000").json()["reason"], failures.CODE_MISMATCH)

        # The guess that exhausts the budget reports the code as gone, because it is: no
        # further guess at it can succeed.
        self.assertEqual(self.verify("000000").json()["reason"], failures.CODE_EXPIRED)

        # And the real code no longer works either. Without this, the limit would only
        # slow an attacker down rather than stop them.
        self.assertEqual(self.verify(code_from_outbox()).json()["reason"], failures.CODE_EXPIRED)

    def test_a_code_cannot_be_used_twice(self) -> None:
        code = code_from_outbox()
        self.verify(code)

        self.assertEqual(self.verify(code).json()["reason"], failures.CODE_EXPIRED)

    def test_issuing_a_code_kills_the_previous_one(self) -> None:
        # Two live codes would double the guessing surface, so issuing one consumes the
        # last. The old code is reported as a mismatch rather than expired, and that is the
        # accurate answer: a code is live, and this is not it.
        first = code_from_outbox()
        _, second = VerificationCode.issue(self.user, PURPOSE_REGISTRATION)

        self.assertEqual(self.verify(first).json()["reason"], failures.CODE_MISMATCH)
        self.assertEqual(self.verify(second).status_code, 200)

    def test_the_superseded_code_is_consumed_in_the_database(self) -> None:
        VerificationCode.issue(self.user, PURPOSE_REGISTRATION)

        superseded = VerificationCode.objects.filter(user=self.user).order_by("created_at").first()

        self.assertIsNotNone(superseded.consumed_at)
        self.assertTrue(superseded.is_burnt)

    def test_a_malformed_code_is_a_mismatch_not_a_server_problem(self) -> None:
        self.assertEqual(self.verify("abc").json()["reason"], failures.CODE_MISMATCH)

    def test_verifying_an_unknown_address_says_so(self) -> None:
        response = self.client.post("/auth/verify/", {"email": "nobody@example.com", "code": "123456"}, format="json")

        self.assertEqual(response.json()["reason"], failures.ACCOUNT_NOT_FOUND)


class SignInTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="a@example.com", password=GOOD_PASSWORD, role="professional", is_verified=True
        )

    def sign_in(self, email: str = "a@example.com", password: str = GOOD_PASSWORD):
        return self.client.post("/auth/sign-in/", {"email": email, "password": password}, format="json")

    def test_correct_credentials_open_a_session(self) -> None:
        response = self.sign_in()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["userId"], str(self.user.id))

    def test_the_address_is_matched_case_insensitively(self) -> None:
        # Phone keyboards capitalise the first letter of a field. An address that fails to
        # match because of it reads to the user as a forgotten password.
        self.assertEqual(self.sign_in(email="A@Example.com").status_code, 200)

    def test_a_wrong_password_is_a_mismatch(self) -> None:
        self.assertEqual(self.sign_in(password="wrong password").json()["reason"], failures.CREDENTIALS_MISMATCH)

    def test_an_unknown_address_is_not_found(self) -> None:
        # The app's union distinguishes these so the screen can offer to register instead.
        self.assertEqual(self.sign_in(email="nobody@example.com").json()["reason"], failures.ACCOUNT_NOT_FOUND)

    def test_a_short_wrong_password_is_a_mismatch_not_a_weak_password(self) -> None:
        # 'weak-password' here would tell an attacker the real password is longer than
        # what they tried.
        self.assertEqual(self.sign_in(password="x").json()["reason"], failures.CREDENTIALS_MISMATCH)

    def test_an_inactive_account_cannot_sign_in(self) -> None:
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assertEqual(self.sign_in().json()["reason"], failures.CREDENTIALS_MISMATCH)


class PasswordResetTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="a@example.com", password=GOOD_PASSWORD, role="student", is_verified=True
        )

    def request_reset(self, email: str):
        return self.client.post("/auth/password-reset/", {"email": email}, format="json")

    def test_a_reset_emails_a_code(self) -> None:
        response = self.request_reset("a@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(VerificationCode.objects.filter(user=self.user, purpose=PURPOSE_PASSWORD_RESET).exists())

    def test_an_unknown_address_gets_the_same_answer_and_no_email(self) -> None:
        # This endpoint takes an address alone, which makes it the one an attacker probes
        # to enumerate accounts. Its answer must carry no information.
        known = self.request_reset("a@example.com")
        mail.outbox.clear()
        unknown = self.request_reset("nobody@example.com")

        self.assertEqual(unknown.status_code, known.status_code)
        self.assertEqual(unknown.json(), {"email": "nobody@example.com", "expiresInSeconds": 600})
        self.assertEqual(len(mail.outbox), 0)

    def test_a_reset_code_is_redeemed_through_the_same_verify_call(self) -> None:
        # AuthService has one verifyCode and no method for submitting a new password, so a
        # reset code has to be redeemable there or it cannot be redeemed at all.
        self.request_reset("a@example.com")

        response = self.client.post(
            "/auth/verify/", {"email": "a@example.com", "code": code_from_outbox()}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["role"], "student")


class SessionTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="a@example.com", password=GOOD_PASSWORD, role="professional", is_verified=True
        )
        self.token = self.client.post(
            "/auth/sign-in/", {"email": "a@example.com", "password": GOOD_PASSWORD}, format="json"
        ).json()["token"]

    def test_the_token_identifies_the_session(self) -> None:
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")

        response = self.client.get("/auth/session/")

        self.assertEqual(response.json()["session"]["email"], "a@example.com")

    def test_signing_out_stops_the_token_working(self) -> None:
        # The app clears its own storage regardless; this is what protects a device that
        # was handed to somebody else.
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")
        self.client.post("/auth/sign-out/")

        self.assertEqual(self.client.get("/auth/session/").status_code, 401)

    def test_no_token_is_refused(self) -> None:
        self.assertEqual(self.client.get("/auth/session/").status_code, 401)


class ThrottlingTests(TestCase):
    """sign_in, verify_code, register and request_password_reset take no credential to
    gate on, so each is throttled (accounts/throttling.py): mainly by the email in the
    request body, plus a much higher IP-keyed backstop shared by all four. The rate is
    overridden down to a handful of requests a minute so a test does not have to fire off
    sixty seconds of real traffic to reach it.
    """

    def setUp(self) -> None:
        self.client = APIClient()
        User.objects.create_user(email="a@example.com", password=GOOD_PASSWORD, role="professional", is_verified=True)
        # DRF keeps throttle history in the default cache. Every test in this class shares
        # one key (the same email, or the test client's one IP), so a leftover entry from
        # one test would make the next test's very first request already throttled.
        cache.clear()

    def tearDown(self) -> None:
        cache.clear()

    @override_settings(REST_FRAMEWORK=with_throttle_rate("10/min"))
    def test_the_eleventh_sign_in_in_a_minute_is_throttled(self) -> None:
        for _ in range(10):
            response = self.client.post(
                "/auth/sign-in/", {"email": "a@example.com", "password": "wrong"}, format="json"
            )
            self.assertEqual(response.status_code, 401)

        response = self.client.post("/auth/sign-in/", {"email": "a@example.com", "password": "wrong"}, format="json")

        # A cause, not DRF's default free-text 429: see accounts/exceptions.py. There is
        # no reason in the app's union for "you are rate limited" (AuthService.ts), so this
        # is 'unexpected' rather than a reason invented only for the server's own use.
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"reason": failures.UNEXPECTED})

    @override_settings(REST_FRAMEWORK=with_throttle_rate("2/min"))
    def test_sign_in_and_verify_code_share_one_budget(self) -> None:
        # Both draw from the 'auth' scope (accounts/throttling.py): an attacker gains
        # nothing by alternating between guessing a password and guessing a code.
        self.client.post("/auth/sign-in/", {"email": "a@example.com", "password": "wrong"}, format="json")
        self.client.post("/auth/verify/", {"email": "a@example.com", "code": "000000"}, format="json")

        response = self.client.post("/auth/sign-in/", {"email": "a@example.com", "password": "wrong"}, format="json")

        self.assertEqual(response.status_code, 429)

    @override_settings(REST_FRAMEWORK=with_throttle_rate("2/min"))
    def test_register_is_throttled(self) -> None:
        # Keyed on the email in the body (accounts/throttling.py), so all three calls
        # here have to target the same address to share one budget -- a different address
        # each time, as a real classroom's ten distinct students would send, must not
        # collide (see test_ten_accounts_from_the_same_ip_can_sign_in_and_register_in_one_minute).
        for _ in range(2):
            self.client.post(
                "/auth/register/",
                {"email": "new@example.com", "password": GOOD_PASSWORD, "role": "professional"},
                format="json",
            )

        response = self.client.post(
            "/auth/register/",
            {"email": "new@example.com", "password": GOOD_PASSWORD, "role": "professional"},
            format="json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"reason": failures.UNEXPECTED})

    @override_settings(REST_FRAMEWORK=with_throttle_rate("2/min"))
    def test_request_password_reset_is_throttled(self) -> None:
        for _ in range(2):
            self.client.post("/auth/password-reset/", {"email": "a@example.com"}, format="json")

        response = self.client.post("/auth/password-reset/", {"email": "a@example.com"}, format="json")

        self.assertEqual(response.status_code, 429)

    @override_settings(REST_FRAMEWORK=with_throttle_rate("2/min"))
    def test_register_and_password_reset_share_the_other_budget(self) -> None:
        # Both draw from 'auth-email' rather than 'auth': each accepted call here sends an
        # email, which is the resource being protected, and it is a separate budget from
        # sign_in/verify_code so guessing a password cannot exhaust a legitimate
        # registration's headroom. Keyed on the email in the body, so it is the same
        # target account's budget the two endpoints have to share here.
        self.client.post(
            "/auth/register/",
            {"email": "a@example.com", "password": GOOD_PASSWORD, "role": "professional"},
            format="json",
        )
        self.client.post("/auth/password-reset/", {"email": "a@example.com"}, format="json")

        response = self.client.post("/auth/password-reset/", {"email": "a@example.com"}, format="json")

        self.assertEqual(response.status_code, 429)

    def test_ten_accounts_from_the_same_ip_can_sign_in_and_register_in_one_minute(self) -> None:
        # The classroom scenario the per-account keying exists for: ten-plus students
        # behind one wifi's single public address, each spending only their own budget.
        # No @override_settings here -- this checks the rates the piloto actually ships
        # with (accounts/throttling.py, config/settings.py), not an inflated test rate.
        for i in range(10):
            User.objects.create_user(email=f"student{i}@example.com", password=GOOD_PASSWORD, is_verified=True)

        for i in range(10):
            response = self.client.post(
                "/auth/sign-in/", {"email": f"student{i}@example.com", "password": GOOD_PASSWORD}, format="json"
            )
            self.assertEqual(response.status_code, 200)

        for i in range(10):
            response = self.client.post(
                "/auth/register/",
                {"email": f"new-student{i}@example.com", "password": GOOD_PASSWORD, "role": "student"},
                format="json",
            )
            self.assertEqual(response.status_code, 200)

    @override_settings(REST_FRAMEWORK=with_throttle_rate("2/min"))
    def test_throttling_one_account_does_not_spend_anothers_budget(self) -> None:
        # The per-email key is what makes the classroom case above work: exhausting one
        # account's budget must not touch a second account sharing the same IP.
        User.objects.create_user(email="c@example.com", password=GOOD_PASSWORD, is_verified=True)
        for _ in range(2):
            self.client.post("/auth/sign-in/", {"email": "a@example.com", "password": "wrong"}, format="json")
        exhausted = self.client.post("/auth/sign-in/", {"email": "a@example.com", "password": "wrong"}, format="json")
        self.assertEqual(exhausted.status_code, 429)

        response = self.client.post(
            "/auth/sign-in/", {"email": "c@example.com", "password": GOOD_PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 200)

    @override_settings(REST_FRAMEWORK=with_throttle_rate("1000/min", ip_rate="3/min"))
    def test_the_ip_backstop_still_catches_abuse_across_many_accounts(self) -> None:
        # The per-account budget alone would never catch this: guessing against a fresh
        # address each time gets a fresh budget every time. AuthIPRateThrottle (scope
        # 'auth-ip') is the one that still stops it, since it is keyed on the caller's
        # address rather than the target account.
        for i in range(3):
            self.client.post("/auth/sign-in/", {"email": f"unknown{i}@example.com", "password": "wrong"}, format="json")

        response = self.client.post(
            "/auth/sign-in/", {"email": "yet-another@example.com", "password": "wrong"}, format="json"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"reason": failures.UNEXPECTED})


class FailureVocabularyTests(TestCase):
    """The guardrail on the vocabulary itself."""

    def test_every_reason_this_service_can_return_is_one_the_app_knows(self) -> None:
        # Transcribed from AuthFailureReason in app-EKG's src/auth/AuthService.ts.
        app_side = {
            "credentials-mismatch",
            "account-not-found",
            "email-already-registered",
            "weak-password",
            "code-mismatch",
            "code-expired",
            "network-unreachable",
            "unexpected",
        }

        self.assertTrue(failures.AUTH_FAILURE_REASONS <= app_side)

    def test_an_invented_reason_raises_rather_than_reaching_the_app(self) -> None:
        # A typo that shipped would render as generic copy and hide the real cause.
        with self.assertRaises(ValueError):
            failures.failure("not-a-real-reason")


class AccountDeletionTests(TestCase):
    """DELETE /auth/account/: Google Play requires that an app offering account creation
    also let a user erase it from inside the app (see README, "Despliegue").
    """

    def setUp(self) -> None:
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="a@example.com", password=GOOD_PASSWORD, role="professional", is_verified=True
        )
        self.token = Token.objects.create(user=self.user).key
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token}")

    def test_deleting_the_account_removes_the_user(self) -> None:
        response = self.client.delete("/auth/account/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_the_token_stops_working_afterwards(self) -> None:
        self.client.delete("/auth/account/")

        self.assertEqual(self.client.get("/auth/session/").status_code, 401)

    def test_an_anonymous_caller_is_refused(self) -> None:
        self.client.credentials()

        self.assertEqual(self.client.delete("/auth/account/").status_code, 401)

    def test_deleting_the_account_takes_its_studies_and_files_with_it(self) -> None:
        # Study.owner cascades from User, and studies/signals.py is what actually removes
        # the image from disk -- the same cleanup one study's own deletion gets.
        study = Study.objects.create(
            owner=self.user,
            anonymous_id="ECG-1",
            captured_at=timezone.now(),
            quad=SQUARE_QUAD,
            image=SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
            image_width=200,
            image_height=150,
        )
        image_path = Path(study.image.path)
        self.assertTrue(image_path.exists())

        self.client.delete("/auth/account/")

        self.assertFalse(Study.objects.filter(pk=study.pk).exists())
        self.assertFalse(image_path.exists())

    def test_a_failure_answers_a_cause_the_app_knows_and_keeps_the_account(self) -> None:
        with patch.object(User, "delete", side_effect=RuntimeError("boom")):
            response = self.client.delete("/auth/account/")

        self.assertEqual(response.json()["reason"], failures.UNEXPECTED)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
