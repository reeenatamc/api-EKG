from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from accounts.models import User, VerificationCode

# ``unfold.admin.ModelAdmin`` rather than the stock one, here and in the other two apps.
# Registering INSTALLED_APPS alone themes the shell -- navigation, headers, the index --
# but every change and add form keeps the stock widgets, which reads as a half-finished
# skin rather than a theme.


@admin.register(User)
class UserAdmin(ModelAdmin):
    list_display = ("email", "role", "is_verified", "is_active", "date_joined")
    list_filter = ("role", "is_verified", "is_active")
    search_fields = ("email",)
    ordering = ("-date_joined",)


@admin.register(VerificationCode)
class VerificationCodeAdmin(ModelAdmin):
    """Codes are visible as records, never as codes: ``code_hash`` is not listed."""

    list_display = ("user", "purpose", "created_at", "expires_at", "consumed_at", "attempts")
    list_filter = ("purpose",)
    search_fields = ("user__email",)
