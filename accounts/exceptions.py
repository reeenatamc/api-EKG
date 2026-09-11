"""The service-wide DRF exception handler.

Every refusal this API produces answers {"reason": <cause>} -- see accounts/failures.py and
studies/failures.py -- but a handful of exceptions never reach a view to be turned into
one, because DRF raises and answers them itself before a view's own return value matters.
Throttled is the one this service now provokes: DRF's default rendering is a 429 with
{"detail": "Request was throttled. ..."}, free text exactly where the causes-not-messages
contract forbids it. This is the seam where that translation has to happen by hand.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import exception_handler as default_exception_handler

from accounts import failures


def custom_exception_handler(exc: Exception, context: dict) -> Response | None:
    if isinstance(exc, Throttled):
        # Throttling is wired up only on the four unauthenticated accounts views (see
        # @throttle_classes in accounts/views.py), so the app's AuthFailureReason union
        # is the vocabulary to answer from -- and it has no reason for "you are rate
        # limited" (see AuthService.ts in app-EKG). 'unexpected' is used deliberately
        # rather than inventing a reason only this server knows: the vocabulary test
        # requires every reason here to already exist in the app's union, and the app's
        # generic-error copy is a reasonable rendering of "try again shortly".
        return Response({"reason": failures.UNEXPECTED}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    return default_exception_handler(exc, context)
