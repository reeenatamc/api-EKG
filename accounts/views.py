"""The six operations in the app's ``AuthService``, one view each.

Every refusal leaves through ``failures.failure``. Nothing here returns prose.

On account enumeration
----------------------
The app's ``AuthFailureReason`` distinguishes 'account-not-found' from
'credentials-mismatch', which lets the sign-in screen offer to create an account instead
of just saying no. That is a real product decision and it is implemented as specified --
but it does mean sign-in tells an anonymous caller whether an address is registered.

``ACCOUNT_ENUMERATION_IS_ACCEPTABLE`` below turns it off in one line: unknown addresses
then answer 'credentials-mismatch' like everything else, and the app falls back to its
generic copy without any change on its side. Password reset never leaks regardless -- it
answers the same way for an address that exists and one that does not, which is why its
success carries no information.

On rate limiting
----------------
None of these four views requires a credential to be called, so all four are throttled
(``accounts/throttling.py``): a wrong-code guess already dies on its own after
``MAX_VERIFICATION_ATTEMPTS``, but nothing else here stopped a client from hammering
``sign_in`` with passwords or making ``register``/``request_password_reset`` send
unlimited email. A throttled request is turned into a cause, not DRF's default free-text
429, in ``accounts/exceptions.py``.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from accounts import failures
from accounts.models import PURPOSE_PASSWORD_RESET, PURPOSE_REGISTRATION, VERIFICATION_WINDOW_SECONDS, VerificationCode
from accounts.serializers import (
    MINIMUM_PASSWORD_LENGTH,
    EmailSerializer,
    RegisterSerializer,
    SignInSerializer,
    VerifyCodeSerializer,
)
from accounts.throttling import AuthEmailRateThrottle, AuthRateThrottle

logger = logging.getLogger(__name__)

User = get_user_model()

# See the module docstring. True keeps the app's contract as written.
ACCOUNT_ENUMERATION_IS_ACCEPTABLE = True


def _pending_verification(email: str) -> Response:
    """The app's ``PendingVerification``. Identical for every caller, by design."""
    return Response({"email": email, "expiresInSeconds": VERIFICATION_WINDOW_SECONDS})


def _send_code(email: str, code: str, purpose: str) -> None:
    """Deliver a code.

    In development EMAIL_BACKEND is the console backend, so this prints to the terminal
    running the server -- which is how you get the code while testing on a phone.
    """
    subject = "Your EKG Reader code"
    if purpose == PURPOSE_PASSWORD_RESET:
        subject = "Reset your EKG Reader password"
    send_mail(
        subject=subject,
        message=f"Your code is {code}. It expires in {VERIFICATION_WINDOW_SECONDS // 60} minutes.",
        from_email=None,
        recipient_list=[email],
        fail_silently=False,
    )


def _issue_and_send(user: User, purpose: str) -> None:
    record, code = VerificationCode.issue(user, purpose)
    try:
        _send_code(user.email, code, purpose)
    except Exception:
        # An address that cannot be delivered to must not leave a live code behind: the
        # user has no way to read it and would sit on a screen counting down to nothing.
        record.consume()
        raise


def _token_for(user: User) -> str:
    token, _ = Token.objects.get_or_create(user=user)
    return token.key


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthEmailRateThrottle])
def register(request: Request) -> Response:
    """Create the account and leave verification pending.

    An address that is already registered *and verified* is a conflict. One that exists
    but was never verified is not: it is somebody who closed the app before typing the
    code, and the useful answer is a fresh code rather than a dead end they cannot clear.
    """
    form = RegisterSerializer(data=request.data)
    if not form.is_valid():
        return failures.failure(failures.UNEXPECTED)

    email = form.validated_email()
    password = form.validated_data["password"]
    role = form.validated_data["role"]

    if len(password) < MINIMUM_PASSWORD_LENGTH:
        return failures.failure(failures.WEAK_PASSWORD)

    existing = User.objects.filter(email=email).first()
    if existing is not None:
        if existing.is_verified:
            return failures.failure(failures.EMAIL_ALREADY_REGISTERED)
        # Unverified: adopt the new password and role, then re-send. The account is not
        # yet anyone's, so there is nothing to protect by refusing.
        existing.set_password(password)
        existing.role = role
        existing.save(update_fields=["password", "role"])
        _issue_and_send(existing, PURPOSE_REGISTRATION)
        return _pending_verification(email)

    try:
        with transaction.atomic():
            user = User.objects.create_user(email=email, password=password, role=role, is_verified=False)
    except IntegrityError:
        # Two registrations for the same address, racing. The unique index is the
        # arbiter; whoever lost reports the conflict.
        return failures.failure(failures.EMAIL_ALREADY_REGISTERED)

    _issue_and_send(user, PURPOSE_REGISTRATION)
    return _pending_verification(email)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthRateThrottle])
def verify_code(request: Request) -> Response:
    """Redeem a code and open the session.

    Serves both codes the app can be holding. ``AuthService`` has a single ``verifyCode``
    and no way to submit a new password, so a password reset ends here too: the code
    proves control of the address and the answer is a session. Whoever adds a
    change-password screen should add the endpoint for it rather than overload this one.
    """
    form = VerifyCodeSerializer(data=request.data)
    if not form.is_valid():
        # A malformed code is a wrong code. Saying 'unexpected' here would make a typo
        # look like a server problem.
        return failures.failure(failures.CODE_MISMATCH)

    email = form.validated_email()
    user = User.objects.filter(email=email).first()
    if user is None:
        return failures.failure(failures.ACCOUNT_NOT_FOUND)

    record = VerificationCode.latest_for(user)
    if record is None or record.is_burnt:
        return failures.failure(failures.CODE_EXPIRED)

    if not record.matches(form.validated_data["code"]):
        record.record_failed_attempt()
        # The attempt that exhausts the budget kills the code, and saying so is the honest
        # answer: another guess at this code cannot succeed.
        if record.is_burnt:
            return failures.failure(failures.CODE_EXPIRED)
        return failures.failure(failures.CODE_MISMATCH)

    record.consume()
    if not user.is_verified:
        user.is_verified = True
        user.save(update_fields=["is_verified"])

    return Response(failures.session_body(user, _token_for(user)))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthRateThrottle])
def sign_in(request: Request) -> Response:
    form = SignInSerializer(data=request.data)
    if not form.is_valid():
        return failures.failure(failures.CREDENTIALS_MISMATCH)

    email = form.validated_email()
    user = User.objects.filter(email=email).first()

    if user is None:
        if ACCOUNT_ENUMERATION_IS_ACCEPTABLE:
            return failures.failure(failures.ACCOUNT_NOT_FOUND)
        return failures.failure(failures.CREDENTIALS_MISMATCH)

    if not user.check_password(form.validated_data["password"]) or not user.is_active:
        return failures.failure(failures.CREDENTIALS_MISMATCH)

    if not user.is_verified:
        # The account exists but was never confirmed. Sending a fresh code turns a refusal
        # into the next step, and the app already has a screen for exactly this state.
        _issue_and_send(user, PURPOSE_REGISTRATION)
        return failures.failure(failures.CODE_EXPIRED)

    return Response(failures.session_body(user, _token_for(user)))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthEmailRateThrottle])
def request_password_reset(request: Request) -> Response:
    """Send a reset code, and answer the same way whether or not the account exists.

    This one does not enumerate even when sign-in does. Password reset is the endpoint an
    attacker probes precisely because it takes an address alone, and the app's own mock
    also succeeds unconditionally -- so matching that costs nothing on the client.
    """
    form = EmailSerializer(data=request.data)
    if not form.is_valid():
        return failures.failure(failures.UNEXPECTED)

    email = form.validated_email()
    user = User.objects.filter(email=email).first()
    if user is not None:
        _issue_and_send(user, PURPOSE_PASSWORD_RESET)

    return _pending_verification(email)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def sign_out(request: Request) -> Response:
    """Drop the token.

    The app clears its own storage regardless; this makes the token stop working, so a
    device that is handed to someone else does not carry a live credential.
    """
    Token.objects.filter(user=request.user).delete()
    return Response(status=204)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def current_session(request: Request) -> Response:
    """Answer who the bearer of this token is.

    ``restoreSession`` reads the app's own secure storage, so this is not on the startup
    path. It exists so the adapter can find out that a stored session is no longer valid
    -- after a sign-out on another device, say -- instead of discovering it on the first
    upload.
    """
    return Response({"session": failures.session_body(request.user, "")["session"]})


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_account(request: Request) -> Response:
    """Erase the account: every study, every analysis, and every file either wrote.

    Google Play requires that an app offering in-app account creation also offer deletion
    from inside the app (see README, "Despliegue"). There is no confirmation step here
    beyond the bearer token itself -- the same proof ``sign_out`` already trusts -- because
    this service has no second factor to ask for; a password re-prompt before calling this
    is the app's call to make, not this endpoint's.

    ``request.user.delete()`` cascades through ``Study.owner`` and ``Analysis.study``
    (both ``on_delete=CASCADE``), and ``studies/signals.py`` removes each study's image and
    work directory on the way out -- the same cleanup a single study's own deletion gets.
    The app has no cause for anything more specific than 'unexpected' here, so a failure
    partway through -- inside the transaction, so nothing is left half-deleted -- reports
    that rather than inventing a reason only this endpoint would ever produce.
    """
    try:
        with transaction.atomic():
            request.user.delete()
    except Exception:
        logger.exception("could not delete account %s", request.user.pk)
        return failures.failure(failures.UNEXPECTED)
    return Response(status=204)
