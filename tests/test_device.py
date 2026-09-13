"""The device setting reaches the whole pipeline, and a missing GPU stops the worker at startup.

No GPU is needed: ``ecg_pipeline.devices`` is replaced by a stand-in wherever availability
matters, so this behaves the same whichever ecg-pipeline version is installed.
"""

from __future__ import annotations

import sys
import types
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from analysis.runner import PipelineRunner


def fake_devices(require: mock.Mock) -> types.ModuleType:
    module = types.ModuleType("ecg_pipeline.devices")
    module.require_device = require  # type: ignore[attr-defined]
    return module


class CheckDeviceTests(SimpleTestCase):
    def test_cpu_asks_the_pipeline_nothing(self) -> None:
        # None in sys.modules makes the import fail, so reaching it would raise.
        with mock.patch.dict(sys.modules, {"ecg_pipeline.devices": None}):
            self.assertEqual(PipelineRunner(device="cpu").check_device(), "cpu")

    def test_cuda_is_checked_by_the_pipeline(self) -> None:
        require = mock.Mock(return_value="cuda")
        with mock.patch.dict(sys.modules, {"ecg_pipeline.devices": fake_devices(require)}):
            runner = PipelineRunner(device="CUDA")
            self.assertEqual(runner.check_device(), "cuda")
        require.assert_called_once_with("CUDA")
        self.assertEqual(runner.device, "cuda")

    def test_a_missing_gpu_propagates_the_pipelines_message(self) -> None:
        require = mock.Mock(side_effect=RuntimeError("ECG_DEVICE=cuda asks for the GPU, but torch sees no GPU"))
        with mock.patch.dict(sys.modules, {"ecg_pipeline.devices": fake_devices(require)}):
            with self.assertRaisesMessage(RuntimeError, "sees no GPU"):
                PipelineRunner(device="cuda").check_device()

    def test_a_pipeline_without_gpu_support_is_refused(self) -> None:
        # An older ecg-pipeline would put only ECGFounder on the GPU and leave the digitizer
        # on the CPU, silently. Refused instead.
        with mock.patch.dict(sys.modules, {"ecg_pipeline.devices": None}):
            with self.assertRaisesMessage(RuntimeError, "0.1.5"):
                PipelineRunner(device="cuda").check_device()


class DigitizeDeviceTests(SimpleTestCase):
    def test_the_device_reaches_the_digitizer(self) -> None:
        from ecg_pipeline import pipeline

        study = SimpleNamespace(id="abc", image=SimpleNamespace(path="/nowhere.png"), quad_points=[], mount="3x4")
        with TemporaryDirectory() as work:
            runner = PipelineRunner(device="cuda", work_root=Path(work))
            with (
                mock.patch("analysis.runner.rectify"),
                mock.patch("analysis.runner.lead_layout_for", return_value=None),
                mock.patch.object(pipeline, "run", return_value=[]) as run,
            ):
                runner._digitize(study, Path(work) / "abc")  # type: ignore[arg-type]
        self.assertEqual(run.call_args.kwargs["device"], "cuda")


class WorkerStartupTests(TestCase):
    def test_a_worker_that_cannot_use_its_device_does_not_start(self) -> None:
        with (
            mock.patch.object(PipelineRunner, "check_device", side_effect=RuntimeError("no GPU visible")),
            mock.patch("analysis.management.commands.run_worker.Analysis.claim_next") as claim,
        ):
            with self.assertRaisesMessage(CommandError, "no GPU visible"):
                call_command("run_worker", "--once", stdout=StringIO())
        claim.assert_not_called()

    def test_a_cpu_worker_starts_as_before(self) -> None:
        out = StringIO()
        with self.settings(ECG_DEVICE="cpu"):
            call_command("run_worker", "--once", stdout=out)
        self.assertIn("device=cpu", out.getvalue())
        self.assertIn("Nothing queued.", out.getvalue())
