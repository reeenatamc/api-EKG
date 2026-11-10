"""The worker in ECG_BACKEND=modal mode, with a fake standing in for the GPU side.

What is checked is everything this side owns: that the rectified photograph is what gets
sent, that the files and paths that come back land in the study's own workdir, that a run
through the real ``_run_inner`` ends ready with the early signal stored, and that a dead
GPU is a server error the app can retry -- never a crash of the worker.
"""

from __future__ import annotations

import io
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image

from analysis.calibration import write_canonical_csv
from analysis.modal_runner import ModalRunner, _relocate
from analysis.models import FAILURE_SERVER_ERROR, STATUS_FAILED, STATUS_READY, Analysis
from studies.models import Study

User = get_user_model()

SQUARE_QUAD = [{"x": 0.0, "y": 0.0}, {"x": 200.0, "y": 0.0}, {"x": 200.0, "y": 150.0}, {"x": 0.0, "y": 150.0}]
LEADS = ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]
REMOTE_ROOT = "/modal/tmpREMOTE"


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (200, 150), (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


def canonical_csv_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.csv"
        write_canonical_csv(path, np.tile(np.array([0.1, 0.2, 0.3, 0.2, 0.1]), (12, 1)), LEADS)
        return path.read_bytes()


class _Method:
    def __init__(self, fn: Any) -> None:
        self.fn = fn
        self.calls: list[tuple] = []

    def remote(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.fn(*args)


class FakeGPU:
    """What gpu/modal_app.py's Analizador returns, without a GPU."""

    def __init__(self, record: dict | None = "default", crash: bool = False) -> None:  # type: ignore[assignment]
        csv_name = "x_timeseries_canonical.csv"
        if record == "default":
            record = {
                "record": "x",
                "source_csv": f"{REMOTE_ROOT}/output/{csv_name}",
                "source_image": f"{REMOTE_ROOT}/input/x.png",
                "digitization": {"lead_layout": "standard_3x4"},
                "warnings": [],
                "degraded": False,
                "gates": [],
            }

        def digitalizar(png: bytes, name: str, layout: str | None) -> dict:
            if crash:
                raise ConnectionError("container lost")
            return {"record": record, "remote_root": REMOTE_ROOT, "files": {csv_name: canonical_csv_bytes()}}

        self.digitalizar = _Method(digitalizar)
        self.interpretar = _Method(lambda text, name, pathway, k: {"topk": [], "degraded": False, "warnings": []})


@override_settings(MODAL_TOKEN_ID="ak-test", MODAL_TOKEN_SECRET="as-test")
class ModalRunnerTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="m@example.com", password="a password", is_verified=True)
        self.work_root = Path(tempfile.mkdtemp())

    def make_analysis(self) -> Analysis:
        study = Study.objects.create(
            owner=self.user,
            anonymous_id="ECG-261110-0001",
            captured_at=timezone.now(),
            mount="standard-3x4",
            quad=SQUARE_QUAD,
            image=SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
            image_width=200,
            image_height=150,
        )
        return Analysis.objects.create(study=study)

    def test_a_study_runs_to_ready_through_the_remote_gpu(self) -> None:
        gpu = FakeGPU()
        runner = ModalRunner(remote=gpu, work_root=self.work_root)
        analysis = self.make_analysis()

        runner.run(analysis)
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, STATUS_READY)
        self.assertIsNotNone(analysis.signal)
        self.assertEqual(len(analysis.signal["leads"]), 12)
        # The rectified PNG crossed, named after the study, with the mount's layout.
        png, name, _layout = gpu.digitalizar.calls[0]
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(name.startswith(str(analysis.study_id)))
        # Interpretation got the CSV that came back, and this worker's pathway.
        text, csv_name, pathway, _k = gpu.interpretar.calls[0]
        self.assertIn("II", text.splitlines()[0])
        self.assertEqual(pathway, runner.pathway)

    def test_remote_paths_are_mapped_into_the_study_workdir(self) -> None:
        gpu = FakeGPU()
        runner = ModalRunner(remote=gpu, work_root=self.work_root)
        analysis = self.make_analysis()
        workdir = self.work_root / str(analysis.study_id)

        record = runner._digitize(analysis.study, workdir)

        self.assertEqual(record["source_csv"], f"{workdir}/output/x_timeseries_canonical.csv")
        self.assertTrue(Path(record["source_csv"]).exists())
        self.assertEqual(record["source_image"], f"{workdir}/input/x.png")

    def test_nothing_back_from_the_digitizer_is_an_unreadable_image(self) -> None:
        runner = ModalRunner(remote=FakeGPU(record=None), work_root=self.work_root)
        analysis = self.make_analysis()

        runner.run(analysis)
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, STATUS_FAILED)
        self.assertNotEqual(analysis.failure, FAILURE_SERVER_ERROR)

    def test_a_lost_gpu_is_a_server_error_the_app_can_retry(self) -> None:
        runner = ModalRunner(remote=FakeGPU(crash=True), work_root=self.work_root)
        analysis = self.make_analysis()

        runner.run(analysis)
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, STATUS_FAILED)
        self.assertEqual(analysis.failure, FAILURE_SERVER_ERROR)

    def test_files_cannot_escape_the_workdir(self) -> None:
        gpu = FakeGPU()
        gpu.digitalizar = _Method(
            lambda *a: {"record": None, "remote_root": REMOTE_ROOT, "files": {"../../escaped.txt": b"x"}}
        )
        runner = ModalRunner(remote=gpu, work_root=self.work_root)
        analysis = self.make_analysis()

        runner._digitize(analysis.study, self.work_root / str(analysis.study_id))

        self.assertFalse((self.work_root / "escaped.txt").exists())

    @override_settings(MODAL_TOKEN_ID="", MODAL_TOKEN_SECRET="")
    def test_the_worker_refuses_to_start_without_a_token(self) -> None:
        with self.assertRaises(RuntimeError):
            ModalRunner(remote=FakeGPU()).check_device()

    def test_relocate_leaves_other_strings_alone(self) -> None:
        value = {"a": [f"{REMOTE_ROOT}/output/f.csv", "standard_3x4"], "b": 3}
        self.assertEqual(
            _relocate(value, REMOTE_ROOT, Path("/w/s")),
            {"a": ["/w/s/output/f.csv", "standard_3x4"], "b": 3},
        )


@override_settings(ECG_BACKEND="nonsense")
class BackendSettingTests(TestCase):
    def test_an_unknown_backend_stops_the_worker(self) -> None:
        with self.assertRaises(CommandError):
            call_command("run_worker", "--once")


@override_settings(
    ECG_BACKEND="modal",
    ECG_WORKER_THREADS=4,
    ECG_WORKER_POLL_SECONDS=0.05,
    MODAL_TOKEN_ID="ak-test",
    MODAL_TOKEN_SECRET="as-test",
)
class ParallelWorkerTests(TransactionTestCase):
    """Four queued studies, four threads, a GPU that takes its time: they overlap."""

    def test_studies_run_at_the_same_time_and_each_exactly_once(self) -> None:
        user = User.objects.create_user(email="p@example.com", password="a password", is_verified=True)
        work_root = Path(tempfile.mkdtemp())
        for i in range(4):
            study = Study.objects.create(
                owner=user,
                anonymous_id=f"ECG-261110-010{i}",
                captured_at=timezone.now(),
                mount="standard-3x4",
                quad=SQUARE_QUAD,
                image=SimpleUploadedFile("ecg.png", png_bytes(), "image/png"),
                image_width=200,
                image_height=150,
            )
            Analysis.objects.create(study=study)

        lock = threading.Lock()
        state = {"now": 0, "peak": 0, "calls": 0}
        gpu = FakeGPU()
        slow_inner = gpu.digitalizar.fn

        def slow(*args: Any) -> Any:
            with lock:
                state["now"] += 1
                state["calls"] += 1
                state["peak"] = max(state["peak"], state["now"])
            threading.Event().wait(0.5)
            with lock:
                state["now"] -= 1
            return slow_inner(*args)

        gpu.digitalizar = _Method(slow)

        from analysis.management.commands import run_worker

        command = run_worker.Command()
        command._running = True

        def stop_when_done() -> None:
            while Analysis.objects.exclude(status__in=[STATUS_READY, STATUS_FAILED]).exists():
                threading.Event().wait(0.1)
            command._running = False

        with patch("analysis.modal_runner.remote_analyzer", return_value=gpu), self.settings(WORK_ROOT=work_root):
            watcher = threading.Thread(target=stop_when_done)
            watcher.start()
            command._loop_threads(None, 4)
            watcher.join()

        self.assertEqual(state["calls"], 4)
        self.assertGreaterEqual(state["peak"], 2)
        self.assertEqual(Analysis.objects.filter(status=STATUS_READY).count(), 4)
