"""Accounts and the codes that verify them.

The user model is email-first because the app has no concept of a username: every screen
in ``app-EKG`` asks for an email address. Django's stock ``User`` would force a username
field that nothing fills and that would quietly become a second identity.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

# Mirrors the app's ``UserRole``. The choice is made at registration and conditions what
# the user sees afterwards, so it belongs on the account rather than in a profile table.
ROLE_PROFESSIONAL = "professional"
ROLE_STUDENT = "student"
ROLE_CHOICES = [(ROLE_PROFESSIONAL, "Professional"), (ROLE_STUDENT, "Student")]

# Matches VERIFICATION_WINDOW_SECONDS in the app's MockAuthService, which is what the
# registration screen counts down. A shorter window here would expire codes while the
# screen still showed time remaining.
VERIFICATION_WINDOW_SECONDS = 600

# A six-digit code has a million combinations; over a ten-minute window that is brute
# forceable at a few hundred requests a second. Attempts are therefore counted and the
# code dies on the fifth wrong one, which reduces the odds to negligible without adding a
# lockout that a user could trigger on themselves by fat-fingering twice.
MAX_VERIFICATION_ATTEMPTS = 5

PURPOSE_REGISTRATION = "registration"
PURPOSE_PASSWORD_RESET = "password-reset"
PURPOSE_CHOICES = [(PURPOSE_REGISTRATION, "Registration"), (PURPOSE_PASSWORD_RESET, "Password reset")]


class UserManager(BaseUserManager):
    """Creates users keyed by a normalised email address."""

    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra: object) -> "User":
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: object) -> "User":
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: object) -> "User":
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_verified", True)
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """An account.

    The primary key is a UUID rather than a sequence because it is handed to the client as
    ``Session.userId``. A sequential id would tell anyone holding one how many accounts
    exist and let them address the others.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_PROFESSIONAL)

    # False between registering and entering the code. Such an account can hold a pending
    # verification but cannot sign in, so an address someone else typed by mistake never
    # becomes a usable account.
    is_verified = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return self.email


class VerificationCode(models.Model):
    """A one-time code sent to an address, for registration or a password reset.

    The code is stored hashed. It is short-lived and single-use, so plaintext would be
    defensible -- but a hash costs one function call and means a copy of the database is
    not a list of live codes for every account mid-registration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_codes")
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "purpose", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.purpose} code for {self.user.email}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_burnt(self) -> bool:
        """Spent, timed out, or guessed at too many times. All three mean 'code-expired'."""
        return self.consumed_at is not None or self.is_expired or self.attempts >= MAX_VERIFICATION_ATTEMPTS

    def matches(self, code: str) -> bool:
        return check_password(code, self.code_hash)

    def consume(self) -> None:
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])

    def record_failed_attempt(self) -> None:
        self.attempts += 1
        self.save(update_fields=["attempts"])

    @classmethod
    def issue(cls, user: User, purpose: str) -> tuple["VerificationCode", str]:
        """Mint a code for ``user``, invalidating any earlier one for the same purpose.

        Earlier codes are consumed rather than left alive: two valid codes at once doubles
        the guessing surface, and a user who asked for a new one is looking at the new one.

        Returns the record and the plaintext code, which is the only moment the plaintext
        exists. Send it and let it go.
        """
        cls.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(consumed_at=timezone.now())

        # secrets, not random: this is a credential. randbelow is uniform over the range,
        # which a modulo of a larger random number would not be.
        code = f"{secrets.randbelow(1_000_000):06d}"
        record = cls.objects.create(
            user=user,
            purpose=purpose,
            code_hash=make_password(code),
            expires_at=timezone.now() + timedelta(seconds=VERIFICATION_WINDOW_SECONDS),
        )
        return record, code

    @classmethod
    def latest_for(cls, user: User, purpose: str | None = None) -> "VerificationCode | None":
        """The most recent code for ``user``, of any purpose unless one is named.

        Verification is purpose-blind on the way in, and that is forced by the app: its
        ``AuthService`` has one ``verifyCode`` and no method for setting a new password, so
        a reset code is redeemed through the same call as a registration code. Filtering
        by purpose here would make reset codes unredeemable. Since ``issue`` invalidates
        the previous code of its own purpose, the newest code is always the one the user
        is looking at.
        """
        codes = cls.objects.filter(user=user)
        if purpose is not None:
            codes = codes.filter(purpose=purpose)

        # The live code wins a tie on ``created_at``, and ties do happen: ``issue`` consumes
        # the previous code and mints the next one in the same call, so the two timestamps
        # are separated only by a password hash. Under the test runner's MD5 hasher, and on
        # any platform whose clock is coarser than the work between those two writes, they
        # land on the same tick. Ordering by ``-created_at`` alone then picks whichever row
        # the database offers first, and when that is the superseded one the caller is told
        # ``code-expired`` about a code that is merely wrong. The primary key is a UUID, so
        # it cannot break the tie: it carries no order.
        return codes.order_by(
            "-created_at", models.F("consumed_at").desc(nulls_first=True)
        ).first()
