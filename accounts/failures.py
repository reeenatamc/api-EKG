"""The failure vocabulary the app speaks, and the only way this service reports a refusal.

``AuthService.ts`` in app-EKG is explicit about why: the service returns a *cause*, never
text to display, so that no "Error 401" from a server can end up on a screen and so the
wording lives in one place. This module is the server half of that agreement. A view that
returns a message instead of one of these constants has broken the contract, even if the
message is friendlier.

Every constant here appears verbatim in the app's ``AuthFailureReason`` union. Adding one
that is not in that union means the app falls through to 'unexpected' and shows the
generic copy, which is the failure mode this file exists to make hard.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.response import Response

CREDENTIALS_MISMATCH = "credentials-mismatch"
ACCOUNT_NOT_FOUND = "account-not-found"
EMAIL_ALREADY_REGISTERED = "email-already-registered"
WEAK_PASSWORD = "weak-password"
CODE_MISMATCH = "code-mismatch"
CODE_EXPIRED = "code-expired"
UNEXPECTED = "unexpected"

# 'network-unreachable' is in the app's union but is deliberately absent here: it is what
# the app's own adapter reports when the request never arrived. A server cannot answer
# that it is unreachable.

AUTH_FAILURE_REASONS = frozenset(
    {
        CREDENTIALS_MISMATCH,
        ACCOUNT_NOT_FOUND,
        EMAIL_ALREADY_REGISTERED,
        WEAK_PASSWORD,
        CODE_MISMATCH,
        CODE_EXPIRED,
        UNEXPECTED,
    }
)

# HTTP status per cause. The app reads the body, not the code, but a proxy or a log reader
# does read the code, and answering 200 to a refusal would misinform both.
_STATUS = {
    CREDENTIALS_MISMATCH: status.HTTP_401_UNAUTHORIZED,
    ACCOUNT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    EMAIL_ALREADY_REGISTERED: status.HTTP_409_CONFLICT,
    WEAK_PASSWORD: status.HTTP_400_BAD_REQUEST,
    CODE_MISMATCH: status.HTTP_400_BAD_REQUEST,
    CODE_EXPIRED: status.HTTP_410_GONE,
    UNEXPECTED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def failure(reason: str) -> Response:
    """Refuse with a cause.

    Raises on an unknown reason rather than passing it through. A typo would otherwise
    reach the app as an unrecognised string and be rendered as generic copy, hiding the
    real cause behind a shrug -- and it would be found in QA, not in a test.
    """
    if reason not in AUTH_FAILURE_REASONS:
        raise ValueError(f"{reason!r} is not an AuthFailureReason; see accounts/failures.py")
    return Response({"reason": reason}, status=_STATUS[reason])


def session_body(user: Any, token: str) -> dict[str, Any]:
    """The success body for sign-in and verification.

    ``session`` is exactly the app's ``Session``. The token travels beside it rather than
    inside it because ``Session`` has no field for one: the app models what its screens
    need, and no screen needs a credential. Storing it is the adapter's job.
    """
    return {
        "token": token,
        "session": {
            "userId": str(user.id),
            "email": user.email,
            "role": user.role,
        },
    }
