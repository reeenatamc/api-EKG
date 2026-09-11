"""The analysis endpoint, the job queue, and the parts of the runner that can be tested
without a 370 MB checkpoint.

The end-to-end run -- a real photograph through the digitizer and ECGFounder -- is not
here. It needs the Open-ECG-Digitizer checkout, its weights, and tens of seconds of CPU
per case, which makes it an integration test rather than a unit one. What is here is
everything that decides *what the app is told*, which is where a wrong answer would do
its damage.
"""

from __future__ import annotations

import io
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from analysis.models import (
    ANALYSIS_FAILURE_REASONS,
    FAILURE_SERVER_ERROR,
    FAILURE_UNSUPPORTED_MOUNT,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_READY,
    Analysis,
)
from analysis.runner import PipelineRunner
from studies.models import Study

User = get_user_model()

# EcgAnalysis in app-EKG's src/ecg/EcgAnalysisService.ts. None of these members is
# optional, so a missing key lands in the app as undefined where it expects null.
ECG_ANALYSIS_KEYS = {"studyId", "status", "signal", "measurements", "observations", "failure", "completedAt"}

SQUARE_QUAD = [{"x": 0.0, "y": 0.0}, {"x": 200.0, "y": 0.0}, {"x": 200.0, "y": 150.0}, {"x": 0.0, "y": 150.0}]


def png_bytes(width: int = 200, height: int = 150) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AnalysisEndpointTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="a@example.com", password="a password", is_verified=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.study = self.make_study(self.user)

    def make_study(self, owner) -> Study:
        return Study.objects.create(
            owner=owner,
            anonymous_id="ECG-260806-4K2M",
            captured_at=timezone.now(),
            mount="standard-3x4",
            quad=SQUARE_QUAD,
            image=SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
            image_width=200,
            image_height=150,
        )

    def url(self, study: Study | None = None) -> str:
        return f"/studies/{(study or self.study).id}/analysis/"

    def test_getting_an_analysis_nobody_requested_is_not_found(self) -> None:
        # The app's ``get`` is specified to answer null for a study the server does not
        # know about, and it does not know about an analysis nobody asked for.
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_requesting_an_analysis_queues_it(self) -> None:
        response = self.client.post(self.url())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], STATUS_QUEUED)
        self.assertEqual(Analysis.objects.count(), 1)

    def test_the_initial_body_is_a_complete_ecg_analysis(self) -> None:
        body = self.client.post(self.url()).json()

        self.assertEqual(set(body), ECG_ANALYSIS_KEYS)
        self.assertEqual(body["studyId"], str(self.study.id))
        self.assertIsNone(body["signal"])
        self.assertIsNone(body["measurements"])
        self.assertIsNone(body["failure"])
        self.assertIsNone(body["completedAt"])
        self.assertEqual(body["observations"], [])

    def test_requesting_twice_does_not_start_a_second_run(self) -> None:
        # The app's queue retries. A second request must not duplicate the work, and must
        # not discard a finished result.
        first = self.client.post(self.url())
        second = self.client.post(self.url())

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Analysis.objects.count(), 1)

    def test_requesting_again_after_a_failure_puts_it_back_in_the_queue(self) -> None:
        # The other half of the idempotency rule, and the one that was missing. The app
        # offers a retry on a failed study; without this the second request returned the
        # same failed record and the button did nothing anyone could see.
        self.client.post(self.url())
        Analysis.objects.get().mark_failed(FAILURE_UNSUPPORTED_MOUNT)

        body = self.client.post(self.url()).json()

        self.assertEqual(body["status"], STATUS_QUEUED)
        self.assertIsNone(body["failure"])
        self.assertEqual(Analysis.objects.count(), 1)

    def test_a_queued_analysis_is_not_disturbed_by_a_repeated_request(self) -> None:
        # Requeueing one that is already waiting would reset the attempt counter that
        # stops a study capable of killing the worker from being retried forever.
        self.client.post(self.url())
        Analysis.objects.filter(pk=Analysis.objects.get().pk).update(attempts=2)

        self.client.post(self.url())

        self.assertEqual(Analysis.objects.get().attempts, 2)

    def test_a_finished_analysis_survives_a_repeated_request(self) -> None:
        self.client.post(self.url())
        analysis = Analysis.objects.get()
        analysis.mark_finished(
            {
                "studyId": str(self.study.id),
                "status": STATUS_READY,
                "signal": {"samplingRateHz": 500, "durationSeconds": 10.0, "leads": []},
                "measurements": None,
                "observations": [
                    {
                        "id": "sinus-rhythm",
                        "label": "SINUS RHYTHM",
                        "leads": ["II"],
                        "confidence": 0.9,
                        "needsReview": True,
                    }
                ],
                "failure": None,
                "completedAt": "2026-08-06T10:00:00+00:00",
            }
        )

        body = self.client.post(self.url()).json()

        self.assertEqual(body["status"], STATUS_READY)
        self.assertEqual(len(body["observations"]), 1)

    def test_the_status_is_polled_through_get(self) -> None:
        self.client.post(self.url())

        body = self.client.get(self.url()).json()

        self.assertEqual(body["status"], STATUS_QUEUED)
        self.assertEqual(set(body), ECG_ANALYSIS_KEYS)

    def test_another_users_study_is_not_found_rather_than_forbidden(self) -> None:
        # A 403 would confirm the id exists, and a study id is the only handle anyone has
        # on a clinical image.
        stranger = User.objects.create_user(email="b@example.com", password="a password", is_verified=True)
        theirs = self.make_study(stranger)

        self.assertEqual(self.client.get(self.url(theirs)).status_code, 404)
        self.assertEqual(self.client.post(self.url(theirs)).status_code, 404)

    def test_an_anonymous_caller_is_refused(self) -> None:
        self.client.force_authenticate(None)

        self.assertEqual(self.client.get(self.url()).status_code, 401)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class QueueTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="a@example.com", password="a password", is_verified=True)

    def make_analysis(self) -> Analysis:
        study = Study.objects.create(
            owner=self.user,
            anonymous_id="ECG-260806-0001",
            captured_at=timezone.now(),
            mount="standard-3x4",
            quad=SQUARE_QUAD,
            image=SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
            image_width=200,
            image_height=150,
        )
        return Analysis.objects.create(study=study)

    def test_claiming_takes_the_oldest_queued_analysis(self) -> None:
        first = self.make_analysis()
        self.make_analysis()

        claimed = Analysis.claim_next()

        self.assertEqual(claimed.pk, first.pk)
        self.assertEqual(claimed.status, "processing")
        self.assertEqual(claimed.attempts, 1)

    def test_a_claimed_analysis_cannot_be_claimed_again(self) -> None:
        # This is what keeps two workers from reading the same ECG twice.
        self.make_analysis()

        self.assertIsNotNone(Analysis.claim_next())
        self.assertIsNone(Analysis.claim_next())

    def test_an_empty_queue_claims_nothing(self) -> None:
        self.assertIsNone(Analysis.claim_next())

    def test_stale_processing_rows_are_returned_to_the_queue(self) -> None:
        # A worker killed mid-study leaves a row claimed that nothing would ever pick up.
        analysis = self.make_analysis()
        Analysis.claim_next()

        # Age the claim on purpose. reclaim_stale filters on started_at < cutoff, so
        # passing timezone.now() right after the claim leaves the two one clock tick
        # apart at best, and on a platform whose clock is coarser than the claim itself
        # they are the same instant: nothing is older than the cutoff and the test fails
        # without anything being wrong. The filter is right as it stands -- widening it
        # to <= would let a worker reclaim the row it just took. What was missing is a
        # row that is actually stale, which is what the worker means by it: its own
        # cutoff is half an hour back.
        Analysis.objects.filter(pk=analysis.pk).update(started_at=timezone.now() - timedelta(hours=1))

        reclaimed = Analysis.reclaim_stale(timezone.now())

        analysis.refresh_from_db()
        self.assertEqual(reclaimed, 1)
        self.assertEqual(analysis.status, STATUS_QUEUED)

    def test_requeue_returns_a_failed_analysis_to_the_queue(self) -> None:
        # The app offers a retry on a failed study, and POST is idempotent: without this
        # transition the button returned the same failed record and did nothing visible.
        analysis = self.make_analysis()
        analysis.mark_failed(FAILURE_UNSUPPORTED_MOUNT)

        analysis.requeue()
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, STATUS_QUEUED)
        self.assertIsNone(analysis.failure)
        self.assertEqual(analysis.attempts, 0)

    def test_requeue_drops_the_previous_run(self) -> None:
        # Keeping the old timestamps would describe a run that is no longer the current
        # one, and to_body would serve a queued analysis carrying last time's reason.
        analysis = self.make_analysis()
        analysis.mark_failed(FAILURE_UNSUPPORTED_MOUNT, diagnostics={"stage": "digitize"})

        analysis.requeue()

        self.assertIsNone(analysis.started_at)
        self.assertIsNone(analysis.completed_at)
        self.assertIsNone(analysis.payload)
        # The diagnostics described that run. mark_finished only replaces them when it is
        # handed some, so left here they would outlive the failure and sit on a ready row.
        self.assertEqual(analysis.diagnostics, {})

    def test_requeue_does_not_touch_a_row_a_worker_has_since_claimed(self) -> None:
        # The app's queue retries, so two retries can read the same failed row. The first
        # requeues it and a worker claims it; the second must then do nothing, or it puts
        # a study that is being processed back to queued with attempts at zero, and the
        # next worker runs it a second time.
        analysis = self.make_analysis()
        analysis.mark_failed(FAILURE_UNSUPPORTED_MOUNT)
        stale = Analysis.objects.get(pk=analysis.pk)  # what the second request read

        self.assertTrue(analysis.requeue())
        claimed = Analysis.claim_next()
        self.assertEqual(claimed.pk, analysis.pk)

        self.assertFalse(stale.requeue())

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, "processing")
        self.assertEqual(analysis.attempts, 1)

    def test_a_repeatedly_failing_study_is_not_reclaimed_forever(self) -> None:
        analysis = self.make_analysis()
        Analysis.objects.filter(pk=analysis.pk).update(status="processing", attempts=3, started_at=timezone.now())

        self.assertEqual(Analysis.reclaim_stale(timezone.now()), 0)

    def test_mark_failed_refuses_a_reason_the_app_does_not_know(self) -> None:
        analysis = self.make_analysis()

        with self.assertRaises(ValueError):
            analysis.mark_failed("out-of-disk")

    def test_mark_failed_produces_a_complete_body(self) -> None:
        analysis = self.make_analysis()

        analysis.mark_failed(FAILURE_SERVER_ERROR)

        self.assertEqual(set(analysis.to_body()), ECG_ANALYSIS_KEYS)
        self.assertEqual(analysis.status, STATUS_FAILED)
        self.assertEqual(analysis.to_body()["failure"], FAILURE_SERVER_ERROR)

    def test_the_status_is_taken_from_the_contract_not_decided_twice(self) -> None:
        # ecg_pipeline.contract.to_analysis already decides that a degraded result is
        # 'failed'. Deciding it again here is how the two would come to disagree.
        analysis = self.make_analysis()

        analysis.mark_finished(
            {
                "studyId": "x",
                "status": STATUS_FAILED,
                "signal": None,
                "measurements": None,
                "observations": [],
                "failure": "unreadable-image",
                "completedAt": None,
            }
        )

        self.assertEqual(analysis.status, STATUS_FAILED)
        self.assertEqual(analysis.failure, "unreadable-image")


class FailureVocabularyTests(TestCase):
    def test_every_reason_is_one_the_app_knows(self) -> None:
        # Transcribed from AnalysisFailureReason in EcgAnalysisService.ts.
        app_side = {
            "unreadable-image",
            "grid-not-detected",
            "trace-incomplete",
            "unsupported-mount",
            "network-unreachable",
            "server-error",
            "unexpected",
        }

        self.assertTrue(ANALYSIS_FAILURE_REASONS <= app_side)


class MountCrossCheckTests(TestCase):
    """Two independent answers to 'which mount is this', and what to do when they differ."""

    def setUp(self) -> None:
        self.runner = PipelineRunner()

    def check(self, declared: str, identified: str | None) -> dict:
        study = SimpleNamespace(id="study-1", mount=declared)
        record = {"digitization": {"lead_layout": identified}}
        body = {
            "studyId": "study-1",
            "status": STATUS_READY,
            "signal": {},
            "measurements": None,
            "observations": [{"id": "x"}],
            "failure": None,
            "completedAt": "2026-08-06T10:00:00+00:00",
        }
        return self.runner._apply_mount_cross_check(study, record, body, {})

    def test_an_agreeing_layout_passes_through(self) -> None:
        body = self.check("standard-3x4", "standard_3x4")

        self.assertEqual(body["status"], STATUS_READY)
        self.assertEqual(len(body["observations"]), 1)

    def test_a_rhythm_variant_agrees_with_the_rhythm_mount(self) -> None:
        for layout in ("standard_3x4_with_r1", "standard_3x4_with_r2", "standard_3x4_with_r3"):
            with self.subTest(layout=layout):
                self.assertEqual(self.check("rhythm-3x4", layout)["status"], STATUS_READY)

    def test_a_rhythm_strip_on_a_plain_3x4_mount_is_not_a_disagreement(self) -> None:
        # The mount menu is a framing guide; it does not ask whether the print carries a
        # rhythm strip. The grid is the same grid and the leads have the same names, so a
        # strip is extra signal rather than different signal. Refusing this rejected a
        # perfectly readable ECG in the first version of the check.
        for layout in ("standard_3x4_with_r1", "standard_3x4_with_r2", "standard_3x4_with_r3"):
            with self.subTest(layout=layout):
                self.assertEqual(self.check("standard-3x4", layout)["status"], STATUS_READY)

    def test_a_plain_3x4_under_the_rhythm_mount_is_not_a_disagreement(self) -> None:
        self.assertEqual(self.check("rhythm-3x4", "standard_3x4")["status"], STATUS_READY)

    def test_a_different_grid_is_still_a_disagreement(self) -> None:
        # What the check is actually for: a layout that puts different lead names in the
        # same grid positions.
        for declared, identified in [
            ("six-2", "standard_3x4"),
            ("standard-3x4", "standard_6x2"),
            ("twelve-1", "standard_6x2"),
        ]:
            with self.subTest(declared=declared, identified=identified):
                self.assertEqual(self.check(declared, identified)["failure"], FAILURE_UNSUPPORTED_MOUNT)

    def test_a_disagreeing_layout_refuses_the_reading(self) -> None:
        # The user said 12x1, the image says 3x4. Either the framing guide was wrong or
        # the layout was misidentified -- and misidentification means the leads are about
        # to be served under the wrong names.
        body = self.check("twelve-1", "standard_3x4")

        self.assertEqual(body["status"], STATUS_FAILED)
        self.assertEqual(body["failure"], FAILURE_UNSUPPORTED_MOUNT)
        self.assertEqual(body["observations"], [])
        self.assertIsNone(body["completedAt"])

    def test_a_right_sided_print_read_as_left_sided_is_refused(self) -> None:
        # The one that matters most: the digitizer fills V4/V5/V6 by position, and
        # ecg_pipeline.contract relabels them to V4R/V5R/V6R only for the right-sided
        # layout. Getting this wrong serves a right-sided trace under a left-sided name.
        body = self.check("right-3x3", "standard_3x4")

        self.assertEqual(body["failure"], FAILURE_UNSUPPORTED_MOUNT)

    def test_an_unidentified_layout_is_left_to_the_pipeline(self) -> None:
        # ecg_pipeline already degrades this case; saying it again here would report the
        # same problem in two vocabularies.
        body = self.check("standard-3x4", "Unknown layout")

        self.assertEqual(body["status"], STATUS_READY)


class WorkdirCleanupTests(TestCase):
    """A study's intermediate files are dropped once it is ready, and kept when it failed."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(email="a@example.com", password="a password", is_verified=True)
        self.work_root = Path(tempfile.mkdtemp())
        self.runner = PipelineRunner(work_root=self.work_root)

    def run_with(self, record: dict | None | Exception) -> tuple[Analysis, Path]:
        study = Study.objects.create(
            owner=self.user,
            anonymous_id="ECG-260806-0001",
            captured_at=timezone.now(),
            mount="standard-3x4",
            quad=SQUARE_QUAD,
            image=SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
            image_width=200,
            image_height=150,
        )
        analysis = Analysis.objects.create(study=study)
        workdir = self.work_root / str(study.id)
        (workdir / "output").mkdir(parents=True)
        (workdir / "output" / "ecg_timeseries_canonical.csv").write_text("I,II\n1,2\n")

        # No CSV in the record, so interpretation is skipped and no checkpoint is needed;
        # what is under test is what happens to the directory afterwards. An exception
        # stands in for the digitizer blowing up.
        with patch.object(PipelineRunner, "_digitize") as digitize:
            if isinstance(record, Exception):
                digitize.side_effect = record
            else:
                digitize.return_value = record
            self.runner.run(analysis)
        analysis.refresh_from_db()
        return analysis, workdir

    def test_a_ready_analysis_drops_its_intermediate_files(self) -> None:
        analysis, workdir = self.run_with({"digitization": {"lead_layout": "standard_3x4"}, "degraded": False})

        self.assertEqual(analysis.status, STATUS_READY)
        self.assertFalse(workdir.exists())

    def test_a_failed_analysis_keeps_them_for_inspection(self) -> None:
        analysis, workdir = self.run_with({"digitization": {"lead_layout": "Unknown layout"}, "degraded": True})

        self.assertEqual(analysis.status, STATUS_FAILED)
        self.assertTrue(workdir.exists())

    @override_settings(ECG_KEEP_WORK=True)
    def test_the_setting_keeps_everything(self) -> None:
        analysis, workdir = self.run_with({"digitization": {"lead_layout": "standard_3x4"}, "degraded": False})

        self.assertEqual(analysis.status, STATUS_READY)
        self.assertTrue(workdir.exists())

    def test_the_pipeline_version_is_recorded(self) -> None:
        # The pipeline is a separate repository that moves on its own; the row is the only
        # place that ties a stored reading to the code that produced it.
        analysis, _ = self.run_with({"digitization": {"lead_layout": "standard_3x4"}, "degraded": False})

        self.assertRegex(analysis.diagnostics["pipeline_version"], r"^\d+\.\d+")

    def test_the_pipeline_version_is_recorded_on_failures_too(self) -> None:
        # A study that failed under one version and is retried under another is the case
        # where knowing which one it was matters most.
        unreadable, _ = self.run_with(None)
        self.assertEqual(unreadable.status, STATUS_FAILED)
        self.assertIn("pipeline_version", unreadable.diagnostics)

        crashed, _ = self.run_with(RuntimeError("boom"))
        self.assertEqual(crashed.failure, FAILURE_SERVER_ERROR)
        self.assertIn("pipeline_version", crashed.diagnostics)


class InterpretationMergeTests(TestCase):
    """Digitization problems dominate: they invalidate everything downstream."""

    def setUp(self) -> None:
        self.runner = PipelineRunner()

    def merge(self, record: dict, interpreted: dict | Exception) -> dict:
        with patch.object(PipelineRunner, "_interpret") as interpret:
            if isinstance(interpreted, Exception):
                interpret.side_effect = interpreted
            else:
                interpret.return_value = interpreted
            return self.runner._merge_interpretation(record, "/tmp/x.csv")

    def test_a_clean_record_stays_clean(self) -> None:
        merged = self.merge(
            {"digitization": {"lead_layout": "standard_3x4"}, "warnings": [], "degraded": False},
            {"topk": [{"label": "SINUS RHYTHM", "prob": 0.9}], "degraded": False},
        )

        self.assertFalse(merged["degraded"])
        self.assertEqual(len(merged["topk"]), 1)

    def test_a_degraded_digitization_stays_degraded_however_confident_the_model_is(self) -> None:
        # The failure mode ecg-pipeline exists to guard against: a confident probability
        # on a scan that could not be read.
        merged = self.merge(
            {"digitization": {"lead_layout": "standard_3x4"}, "warnings": ["poor coverage"], "degraded": True},
            {"topk": [{"label": "LATERAL INFARCT", "prob": 0.99}], "degraded": False},
        )

        self.assertTrue(merged["degraded"])

    def test_an_unidentified_layout_degrades_the_record(self) -> None:
        merged = self.merge(
            {"digitization": {"lead_layout": "Unknown layout"}, "warnings": [], "degraded": False},
            {"topk": [], "degraded": False},
        )

        self.assertTrue(merged["degraded"])

    def test_an_interpretation_crash_degrades_rather_than_propagates(self) -> None:
        # One bad record must not cost the whole study an exception; it costs it a result.
        merged = self.merge(
            {"digitization": {"lead_layout": "standard_3x4"}, "warnings": [], "degraded": False},
            RuntimeError("checkpoint went missing"),
        )

        self.assertTrue(merged["degraded"])
        self.assertIn("error", merged)

    def test_warnings_from_both_halves_are_kept(self) -> None:
        merged = self.merge(
            {"digitization": {"lead_layout": "standard_3x4"}, "warnings": ["upscaled 3x"], "degraded": False},
            {"topk": [], "warnings": ["no full-length lead"], "degraded": True},
        )

        self.assertEqual(merged["warnings"], ["upscaled 3x", "no full-length lead"])

    def test_the_digitization_metadata_is_not_overwritten_by_the_interpretation(self) -> None:
        merged = self.merge(
            {"digitization": {"lead_layout": "standard_3x4"}, "warnings": [], "degraded": False},
            {"digitization": None, "topk": []},
        )

        self.assertEqual(merged["digitization"], {"lead_layout": "standard_3x4"})
