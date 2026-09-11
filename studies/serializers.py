"""Validation of an upload.

The quad checks are the substantial ones. Everything downstream trusts those four points:
``analysis.rectify`` builds a homography from them, and a homography from a degenerate or
self-crossing quadrilateral does not fail -- it produces a mirrored, folded, or wildly
stretched image that still looks like an ECG and still digitizes into a plausible-looking
trace. There is no later stage that catches it. So it is caught here.

Two more checks exist for the same reason a bouncer checks a bag before checking the
invitation: ``serializers.ImageField`` alone accepts a file of any size and any pixel
count, and ``STUDY_MAX_UPLOAD_SIZE_BYTES``/``STUDY_MAX_UPLOAD_PIXELS`` (config/settings.py)
refuse a payload that is too large, or too densely packed with pixels, before anything
about the quad or the calibration is even looked at.
"""

from __future__ import annotations

import json
import math
from typing import Any

from django.conf import settings
from rest_framework import serializers

from studies.models import Study
from studies.mounts import MOUNT_IDS

# Generous bounds rather than the three values the app currently offers, so that a future
# app version with another speed on the menu does not need a backend release. What is
# rejected is nonsense: zero, negative, infinite, or off by orders of magnitude.
MIN_SPEED_MM_PER_SECOND, MAX_SPEED_MM_PER_SECOND = 1.0, 1000.0
MIN_GAIN_MM_PER_MILLIVOLT, MAX_GAIN_MM_PER_MILLIVOLT = 0.5, 200.0

# A corner may land a hair outside the image through rounding in the app's own clamping.
# Beyond this it is not rounding, it is a quad that belongs to a different image.
QUAD_BOUNDS_TOLERANCE_PX = 2.0

# The crop must be most of what was uploaded. The app sends the bounding rectangle of the
# quad, so a quad covering a small fraction of it means the two do not describe the same
# capture -- most likely a stale quad sent with a new photograph.
MIN_QUAD_AREA_FRACTION = 0.2


def _finite(value: Any) -> float:
    """A coordinate that arithmetic can be done on.

    ``json.loads`` accepts the literals ``Infinity`` and ``NaN``, and DRF's FloatField
    passes them through. Either one turns the homography into a matrix of NaNs and the
    rectified image into a blank rectangle, several steps from here.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise serializers.ValidationError(f"{value!r} is not a number")
    if not math.isfinite(number):
        raise serializers.ValidationError(f"{value!r} is not a finite number")
    return number


def _image_size(uploaded: Any) -> tuple[int, int] | None:
    """The uploaded file's real pixel dimensions, as Pillow read them.

    DRF's ImageField has already opened and verified the file at this point and left the
    Pillow image on the upload as ``.image``. Returns None if that is not there, which
    means something other than an ImageField produced this value.
    """
    image = getattr(uploaded, "image", None)
    size = getattr(image, "size", None)
    if not size or len(size) != 2:
        return None
    return int(size[0]), int(size[1])


def _shoelace_area(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _is_simple_quad(points: list[tuple[float, float]]) -> bool:
    """True when the four corners form a convex, non-self-crossing quadrilateral.

    The test is that every turn goes the same way. A bow-tie -- two corners dragged across
    each other, which ``quad.ts`` calls out as the case that breaks the crop screen --
    reverses one of them. Sign, not winding: image coordinates run y-down, so either
    overall sign is valid as long as it is consistent.
    """
    signs = []
    for index in range(4):
        ax, ay = points[index]
        bx, by = points[(index + 1) % 4]
        cx, cy = points[(index + 2) % 4]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if cross == 0:
            return False  # three corners in a line: no quadrilateral to speak of
        signs.append(cross > 0)
    return all(signs) or not any(signs)


class CalibrationSerializer(serializers.Serializer):
    speedMmPerSecond = serializers.FloatField(min_value=MIN_SPEED_MM_PER_SECOND, max_value=MAX_SPEED_MM_PER_SECOND)
    gainMmPerMillivolt = serializers.FloatField(
        min_value=MIN_GAIN_MM_PER_MILLIVOLT, max_value=MAX_GAIN_MM_PER_MILLIVOLT
    )


class PointSerializer(serializers.Serializer):
    x = serializers.FloatField()
    y = serializers.FloatField()


class StudyMetadataSerializer(serializers.Serializer):
    """The app's ``StudyMetadata``, field for field."""

    anonymousId = serializers.CharField(max_length=24, allow_blank=False)
    capturedAt = serializers.DateTimeField()
    mount = serializers.ChoiceField(choices=list(MOUNT_IDS))
    calibration = CalibrationSerializer()
    quad = serializers.ListField(child=PointSerializer(), min_length=4, max_length=4)


class StudyUploadSerializer(serializers.Serializer):
    """A multipart upload: the image, its dimensions, and the metadata as JSON.

    The dimensions travel beside the metadata rather than inside it because that is where
    ``QueuedStudy`` keeps them, and an adapter that can transcribe rather than restructure
    is an adapter with fewer places to put a value in the wrong field.
    """

    image = serializers.ImageField()
    imageWidth = serializers.IntegerField(min_value=1)
    imageHeight = serializers.IntegerField(min_value=1)
    metadata = serializers.JSONField()

    def validate_metadata(self, value: Any) -> dict[str, Any]:
        # Multipart carries strings, so metadata usually arrives as a JSON string; a JSON
        # request body delivers it already parsed. Accept both.
        if isinstance(value, (str, bytes)):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                raise serializers.ValidationError("metadata is not valid JSON")
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata must be an object")

        form = StudyMetadataSerializer(data=value)
        form.is_valid(raise_exception=True)
        return dict(form.validated_data)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        image = attrs["image"]
        declared_width = attrs["imageWidth"]
        declared_height = attrs["imageHeight"]

        # Checked before anything about the quad: a payload this large or this densely
        # packed with pixels is refused regardless of whether the rest of it is otherwise
        # fine. See the settings' own comments for why FILE_UPLOAD_MAX_MEMORY_SIZE does not
        # already cover this.
        if image.size > settings.STUDY_MAX_UPLOAD_SIZE_BYTES:
            raise serializers.ValidationError(
                f"image is {image.size} bytes, over the {settings.STUDY_MAX_UPLOAD_SIZE_BYTES} byte limit"
            )

        # The quad is in the pixels of *this* image. If the declared dimensions are not
        # the file's own, the corners point somewhere else entirely, and the rectified
        # crop would be of the wrong region while looking perfectly ordinary.
        actual = _image_size(image)
        if actual is not None:
            # A "decompression bomb": a file that is small in bytes -- a few KB of a
            # solid-colour PNG compresses trivially well -- but declares dimensions that
            # decode to gigabytes of pixel data. Pillow has its own guard for this
            # (Image.MAX_IMAGE_PIXELS, a DecompressionBombError above roughly twice its
            # ~89 megapixel default), but that is a backstop for arbitrary Pillow callers:
            # it would surface here as an unhandled exception rather than
            # 'payload-rejected', and it says nothing at all between its warning
            # threshold and that error threshold. This check runs first, at a lower
            # bound, so it is the one that actually speaks.
            pixel_count = actual[0] * actual[1]
            if pixel_count > settings.STUDY_MAX_UPLOAD_PIXELS:
                raise serializers.ValidationError(
                    f"image is {actual[0]}x{actual[1]} ({pixel_count} pixels), over the "
                    f"{settings.STUDY_MAX_UPLOAD_PIXELS} pixel limit"
                )

            if actual != (declared_width, declared_height):
                raise serializers.ValidationError(
                    f"imageWidth/imageHeight ({declared_width}x{declared_height}) do not match "
                    f"the uploaded image ({actual[0]}x{actual[1]})"
                )

        points = [(_finite(p["x"]), _finite(p["y"])) for p in attrs["metadata"]["quad"]]

        tol = QUAD_BOUNDS_TOLERANCE_PX
        for x, y in points:
            if not (-tol <= x <= declared_width + tol and -tol <= y <= declared_height + tol):
                raise serializers.ValidationError(f"quad corner ({x}, {y}) lies outside the image")

        if not _is_simple_quad(points):
            raise serializers.ValidationError("quad is self-crossing or degenerate")

        area_fraction = _shoelace_area(points) / float(declared_width * declared_height)
        if area_fraction < MIN_QUAD_AREA_FRACTION:
            raise serializers.ValidationError(
                f"quad covers {area_fraction:.1%} of the image, below the {MIN_QUAD_AREA_FRACTION:.0%} minimum"
            )

        # Clamped after validating, not before: a corner two pixels out is rounding and is
        # pulled in, but one that was far out has already been refused above.
        attrs["metadata"]["quad"] = [
            {"x": min(max(x, 0.0), float(declared_width)), "y": min(max(y, 0.0), float(declared_height))}
            for x, y in points
        ]
        return attrs

    def create(self, validated_data: dict[str, Any]) -> Study:
        metadata = validated_data["metadata"]
        calibration = metadata["calibration"]
        return Study.objects.create(
            owner=self.context["request"].user,
            anonymous_id=metadata["anonymousId"],
            captured_at=metadata["capturedAt"],
            mount=metadata["mount"],
            speed_mm_per_second=calibration["speedMmPerSecond"],
            gain_mm_per_millivolt=calibration["gainMmPerMillivolt"],
            quad=metadata["quad"],
            image=validated_data["image"],
            image_width=validated_data["imageWidth"],
            image_height=validated_data["imageHeight"],
        )
