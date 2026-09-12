"""Receiving a study.

The quad checks carry the weight here. Everything downstream trusts those four corners:
a homography built from a self-crossing or misplaced quad does not fail, it rectifies the
wrong region into an image that still looks like an ECG. There is no later stage that
notices, so this is the stage that has to.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from studies import failures
from studies.models import Study
from studies.mounts import MOUNT_IDS

User = get_user_model()

IMAGE_WIDTH, IMAGE_HEIGHT = 400, 300

# A crop well inside the frame, slightly skewed the way a hand-held photograph is.
TILTED_QUAD = [
    {"x": 20.0, "y": 15.0},
    {"x": 380.0, "y": 25.0},
    {"x": 375.0, "y": 285.0},
    {"x": 15.0, "y": 275.0},
]


def png_bytes(width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


def metadata(**overrides: Any) -> dict[str, Any]:
    body = {
        "anonymousId": "ECG-260806-4K2M",
        "capturedAt": "2026-08-06T10:00:00Z",
        "mount": "standard-3x4",
        "calibration": {"speedMmPerSecond": 25, "gainMmPerMillivolt": 10},
        "quad": TILTED_QUAD,
    }
    body.update(overrides)
    return body


class UploadTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="a@example.com", password="a password", is_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def upload(self, meta: dict[str, Any] | None = None, image: bytes | None = None, **overrides: Any):
        payload = {
            "image": SimpleUploadedFile("ecg.png", image if image is not None else png_bytes(), "image/png"),
            "imageWidth": IMAGE_WIDTH,
            "imageHeight": IMAGE_HEIGHT,
            "metadata": json.dumps(meta if meta is not None else metadata()),
        }
        payload.update(overrides)
        return self.client.post("/studies/", payload, format="multipart")

    # -- the happy path ---------------------------------------------------------------

    def test_an_upload_returns_the_apps_receipt(self) -> None:
        response = self.upload()

        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(body), {"remoteId", "receivedAt"})
        self.assertEqual(body["remoteId"], str(Study.objects.get().id))

    def test_the_upload_is_stored_with_its_calibration_and_mount(self) -> None:
        self.upload(metadata(mount="six-2", calibration={"speedMmPerSecond": 50, "gainMmPerMillivolt": 5}))

        study = Study.objects.get()
        self.assertEqual(study.mount, "six-2")
        self.assertEqual(study.speed_mm_per_second, 50)
        self.assertEqual(study.gain_mm_per_millivolt, 5)
        self.assertFalse(study.has_standard_calibration)

    def test_every_mount_the_app_offers_is_accepted(self) -> None:
        for mount in MOUNT_IDS:
            with self.subTest(mount=mount):
                self.assertEqual(self.upload(metadata(mount=mount)).status_code, 201)

    def test_the_study_belongs_to_the_uploader(self) -> None:
        self.upload()

        self.assertEqual(Study.objects.get().owner, self.user)

    def test_there_is_nowhere_to_put_a_patient_name(self) -> None:
        # study.ts is explicit that the absence is the design: an ECG photograph with a
        # name beside it stops being an anonymous datum and becomes a medical record.
        # A field added here would be filled by somebody eventually.
        fields = {field.name for field in Study._meta.get_fields()}

        self.assertNotIn("patient_name", fields)
        self.assertNotIn("name", fields)
        self.assertNotIn("date_of_birth", fields)

    # -- refusals ---------------------------------------------------------------------

    def test_an_anonymous_caller_is_refused(self) -> None:
        self.client.force_authenticate(None)

        self.assertEqual(self.upload().status_code, 401)

    def test_a_self_crossing_quad_is_rejected(self) -> None:
        # Two corners dragged across each other -- the bow-tie quad.ts calls out. A
        # homography from it folds the image rather than failing.
        bow_tie = [
            {"x": 20.0, "y": 15.0},
            {"x": 380.0, "y": 25.0},
            {"x": 15.0, "y": 275.0},
            {"x": 375.0, "y": 285.0},
        ]

        response = self.upload(metadata(quad=bow_tie))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], failures.PAYLOAD_REJECTED)

    def test_a_corner_outside_the_image_is_rejected(self) -> None:
        outside = [{"x": -50.0, "y": 15.0}] + TILTED_QUAD[1:]

        self.assertEqual(self.upload(metadata(quad=outside)).status_code, 400)

    def test_a_quad_covering_almost_nothing_is_rejected(self) -> None:
        # A stale quad sent with a new photograph. The crop would be of a corner.
        tiny = [{"x": 0.0, "y": 0.0}, {"x": 40.0, "y": 0.0}, {"x": 40.0, "y": 30.0}, {"x": 0.0, "y": 30.0}]

        self.assertEqual(self.upload(metadata(quad=tiny)).status_code, 400)

    def test_three_collinear_corners_are_rejected(self) -> None:
        flat = [{"x": 0.0, "y": 0.0}, {"x": 200.0, "y": 0.0}, {"x": 400.0, "y": 0.0}, {"x": 0.0, "y": 300.0}]

        self.assertEqual(self.upload(metadata(quad=flat)).status_code, 400)

    def test_a_quad_with_the_wrong_number_of_corners_is_rejected(self) -> None:
        self.assertEqual(self.upload(metadata(quad=TILTED_QUAD[:3])).status_code, 400)

    def test_a_non_finite_coordinate_is_rejected(self) -> None:
        # json.loads accepts the literal Infinity, and it would turn the homography into a
        # matrix of NaNs several steps from here.
        raw = json.dumps(metadata()).replace('"x": 20.0', '"x": Infinity')

        self.assertEqual(self.upload_raw(raw).status_code, 400)

    def upload_raw(self, raw_metadata: str):
        return self.client.post(
            "/studies/",
            {
                "image": SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
                "imageWidth": IMAGE_WIDTH,
                "imageHeight": IMAGE_HEIGHT,
                "metadata": raw_metadata,
            },
            format="multipart",
        )

    def test_dimensions_that_do_not_match_the_image_are_rejected(self) -> None:
        # The quad is in this image's pixels. If the declared size is not the file's own,
        # the corners point at a different region entirely and the crop looks ordinary.
        response = self.upload(image=png_bytes(800, 600))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], failures.PAYLOAD_REJECTED)

    def test_an_unknown_mount_is_rejected(self) -> None:
        self.assertEqual(self.upload(metadata(mount="six-3")).status_code, 400)

    def test_a_zero_or_negative_calibration_is_rejected(self) -> None:
        for calibration in (
            {"speedMmPerSecond": 0, "gainMmPerMillivolt": 10},
            {"speedMmPerSecond": 25, "gainMmPerMillivolt": -10},
        ):
            with self.subTest(calibration=calibration):
                self.assertEqual(self.upload(metadata(calibration=calibration)).status_code, 400)

    def test_a_file_that_is_not_an_image_is_rejected(self) -> None:
        self.assertEqual(self.upload(image=b"this is not a png").status_code, 400)

    def test_malformed_metadata_is_rejected(self) -> None:
        self.assertEqual(self.upload_raw("{not json").status_code, 400)

    def test_a_rejection_names_a_cause_the_app_knows(self) -> None:
        reason = self.upload(metadata(quad=TILTED_QUAD[:3])).json()["reason"]

        self.assertIn(reason, failures.UPLOAD_FAILURE_REASONS)

    def test_nothing_is_stored_when_an_upload_is_refused(self) -> None:
        self.upload(metadata(quad=TILTED_QUAD[:3]))

        self.assertEqual(Study.objects.count(), 0)

    # -- upload limits ------------------------------------------------------------------

    def test_an_image_over_the_size_limit_is_rejected(self) -> None:
        # The limit is pinned below this ordinary image's own byte length rather than to a
        # fixed number, so the test does not depend on exactly how well PNG compresses a
        # solid-colour fixture.
        image = png_bytes()
        with override_settings(STUDY_MAX_UPLOAD_SIZE_BYTES=len(image) - 1):
            response = self.upload(image=image)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], failures.PAYLOAD_REJECTED)
        self.assertEqual(Study.objects.count(), 0)

    def test_an_image_over_the_pixel_limit_is_rejected(self) -> None:
        # A small, cheaply-generated image stands in for a decompression bomb: what is
        # being checked is the width*height count, not the byte size, so the fixture never
        # needs to actually be huge.
        with override_settings(STUDY_MAX_UPLOAD_PIXELS=(IMAGE_WIDTH * IMAGE_HEIGHT) - 1):
            response = self.upload()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["reason"], failures.PAYLOAD_REJECTED)
        self.assertEqual(Study.objects.count(), 0)

    def test_an_ordinary_image_still_works_under_both_limits(self) -> None:
        # The default limits (25 MB, 50 megapixels) must not reject the phone photos this
        # service exists to read; nothing here overrides them.
        response = self.upload()

        self.assertEqual(response.status_code, 201)


class UploadFailureVocabularyTests(TestCase):
    def test_every_reason_is_one_the_app_knows(self) -> None:
        # Transcribed from UploadFailureReason in app-EKG's src/capture/study.ts.
        app_side = {"network-unreachable", "unauthorized", "payload-rejected", "server-error", "unexpected"}

        self.assertTrue(failures.UPLOAD_FAILURE_REASONS <= app_side)

    def test_an_invented_reason_raises(self) -> None:
        with self.assertRaises(ValueError):
            failures.failure("disk-full")


class StudyDeletionTests(TestCase):
    """Deleting a study must take its files with it.

    ``on_delete=CASCADE`` on ``Study.owner`` and ``Analysis.study`` only ever deletes rows.
    Left alone, the original image and whatever the worker wrote under WORK_ROOT would sit
    on disk forever, owned by nothing -- for a health datum that is a leak, not an
    oversight. ``studies/signals.py`` is what actually removes them.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(email="a@example.com", password="a password", is_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def upload(self) -> Study:
        payload = {
            "image": SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
            "imageWidth": IMAGE_WIDTH,
            "imageHeight": IMAGE_HEIGHT,
            "metadata": json.dumps(metadata()),
        }
        response = self.client.post("/studies/", payload, format="multipart")
        return Study.objects.get(pk=response.json()["remoteId"])

    def test_the_image_file_exists_after_an_upload(self) -> None:
        # The premise every test below needs: if nothing were ever written, deleting the
        # study would trivially "clean up" nothing.
        study = self.upload()

        self.assertTrue(Path(study.image.path).is_file())

    def test_deleting_the_study_removes_its_image_from_disk(self) -> None:
        study = self.upload()
        image_path = Path(study.image.path)

        study.delete()

        self.assertFalse(image_path.exists())
        self.assertFalse(image_path.parent.exists())

    def test_deleting_the_study_removes_its_work_directory(self) -> None:
        study = self.upload()
        workdir = Path(settings.WORK_ROOT) / str(study.id)
        workdir.mkdir(parents=True)
        (workdir / "output" / f"{study.id}_timeseries_canonical.csv").parent.mkdir()
        (workdir / "output" / f"{study.id}_timeseries_canonical.csv").write_text("left by a run that never finished")

        study.delete()

        self.assertFalse(workdir.exists())

    def test_deleting_a_study_with_no_work_directory_does_not_raise(self) -> None:
        # A study whose analysis finished and already cleaned its own workdir
        # (PipelineRunner.clean_workdir), or one whose analysis was never requested at all.
        study = self.upload()

        study.delete()  # must not raise

    def test_deleting_the_owner_removes_the_studys_files_too(self) -> None:
        # The cascade from User down to Study goes through QuerySet-level deletion inside
        # Django's collector, not Study.delete() -- exactly the path a signal, and not an
        # override of delete(), is needed to catch.
        study = self.upload()
        image_path = Path(study.image.path)

        self.user.delete()

        self.assertFalse(image_path.exists())

    def test_deleting_the_study_through_a_queryset_still_removes_files(self) -> None:
        # The admin's bulk delete action and any other Study.objects.filter(...).delete()
        # call this same path; only a per-instance .delete() would be a narrower guarantee
        # than what this project actually needs.
        study = self.upload()
        image_path = Path(study.image.path)

        Study.objects.filter(pk=study.pk).delete()

        self.assertFalse(image_path.exists())
