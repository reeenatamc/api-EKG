"""URL map.

The paths are grouped by the app-side contract they serve, so that a change in
``AuthService`` or ``EcgAnalysisService`` has one obvious place to land.
"""

from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from config.views import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="healthz"),
    path("auth/", include("accounts.urls")),
    path("studies/", include("studies.urls")),
    path("", include("analysis.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
