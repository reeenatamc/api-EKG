"""Analysis routes.

Nested under the study because an analysis has no identity apart from one: the app holds
a ``studyId`` and never sees an analysis id.
"""

from __future__ import annotations

from django.urls import path

from analysis import views

urlpatterns = [
    path("studies/<uuid:study_id>/analysis/", views.study_analysis, name="study-analysis"),
    path("studies/<uuid:study_id>/signal/", views.study_signal, name="study-signal"),
]
