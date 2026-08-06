"""Receiving a study.

One endpoint. It stores what arrived and answers with a receipt; it does not start any
analysis. That split is the app's: ``UploadService.send`` and ``EcgAnalysisService.request``
are separate calls, so a study can be safely in the server's hands before anyone has
decided to spend a minute of CPU reading it.
"""

from __future__ import annotations

import logging

from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from studies import failures
from studies.serializers import StudyUploadSerializer

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def upload_study(request: Request) -> Response:
    """Store an uploaded ECG and return its ``UploadReceipt``.

    Every validation problem is one cause -- 'payload-rejected' -- because that is the one
    the app's queue handles correctly: it stops retrying and surfaces the study, which is
    right for a malformed quad and equally right for a missing field. Splitting it finer
    would give the queue distinctions it has no different behaviour for.
    """
    form = StudyUploadSerializer(data=request.data, context={"request": request})
    if not form.is_valid():
        # The queue will not retry this, so the reason it failed needs to survive in the
        # log; the response carries only a short form of it.
        logger.warning("upload rejected for %s: %s", request.user, form.errors)
        return failures.failure(failures.PAYLOAD_REJECTED, detail=_first_error(form.errors))

    try:
        study = form.save()
    except Exception:
        # An upload the app is entitled to retry: nothing here is the app's fault, and the
        # queue treats 'server-error' as retryable where it treats 'payload-rejected' as
        # final.
        logger.exception("could not store study for %s", request.user)
        return failures.failure(failures.SERVER_ERROR)

    return Response(study.receipt(), status=201)


def _first_error(errors: object, path: str = "") -> str:
    """Flatten DRF's nested error structure to one ``field: message`` line."""
    if isinstance(errors, dict):
        for key, value in errors.items():
            return _first_error(value, f"{path}.{key}" if path else str(key))
    if isinstance(errors, list) and errors:
        return _first_error(errors[0], path)
    return f"{path}: {errors}" if path else str(errors)
