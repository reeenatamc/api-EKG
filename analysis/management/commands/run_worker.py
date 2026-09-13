"""The worker that actually reads the ECGs.

    python manage.py run_worker

Runs until interrupted, taking queued analyses one at a time. It is a separate process
from the web server on purpose: digitizing and interpreting one ECG is tens of seconds of
CPU with a 370 MB model resident, and doing that inside a request would block a worker
thread for the whole of it and time out on the phone that asked.

Why a database queue and not Celery
-----------------------------------
Because it needs no broker. The states the app polls -- queued, processing, ready, failed
-- are already rows in a table, and a queue on top of them would be a second copy of the
same state that can disagree with the first. ``Analysis.claim_next`` is a compare-and-swap,
so several workers can run against one database safely.

If this ever needs to fan out across machines, the claim is the seam to replace, and
nothing above it changes.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from analysis.models import Analysis
from analysis.runner import PipelineRunner

logger = logging.getLogger(__name__)

# A study still 'processing' after this long belongs to a worker that is gone. It is
# comfortably longer than a slow run on CPU, which is the point: reclaiming a study that
# is merely slow would run it twice.
DEFAULT_STALE_MINUTES = 30


class Command(BaseCommand):
    help = "Process queued ECG analyses until interrupted."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--once", action="store_true", help="Process one study, then exit.")
        parser.add_argument("--study", help="Process this study id only, then exit.")
        parser.add_argument(
            "--reclaim-stale",
            action="store_true",
            help=(
                f"On start, requeue studies stuck in 'processing' for over "
                f"{DEFAULT_STALE_MINUTES} minutes. Only safe when no other worker is running."
            ),
        )
        parser.add_argument("--pathway", help="Override the interpretation pathway for this worker.")

    def handle(self, *args: Any, **options: Any) -> None:
        self._running = True

        # SIGINT and SIGTERM finish the study in hand rather than abandoning it mid-run:
        # a killed worker leaves a row claimed that nothing else will pick up until it goes
        # stale, and the CPU already spent is lost.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._stop)

        runner = PipelineRunner(pathway=options.get("pathway"))
        # Before touching the queue: a worker told to use a GPU it cannot see must refuse to
        # start, not claim a study and fail it as a server error.
        try:
            runner.check_device()
        except (RuntimeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        if options["reclaim_stale"]:
            cutoff = timezone.now() - timedelta(minutes=DEFAULT_STALE_MINUTES)
            reclaimed = Analysis.reclaim_stale(cutoff)
            self.stdout.write(f"Requeued {reclaimed} stale analysis/analyses.")

        if options["study"]:
            self._run_one_by_id(runner, options["study"])
            return

        self.stdout.write(self.style.SUCCESS(f"Worker started (pathway={runner.pathway}, device={runner.device})."))
        self._loop(runner, once=options["once"])
        self.stdout.write("Worker stopped.")

    # -- internals -------------------------------------------------------------------

    def _stop(self, *_: Any) -> None:
        if not self._running:
            # A second interrupt while already finishing: the user means it.
            raise KeyboardInterrupt
        self._running = False
        self.stdout.write("\nFinishing the current study, then stopping. Interrupt again to force.")

    def _loop(self, runner: PipelineRunner, once: bool) -> None:
        from django.conf import settings

        poll = settings.ECG_WORKER_POLL_SECONDS
        while self._running:
            analysis = Analysis.claim_next()
            if analysis is None:
                if once:
                    self.stdout.write("Nothing queued.")
                    return
                time.sleep(poll)
                continue

            self._process(runner, analysis)
            if once:
                return

    def _run_one_by_id(self, runner: PipelineRunner, study_id: str) -> None:
        analysis = Analysis.objects.filter(study_id=study_id).first()
        if analysis is None:
            raise CommandError(f"No analysis has been requested for study {study_id}.")
        analysis.mark_processing()
        self._process(runner, analysis)

    def _process(self, runner: PipelineRunner, analysis: Analysis) -> None:
        started = time.monotonic()
        self.stdout.write(f"Reading study {analysis.study_id} (attempt {analysis.attempts})...")

        # Flushed explicitly, here and after the result. Python buffers stdout when it is
        # not a terminal, so `run_worker > worker.log` showed an empty file for as long as
        # the worker stayed alive -- which for a process designed to run indefinitely means
        # never. Seven studies were read with nothing to watch but an empty log.
        self.stdout.flush()

        # Both callers have already moved the row to 'processing' -- that claim is how a
        # worker wins the row. Letting the runner do it again would count a second attempt.
        runner.run(analysis, already_claimed=True)

        elapsed = time.monotonic() - started
        if analysis.status == "ready":
            observations = len(analysis.payload.get("observations", []) if analysis.payload else [])
            self.stdout.write(self.style.SUCCESS(f"  ready in {elapsed:.1f}s, {observations} observation(s)"))
        else:
            self.stdout.write(self.style.WARNING(f"  {analysis.status} in {elapsed:.1f}s: {analysis.failure}"))
        self.stdout.flush()
