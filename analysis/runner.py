"""Running one study through the pipeline.

The order is: rectify the photograph, digitize it, correct the calibration, interpret,
emit the contract. Only the first and third steps belong to this service; the middle and
the last are ecg-pipeline's, called rather than reimplemented.

Why this does not simply call ``pipeline.run``
----------------------------------------------
``pipeline.run`` digitizes and interprets in one pass, and the calibration correction has
to land between those two. A record printed at 50 mm/s must reach the model already
rescaled, or the model reads a heart beating at twice its real rate -- there is no later
stage that can undo that, because by then it is a probability rather than a signal.

So digitization is run with ``skip_interpretation=True``, the CSV is corrected, and
``interpret_csv`` is called on the corrected file. The merge below mirrors what
``pipeline.run`` does with the two halves, and deliberately keeps its rule that
digitization problems dominate: they invalidate everything downstream.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from analysis import models as analysis_models
from analysis.calibration import correct_csv, is_standard
from analysis.rectify import RECTIFIED_SUFFIX, rectify
from studies.models import Study
from studies.mounts import layout_disagrees_with_mount, lead_layout_for

logger = logging.getLogger(__name__)

UNKNOWN_LAYOUT = "Unknown layout"


class PipelineRunner:
    """Runs studies, holding the interpretation model between them.

    The ECGFounder checkpoint is 370 MB and over a second to read. A worker processes
    studies one after another, so it is loaded once on first use and kept -- which is the
    same reason ``pipeline.run`` builds it outside its own per-record loop.
    """

    def __init__(
        self,
        pathway: str | None = None,
        device: str | None = None,
        top_k: int | None = None,
        work_root: Path | None = None,
    ) -> None:
        self.pathway = pathway or settings.ECG_PATHWAY
        self.device = device or settings.ECG_DEVICE
        self.top_k = top_k if top_k is not None else settings.ECG_TOP_K
        self.work_root = Path(work_root or settings.WORK_ROOT)
        self._model: Any = None
        self._checkpoint: str | None = None

    # -- pipeline stages ------------------------------------------------------------

    def _load_model(self) -> tuple[Any, str]:
        if self._model is None:
            # Imported here, not at module scope: this pulls in torch and a 370 MB
            # checkpoint, and the web process must never pay for it just to serve a
            # status poll.
            from ecg_pipeline.interpret.interpret_ecg import build_model_for

            self._model, self._checkpoint = build_model_for(self.pathway, device=self.device)
            logger.info("loaded %s model for pathway %s", self._checkpoint, self.pathway)
        return self._model, str(self._checkpoint)

    def _digitize(self, study: Study, workdir: Path) -> dict[str, Any] | None:
        """Rectify and digitize. Returns the pipeline's record, or None if nothing came back."""
        from ecg_pipeline import pipeline

        staged = workdir / "input"
        output = workdir / "output"
        staged.mkdir(parents=True, exist_ok=True)

        # Named after the study so that the pipeline's record name -- taken from the image
        # basename -- is the id everything else already uses.
        rectify(study.image.path, study.quad_points, staged / f"{study.id}{RECTIFIED_SUFFIX}")

        results = pipeline.run(
            image_dir=staged,
            output_dir=output,
            lead_layout=lead_layout_for(study.mount),
            skip_interpretation=True,
            upscale="auto",
            quiet=True,
        )
        return results[0] if results else None

    def _interpret(self, csv_path: str) -> dict[str, Any]:
        from ecg_pipeline.interpret.interpret_ecg import interpret_csv

        model, checkpoint = self._load_model()
        return interpret_csv(
            csv_path,
            pathway=self.pathway,
            ckpt=checkpoint,
            k=self.top_k,
            device=self.device,
            model=model,
        )

    # -- orchestration --------------------------------------------------------------

    def run(self, analysis: "analysis_models.Analysis", already_claimed: bool = False) -> None:
        """Take one analysis from queued to ready or failed. Never raises.

        ``already_claimed`` is for a caller that has moved the row to 'processing' itself
        -- which the worker does, atomically, as the way it wins the row against other
        workers. Marking it again would count a second attempt against a study on its
        first.
        """
        study = analysis.study
        workdir = self.work_root / str(study.id)
        if not already_claimed:
            analysis.mark_processing()

        try:
            self._run_inner(analysis, study, workdir)
        except Exception:
            # A crash here is this service's fault, not the image's: 'server-error' is the
            # cause that says so, and the app's UI offers a retry for it.
            logger.exception("analysis failed for study %s", study.id)
            analysis.mark_failed(
                analysis_models.FAILURE_SERVER_ERROR,
                diagnostics=self._diagnostics(stage="runner", detail=_traceback()),
            )

    def _run_inner(self, analysis: "analysis_models.Analysis", study: Study, workdir: Path) -> None:
        from ecg_pipeline.contract import to_analysis

        record = self._digitize(study, workdir)

        if record is None:
            # The digitizer wrote nothing for the only image it was given. That is an
            # image it could not read -- including the grid-not-found case, which raises
            # per-image upstream and arrives here indistinguishable from any other.
            analysis.mark_failed(
                analysis_models.FAILURE_UNREADABLE_IMAGE,
                diagnostics=self._diagnostics(stage="digitize", detail="the digitizer produced no output"),
            )
            return

        csv_path = record.get("source_csv")
        if csv_path:
            csv_path = self._corrected_csv(study, csv_path, workdir)
            record["source_csv"] = csv_path
            self._store_early_signal(analysis, record, csv_path)
            record = self._merge_interpretation(record, csv_path)

        body = to_analysis(record, study_id=str(study.id), completed_at=timezone.now().isoformat())

        diagnostics = self._diagnostics(
            pathway=self.pathway,
            warnings=record.get("warnings", []),
            digitization=record.get("digitization"),
            signal_quality=record.get("signal_quality"),
            signal_bridging=record.get("signal_bridging"),
            calibration_corrected=not study.has_standard_calibration,
            # Where the threshold that produced ``record["flagged"]`` came from -- explicit,
            # ecg-pipeline's own default, or "none" -- straight from ``interpret_csv`` via
            # ``_merge_interpretation``'s spread. None here (rather than the key being absent)
            # means interpretation was never reached, e.g. no CSV to interpret at all.
            threshold_source=record.get("threshold_source"),
        )

        body = self._apply_mount_cross_check(study, record, body, diagnostics)
        analysis.mark_finished(body, diagnostics)

        # A ready analysis has everything it will ever serve in its payload and everything
        # worth knowing about the run in its diagnostics, so its intermediate files (the
        # rectified PNG, the digitizer's CSVs and debug images, about 9 MB per study) are
        # done. A failed one keeps them: the CSV the digitizer actually wrote is what
        # someone comparing against the corrected one, or against the image, will want.
        if analysis.status == analysis_models.STATUS_READY and not settings.ECG_KEEP_WORK:
            self.clean_workdir(str(study.id))

    def _corrected_csv(self, study: Study, csv_path: str, workdir: Path) -> str:
        """Rescale the digitized CSV for a non-standard print. Identity for a standard one.

        The corrected file is written beside the original rather than over it, so that what
        the digitizer actually produced survives for anyone comparing the two.
        """
        if is_standard(study.speed_mm_per_second, study.gain_mm_per_millivolt):
            return csv_path

        destination = workdir / "output" / f"{study.id}_calibrated.csv"
        correct_csv(csv_path, destination, study.speed_mm_per_second, study.gain_mm_per_millivolt)
        logger.info(
            "study %s corrected from %.4g mm/s, %.4g mm/mV to standard",
            study.id,
            study.speed_mm_per_second,
            study.gain_mm_per_millivolt,
        )
        return str(destination)

    def _store_early_signal(self, analysis: "analysis_models.Analysis", record: dict[str, Any], csv_path: str) -> None:
        """Store the digitized trace as soon as it exists, well before interpretation.

        Digitization is the ~15 second stage; interpretation is the ~30 to 55 second one,
        and the one that can still fail. Reading ``csv_path`` after ``_corrected_csv`` means
        the stored signal already carries the calibration correction, the same one the
        finished payload's signal will carry. Built the same way ``to_analysis`` builds its
        own, including the right-sided relabel, so the two never disagree.

        Never allowed to fail the analysis: the early signal is a convenience for whoever
        took the photograph, not the result, and a study must not fail because of it.
        """
        try:
            from ecg_pipeline.contract import signal_bridging_report, to_signal
            from ecg_pipeline.interpret.waveform import load_canonical_csv

            layout = (record.get("digitization") or {}).get("lead_layout", "")
            canonical, names = load_canonical_csv(csv_path)
            signal = to_signal(canonical, names, lead_layout=layout)
            analysis.mark_digitized(signal)
            # What the contract bridged on the way to that signal: digitizer dropouts of a
            # few milliseconds inside a printed stretch (see MAX_BRIDGED_GAP_SECONDS there).
            # Kept in the record so it reaches diagnostics; the trace itself never says.
            record["signal_bridging"] = signal_bridging_report(canonical, names)
        except Exception:
            logger.exception("failed to store the early signal for study %s", analysis.study_id)

    def _merge_interpretation(self, record: dict[str, Any], csv_path: str) -> dict[str, Any]:
        """Interpret, and fold the result into the digitization record.

        Mirrors ``pipeline.run``: the digitization keys are reapplied on top of whatever
        the interpretation returned, and ``degraded`` is the union of both halves plus an
        unidentified layout. Losing that union is how a confident probability on an
        unreadable scan would reach a screen.
        """
        digitization = record.get("digitization")
        preprocessing = record.get("preprocessing")
        digitization_warnings = list(record.get("warnings", []))
        was_degraded = bool(record.get("degraded"))

        try:
            interpreted = self._interpret(csv_path)
        except Exception as exc:
            logger.exception("interpretation failed for %s", csv_path)
            interpreted = {"source_csv": csv_path, "error": f"{type(exc).__name__}: {exc}"}

        merged = {**record, **interpreted}
        merged["source_csv"] = csv_path
        merged["digitization"] = digitization
        merged["preprocessing"] = preprocessing
        merged["warnings"] = _unique(digitization_warnings + list(interpreted.get("warnings", [])))
        # The gate ids are what ``contract.failure_reason`` reads first, and ``pipeline.run``
        # only attaches "interpretation-error" on its own interpretation branch, which this
        # runner does not use. Without it here, a crashed interpretation would still be
        # degraded but its cause would fall through to 'unexpected' instead of
        # 'server-error', which is the one the app offers a retry for.
        gates = list(record.get("gates", []))
        if "error" in interpreted and "interpretation-error" not in gates:
            gates.append("interpretation-error")
        merged["gates"] = gates
        merged["degraded"] = (
            was_degraded
            or bool(interpreted.get("degraded"))
            or "error" in interpreted
            or bool(digitization and digitization.get("lead_layout") == UNKNOWN_LAYOUT)
        )
        return merged

    def _apply_mount_cross_check(
        self,
        study: Study,
        record: dict[str, Any],
        body: dict[str, Any],
        diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        """Refuse a reading whose layout is not the mount the user said they photographed.

        Two independent answers exist to 'which mount is this': the user picked one from a
        menu before the shutter, and the digitizer read one off the image. When they
        disagree, either the framing guide was wrong for the paper or the layout was
        misidentified -- and the second means the leads are about to be served under the
        wrong names. Neither is a reading to hand over.

        'unsupported-mount' is the app's cause for it. The contract only produces that for
        a layout the digitizer could not identify at all, which is the same statement about
        a different failure: we could not read the mount you told us about.
        """
        identified = (record.get("digitization") or {}).get("lead_layout")
        if not layout_disagrees_with_mount(study.mount, identified):
            return body

        logger.warning(
            "study %s declared mount %s but the digitizer identified %s",
            study.id,
            study.mount,
            identified,
        )
        diagnostics["mount_disagreement"] = {"declared": study.mount, "identified": identified}
        return {
            **body,
            "status": analysis_models.STATUS_FAILED,
            "observations": [],
            "failure": analysis_models.FAILURE_UNSUPPORTED_MOUNT,
            "completedAt": None,
        }

    def _diagnostics(self, **fields: Any) -> dict[str, Any]:
        """Every diagnostics dict this runner writes, ready or failed, starts the same way.

        The pipeline version goes on failures too: a study that failed under one version
        and is retried under another is exactly the case where knowing which one it was
        matters.
        """
        return {"pipeline_version": _pipeline_version(), **fields}

    def clean_workdir(self, study_id: str) -> None:
        """Drop a study's intermediate files. The uploaded image is not touched."""
        shutil.rmtree(self.work_root / str(study_id), ignore_errors=True)


def _unique(warnings: list[str]) -> list[str]:
    """Drop repeats, keeping the first occurrence and the order.

    The two halves overlap. ``pipeline.run(skip_interpretation=True)`` already appends the
    quality assessment's warnings to the record, and ``interpret_csv`` assesses quality
    again on the CSV it is handed and returns those same sentences. Concatenating gave
    every coverage complaint twice in the diagnostics -- which reads like two separate
    problems with the same ECG.

    ``pipeline.run``'s own interpretation branch does not hit this: there the digitization
    half is only preprocessing and layout warnings, never quality ones.
    """
    seen: set[str] = set()
    return [w for w in warnings if not (w in seen or seen.add(w))]


def _pipeline_version() -> str:
    """Which ecg-pipeline produced this analysis.

    Recorded per row because the pipeline is a separate repository and moves on its own;
    without this, nothing ties a stored reading to the code that made it.
    """
    import ecg_pipeline

    return str(getattr(ecg_pipeline, "__version__", "unknown"))


def _traceback() -> str:
    import traceback

    return traceback.format_exc(limit=8)
