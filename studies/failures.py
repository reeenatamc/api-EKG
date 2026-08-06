"""The app's ``UploadFailureReason``, and the only way an upload is refused.

Same agreement as ``accounts/failures.py``: causes, not messages. The app's upload queue
reads the cause to decide whether to retry on its own or stop and ask the user, so a
refusal that arrives as prose is a refusal it will retry forever.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

UNAUTHORIZED = "unauthorized"
PAYLOAD_REJECTED = "payload-rejected"
SERVER_ERROR = "server-error"
UNEXPECTED = "unexpected"

# 'network-unreachable' belongs to the app's own adapter: a request that arrived is
# evidence the network worked.

UPLOAD_FAILURE_REASONS = frozenset({UNAUTHORIZED, PAYLOAD_REJECTED, SERVER_ERROR, UNEXPECTED})

_STATUS = {
    UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    PAYLOAD_REJECTED: status.HTTP_400_BAD_REQUEST,
    SERVER_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    UNEXPECTED: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def failure(reason: str, detail: str | None = None) -> Response:
    """Refuse with a cause.

    ``detail`` is for a developer reading a log or a terminal, never for a screen: the app
    ignores it and takes the reason. It carries which field was wrong, which is the
    difference between a five-minute fix and an afternoon of guessing at a 400.
    """
    if reason not in UPLOAD_FAILURE_REASONS:
        raise ValueError(f"{reason!r} is not an UploadFailureReason; see studies/failures.py")
    body: dict[str, object] = {"reason": reason}
    if detail:
        body["detail"] = detail
    return Response(body, status=_STATUS[reason])
