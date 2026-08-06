from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from analysis.models import Analysis


@admin.register(Analysis)
class AnalysisAdmin(ModelAdmin):
    """Read-only. Editing a stored result by hand would put a hand-edited reading on a
    screen with nothing to say it had been touched."""

    list_display = ("study", "status", "failure", "attempts", "requested_at", "completed_at")
    list_filter = ("status", "failure")
    search_fields = ("study__id", "study__anonymous_id")
    readonly_fields = tuple(field.name for field in Analysis._meta.fields)

    def has_add_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False
