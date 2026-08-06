"""The study: an ECG image plus what is needed to read it.

WHAT IS NOT HERE IS PART OF THE DESIGN. There is no patient name, no date of birth, no
identifier belonging to a person. ``study.ts`` in the app states the reason: a photograph
of an ECG with a patient's name beside it stops being an anonymous clinical datum and
becomes a medical record, with everything that drags in. The app never asks for one. This
table has nowhere to put one, and that is deliberate -- adding a nullable ``patient_name``
here would be a bigger decision than it looks.

``anonymous_id`` is the user's own case code, which is what replaces the name.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

from studies.mounts import MOUNT_CHOICES, STANDARD_3X4

# The standard print. Anything else is a deliberate choice by whoever ran the ECG, and the
# digitizer assumes these values, so a study that differs needs correcting downstream --
# see analysis/calibration.py.
STANDARD_SPEED_MM_PER_SECOND = 25.0
STANDARD_GAIN_MM_PER_MILLIVOLT = 10.0


def study_image_path(instance: "Study", filename: str) -> str:
    """One directory per study, under the owner.

    The original filename is discarded except for its suffix: it comes from a phone and
    is attacker-controlled, and nothing downstream needs it. The study id is already the
    only name anything refers to.
    """
    suffix = "".join(c for c in filename[filename.rfind(".") :] if c.isalnum() or c == ".")[:8]
    return f"studies/{instance.owner_id}/{instance.id}/original{suffix.lower() or '.jpg'}"


class Study(models.Model):
    """One captured ECG, as it arrived.

    Nothing on this model is derived: it is the upload, verbatim. Everything computed from
    it lives on ``analysis.Analysis``, so that re-running the pipeline never risks
    rewriting the evidence.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="studies")

    # The user's own case code (``formatAnonymousId``: ECG-260729-4K2M). Not unique: two
    # users may reach for the same code, and the app also lets it be edited to match
    # whatever register they keep. Uniqueness is the primary key's job.
    anonymous_id = models.CharField(max_length=24)
    captured_at = models.DateTimeField()

    mount = models.CharField(max_length=16, choices=MOUNT_CHOICES, default=STANDARD_3X4)

    speed_mm_per_second = models.FloatField(default=STANDARD_SPEED_MM_PER_SECOND)
    gain_mm_per_millivolt = models.FloatField(default=STANDARD_GAIN_MM_PER_MILLIVOLT)

    # The four corners of the paper in the pixels of the uploaded image, in the app's
    # fixed order: top-left, top-right, bottom-right, bottom-left. Stored as data rather
    # than applied to the pixels by the client, because correcting perspective means
    # resampling and resampling is where a one-millimetre trace is lost. The server has
    # the image at native resolution and does it there -- see analysis/rectify.py.
    quad = models.JSONField()

    image = models.ImageField(upload_to=study_image_path)
    image_width = models.PositiveIntegerField()
    image_height = models.PositiveIntegerField()

    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["owner", "-received_at"])]
        verbose_name_plural = "studies"

    def __str__(self) -> str:
        return f"{self.anonymous_id} ({self.id})"

    @property
    def has_standard_calibration(self) -> bool:
        return (
            self.speed_mm_per_second == STANDARD_SPEED_MM_PER_SECOND
            and self.gain_mm_per_millivolt == STANDARD_GAIN_MM_PER_MILLIVOLT
        )

    @property
    def quad_points(self) -> list[tuple[float, float]]:
        """The quad as (x, y) pairs, in the app's corner order."""
        return [(float(p["x"]), float(p["y"])) for p in self.quad]

    def receipt(self) -> dict[str, Any]:
        """The app's ``UploadReceipt``: proof that this arrived, and under what id."""
        return {"remoteId": str(self.id), "receivedAt": self.received_at.isoformat()}
