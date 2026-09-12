"""Project-level views that answer to no app in particular.

Just the health check today. It lives here rather than under any of accounts, studies or
analysis because it is not part of a contract any of those apps serve -- it is what Docker's
own healthcheck and an external uptime monitor poll (see README, "Despliegue").
"""

from __future__ import annotations

from django.db import connection
from django.db.utils import OperationalError
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def healthz(request: Request) -> Response:
    """Is this instance able to do anything useful? Nothing here needs a credential.

    Whatever is watching this endpoint -- Docker's own healthcheck, a free external
    monitor -- is not a signed-in user, and it must never itself become a reason the
    service looks unhealthy: hence no auth and no throttling (the two things every other
    endpoint in this project has). The database is the one dependency worth checking,
    because without it nothing else here can do anything either; there is deliberately no
    check of the worker or the pipeline, both of which can be down while uploads and
    logins keep working.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except OperationalError:
        return Response({"status": "error"}, status=503)
    return Response({"status": "ok"})
