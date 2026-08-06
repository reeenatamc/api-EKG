from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from studies.models import Study


@admin.register(Study)
class StudyAdmin(ModelAdmin):
    list_display = ("anonymous_id", "owner", "mount", "speed_mm_per_second", "gain_mm_per_millivolt", "received_at")
    list_filter = ("mount",)
    search_fields = ("anonymous_id", "owner__email")
    readonly_fields = ("id", "received_at", "image_width", "image_height")
