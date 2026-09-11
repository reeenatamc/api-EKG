"""The two calls in the app's ``EcgAnalysisService``, on one URL.

POST is ``request``: it enqueues, and answers with the analysis in its initial state.
GET is ``get``: it answers with the current state, and 404 for a study the server does not
know -- which is the app's ``null``.

Neither runs the pipeline. A request that took a minute of CPU to answer would time out on
a phone on mobile data, and the app is built to poll precisely so it does not have to.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from analysis.models import STATUS_FAILED, Analysis
from studies.models import Study


def _study_for(request: Request, study_id: str) -> Study:
    """The caller's study, or 404.

    Filtered by owner, so another user's study is not found rather than forbidden. A 403
    would confirm the id exists, and study ids are the only handle anyone has on a
    clinical image.
    """
    return get_object_or_404(Study, pk=study_id, owner=request.user)


@api_view(["POST", "GET"])
@permission_classes([IsAuthenticated])
def study_analysis(request: Request, study_id: str) -> Response:
    study = _study_for(request, study_id)

    if request.method == "GET":
        analysis = Analysis.objects.filter(study=study).first()
        if analysis is None:
            # Uploaded but never requested. The app's ``get`` is specified to return null
            # for a study the server does not know about, and it does not know about an
            # analysis nobody asked for.
            return Response(status=404)
        return Response(analysis.to_body())

    # POST. Idempotent by design: the app's upload queue retries, and a second request
    # must not start a second run or discard a finished one.
    #
    # A FAILED ANALYSIS IS NEITHER OF THOSE, and requeueing it is what the app's retry
    # button means. Without this, pressing it returned the same failed record and the
    # button did nothing visible: each side was coherent alone and the pair was not.
    analysis, created = Analysis.objects.get_or_create(study=study)

    if not created and analysis.status == STATUS_FAILED:
        analysis.requeue()

    return Response(analysis.to_body(), status=201 if created else 200)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def study_signal(request: Request, study_id: str) -> Response:
    """The digitized trace on its own, for a client that only wants it.

    ``study_analysis`` already carries ``signal`` in its body once one exists -- while
    processing, and on a failure that came after digitization -- so this adds nothing new
    to compute. It exists for a caller that wants the trace without the rest of the
    ``EcgAnalysis`` shape, and 404s for exactly as long as ``study_analysis`` would answer
    a null ``signal``: no analysis at all, or one that has not been digitized yet.
    """
    study = _study_for(request, study_id)
    analysis = Analysis.objects.filter(study=study).first()
    if analysis is None or analysis.signal is None:
        return Response(status=404)
    return Response(analysis.signal)
