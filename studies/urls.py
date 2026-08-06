from __future__ import annotations

from django.urls import path

from studies import views

urlpatterns = [
    path("", views.upload_study, name="upload-study"),
]
