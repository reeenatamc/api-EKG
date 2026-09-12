from __future__ import annotations

from django.apps import AppConfig


class StudiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "studies"
    verbose_name = "Estudios"

    def ready(self) -> None:
        # Connects the post_delete receiver that removes a study's image and work
        # directory from disk. Imported here, not at module scope, per Django's own
        # guidance: apps are not fully loaded yet when this module is first imported.
        from studies import signals  # noqa: F401
