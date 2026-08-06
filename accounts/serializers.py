"""Request shapes for the auth endpoints.

These validate *form* only -- that an email looks like an email, that a code is six
digits. Whether the account exists, whether the password is right, whether the code is
still alive: all of that is a cause, and causes are decided in the views so that they can
be returned through ``failures.failure`` rather than as DRF's field-error dictionaries.
"""

from __future__ import annotations

from rest_framework import serializers

from accounts.models import ROLE_CHOICES

# The app's MockAuthService rejects anything shorter as 'weak-password', and its
# registration screen says so before submitting. A different number here would let the
# screen accept a password the server then refuses.
MINIMUM_PASSWORD_LENGTH = 8

CODE_LENGTH = 6


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validated_email(self) -> str:
        return str(self.validated_data["email"]).strip().lower()


class SignInSerializer(EmailSerializer):
    # No min_length here: a short password at sign-in is a wrong password, not a weak one.
    # Reporting 'weak-password' would tell an attacker the length of the real one.
    password = serializers.CharField(trim_whitespace=False)


class RegisterSerializer(EmailSerializer):
    password = serializers.CharField(trim_whitespace=False)
    role = serializers.ChoiceField(choices=[value for value, _ in ROLE_CHOICES])


class VerifyCodeSerializer(EmailSerializer):
    code = serializers.RegexField(rf"^\d{{{CODE_LENGTH}}}$")
