"""Erasing a study's files when the row goes.

``on_delete=CASCADE`` on ``Study.owner`` and ``Analysis.study`` deletes rows; it deletes
nothing on disk. Left alone, removing a study (from the admin, or from the account-deletion
endpoint in ``accounts/views.py``) drops the database record while the original image and
whatever the worker wrote to ``WORK_ROOT`` sit there indefinitely -- nothing else owns them
or ever revisits them. For health data that is not an oversight to leave, it is a leak.

A ``post_delete`` signal, not an override of ``Study.delete()``: the admin's bulk delete
action and the cascade from deleting a ``User`` both go through ``QuerySet.delete()``,
which never calls a single instance's ``delete()``. Signals are the one hook Django fires
either way. The trade-off is that this runs after the row is already gone from the
database, inside the same transaction, so a rollback after this point cannot bring the file
back -- deleting from disk was never going to be transactional either way.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.db.models.signals import post_delete
from django.dispatch import receiver

from studies.models import Study


@receiver(post_delete, sender=Study)
def delete_study_files(sender: type[Study], instance: Study, **kwargs: object) -> None:
    """Remove the original image and the study's work directory from disk.

    The whole per-study media directory goes, not just the image file (``study_image_path``
    puts nothing else there today, but nothing here should have to be updated if that ever
    changes). ``ignore_errors``: the files may already be gone -- a study whose analysis
    already cleaned its own workdir on success (``PipelineRunner.clean_workdir``), or a
    retry of a deletion that partially ran -- and a missing file is not a reason to leave
    the row-less remainder behind.
    """
    if instance.image and instance.image.name:
        study_dir = Path(instance.image.path).parent
        shutil.rmtree(study_dir, ignore_errors=True)
    shutil.rmtree(Path(settings.WORK_ROOT) / str(instance.pk), ignore_errors=True)
