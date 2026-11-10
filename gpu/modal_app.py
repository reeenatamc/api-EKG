"""The GPU half of the worker, deployed on Modal.

    modal deploy gpu/modal_app.py

The Django worker keeps the queue, the rectification, the calibration correction, the
contract and the mount cross-check (see analysis/modal_runner.py). Only the two stages
that need a GPU run here: digitizing the rectified photograph and interpreting the CSV.
That keeps this app free of the database, of Django and of any secret.

The image is the same recipe as docker/Dockerfile.worker-gpu -- torch 2.2.2 cu121, the
inference requirements, ecg-pipeline and Open-ECG-Digitizer pinned with its patches --
built with Modal's own methods rather than from that Dockerfile, which copies Django,
switches USER and brings its own CMD, none of which Modal wants. Bump the two refs below
together with the ones in the Dockerfiles.

One study per container, at most ten containers: the Starter plan allows ten GPUs at
once, which is one per tester in a class of ten. Retries stay off here because the
Django side decides them (Analysis.attempts); retrying on both sides would count a study
twice.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import modal

APP_NAME = "ekg-gpu"
CLASS_NAME = "Analizador"

ECG_PIPELINE_REF = "v0.1.5"
DIGITIZER_COMMIT = "963387f"
DIGITIZER_HOME = "/opt/open-ecg-digitizer"
PIPELINE_SRC = "/opt/ecg-pipeline-src"
WEIGHTS_DIR = "/opt/weights"

# Same values as the Django settings' defaults (ECG_PATHWAY, ECG_TOP_K). The worker passes
# its own on every call, so these only matter for the warm-up.
DEFAULT_PATHWAY = "rhythm"

MAX_GPUS = 10

_REQUIREMENTS = Path(__file__).resolve().parent.parent / "docker" / "requirements-inference.txt"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "git-lfs", "curl", "ca-certificates")
    .run_commands("git lfs install --skip-repo")
    .pip_install(
        "torch==2.2.2",
        "torchvision==0.17.2",
        index_url="https://download.pytorch.org/whl/cu121",
    )
    .pip_install_from_requirements(str(_REQUIREMENTS))
    # ecg-pipeline's own runtime dependencies, as its pyproject pins them; it is then
    # installed with --no-deps so pip cannot move the torch/numpy pair above.
    .pip_install("numpy>=1.26,<2", "scipy>=1.12,<1.16", "pillow>=10.2", "matplotlib>=3.8", "pyyaml>=6.0")
    .run_commands(
        f"git clone --branch {ECG_PIPELINE_REF} --depth 1 https://github.com/reeenatamc/ecg-pipeline.git {PIPELINE_SRC}",
        f"pip install --no-deps {PIPELINE_SRC}",
        f"ECGFOUNDER_WEIGHTS_DIR={WEIGHTS_DIR} bash {PIPELINE_SRC}/scripts/download_weights.sh",
        f"git clone https://github.com/Ahus-AIM/Open-ECG-Digitizer.git {DIGITIZER_HOME}",
        f"git -C {DIGITIZER_HOME} checkout {DIGITIZER_COMMIT}",
        f"git -C {DIGITIZER_HOME} lfs pull",
        f'for p in {PIPELINE_SRC}/patches/*.patch; do echo "applying $p"; git -C {DIGITIZER_HOME} apply "$p"; done',
        f"rm -rf {PIPELINE_SRC} {DIGITIZER_HOME}/.git",
    )
    .env(
        {
            "OPEN_ECG_DIGITIZER_HOME": DIGITIZER_HOME,
            "ECGFOUNDER_WEIGHTS_DIR": WEIGHTS_DIR,
            "ECG_DEVICE": "cuda",
            # The digitizer stays up between studies instead of reloading torch each time.
            "ECG_DIGITIZER_MODE": "persistent",
            # Nothing reads the debug PNG and it costs about 6 s per study.
            "ECG_DIGITIZER_DEBUG_PNG": "0",
        }
    )
)

app = modal.App(APP_NAME)


def _plain(value: Any) -> Any:
    """Make a result safe to send back: JSON types only, no numpy scalars, no Paths.

    The caller has no torch and must not need this container's exact library versions to
    unpickle what it gets, so everything crosses as plain data.
    """

    def default(obj: Any) -> Any:
        if hasattr(obj, "item"):
            return obj.item()
        if hasattr(obj, "tolist"):
            return obj.tolist()
        return str(obj)

    return json.loads(json.dumps(value, default=default))


@app.cls(
    image=image,
    gpu="T4",
    cpu=2.0,
    memory=10240,  # MiB; one study peaks around 7.5 GB
    timeout=300,
    scaledown_window=300,  # five idle minutes, so the second study of a class starts warm
    max_containers=MAX_GPUS,
    retries=0,
)
class Analizador:
    @modal.enter()
    def cargar(self) -> None:
        from ecg_pipeline import devices
        from ecg_pipeline.interpret.interpret_ecg import build_model_for

        devices.require_device("cuda")
        self.models: dict[str, tuple[Any, str]] = {}
        self.models[DEFAULT_PATHWAY] = build_model_for(DEFAULT_PATHWAY, device="cuda")

    def _model(self, pathway: str) -> tuple[Any, str]:
        if pathway not in self.models:
            from ecg_pipeline.interpret.interpret_ecg import build_model_for

            self.models[pathway] = build_model_for(pathway, device="cuda")
        return self.models[pathway]

    @modal.method()
    def digitalizar(self, png: bytes, nombre: str, lead_layout: str | None) -> dict[str, Any]:
        """Digitize one rectified photograph. Returns the record and every output file.

        Paths inside the record point into this container; ``remote_root`` says where, so
        the caller can map them onto its own work directory.
        """
        from ecg_pipeline import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "input").mkdir()
            (root / "input" / nombre).write_bytes(png)
            results = pipeline.run(
                image_dir=root / "input",
                output_dir=root / "output",
                lead_layout=lead_layout,
                skip_interpretation=True,
                upscale="auto",
                quiet=True,
                device="cuda",
            )
            files = {}
            output = root / "output"
            if output.exists():
                for path in output.rglob("*"):
                    if path.is_file():
                        files[str(path.relative_to(output))] = path.read_bytes()
            return {
                "record": _plain(results[0]) if results else None,
                "remote_root": str(root),
                "files": files,
            }

    @modal.method()
    def interpretar(self, csv_text: str, csv_name: str, pathway: str, top_k: int) -> dict[str, Any]:
        from ecg_pipeline.interpret.interpret_ecg import interpret_csv

        model, checkpoint = self._model(pathway)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / csv_name
            path.write_text(csv_text)
            result = interpret_csv(str(path), pathway=pathway, ckpt=checkpoint, k=top_k, device="cuda", model=model)
            result = _plain(result)
            result["source_csv"] = csv_name
            return result

    @modal.method()
    def calentar(self) -> str:
        """No-op: calling it is how a container gets started ahead of a class."""
        return "listo"

    @modal.exit()
    def cerrar(self) -> None:
        try:
            from ecg_pipeline.digitizer import close_persistent_digitizer

            close_persistent_digitizer()
        except ImportError:
            pass
