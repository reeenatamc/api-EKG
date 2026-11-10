"""Running a study with its GPU stages on Modal (ECG_BACKEND=modal).

Only ``_digitize`` and ``_interpret`` change. Rectifying the photograph, the calibration
correction, the early signal, the contract and the mount cross-check all stay here and run
exactly as in ``PipelineRunner`` -- which is why this subclasses it rather than copying
``_run_inner``. The other half is gpu/modal_app.py.

The worker needs no torch and no weights in this mode: ``ecg_pipeline.contract`` and the
waveform loader only import numpy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from django.conf import settings

from analysis.rectify import RECTIFIED_SUFFIX, rectify
from analysis.runner import PipelineRunner
from studies.models import Study
from studies.mounts import lead_layout_for

logger = logging.getLogger(__name__)

MODAL_APP = "ekg-gpu"
MODAL_CLASS = "Analizador"


def remote_analyzer() -> Any:
    """A handle on the deployed Modal class. Imported lazily: only this mode needs the SDK."""
    import modal

    return modal.Cls.from_name(MODAL_APP, MODAL_CLASS)()


class ModalRunner(PipelineRunner):
    def __init__(self, remote: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._remote = remote

    @property
    def remote(self) -> Any:
        if self._remote is None:
            self._remote = remote_analyzer()
        return self._remote

    def check_device(self) -> str:
        """The GPU is on the other side; what can fail here is the token or the deploy."""
        if not (settings.MODAL_TOKEN_ID and settings.MODAL_TOKEN_SECRET):
            raise RuntimeError("ECG_BACKEND=modal needs MODAL_TOKEN_ID and MODAL_TOKEN_SECRET in .env.")
        self.device = "modal:T4"
        return self.device

    def _digitize(self, study: Study, workdir: Path) -> dict[str, Any] | None:
        staged = workdir / "input"
        output = workdir / "output"
        staged.mkdir(parents=True, exist_ok=True)

        name = f"{study.id}{RECTIFIED_SUFFIX}"
        rectify(study.image.path, study.quad_points, staged / name)

        result = self.remote.digitalizar.remote((staged / name).read_bytes(), name, lead_layout_for(study.mount))

        # Every output file comes back, so a failed study keeps its CSVs here for whoever
        # looks into it, the same as when the worker digitized locally.
        output.mkdir(parents=True, exist_ok=True)
        for relative, content in (result.get("files") or {}).items():
            target = (output / relative).resolve()
            if output.resolve() not in target.parents:
                continue  # never write outside the study's own directory
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        record = result.get("record")
        if record is None:
            return None
        return _relocate(record, result["remote_root"], workdir)

    def _interpret(self, csv_path: str) -> dict[str, Any]:
        path = Path(csv_path)
        interpreted = self.remote.interpretar.remote(path.read_text(), path.name, self.pathway, self.top_k)
        interpreted["source_csv"] = csv_path
        return interpreted


def _relocate(value: Any, remote_root: str, local_root: Path) -> Any:
    """Map every path under the container's temp directory onto the study's workdir."""
    if isinstance(value, str) and value.startswith(remote_root):
        return str(local_root) + value[len(remote_root) :]
    if isinstance(value, dict):
        return {k: _relocate(v, remote_root, local_root) for k, v in value.items()}
    if isinstance(value, list):
        return [_relocate(v, remote_root, local_root) for v in value]
    return value
