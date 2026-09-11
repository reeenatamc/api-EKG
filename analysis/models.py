"""The analysis job and its result.

One row per study. It holds the state machine the app polls -- queued, processing, ready,
failed -- and, once finished, the ``EcgAnalysis`` body verbatim as
``ecg_pipeline.contract`` produced it. Storing the emitted body rather than rebuilding it
per request means a served result cannot drift from the one that was computed: a change to
the contract module changes new analyses, not old ones.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import models
from django.utils import timezone

from studies.models import Study

# The app's ``AnalysisStatus``, exactly.
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_CHOICES = [
    (STATUS_QUEUED, "Queued"),
    (STATUS_PROCESSING, "Processing"),
    (STATUS_READY, "Ready"),
    (STATUS_FAILED, "Failed"),
]

# The app's ``AnalysisFailureReason``. 'network-unreachable' belongs to the app's adapter
# and never originates here. 'grid-not-detected' is never produced either, and that is
# ecg_pipeline's doing rather than an omission: when the digitizer cannot find the grid it
# raises per-image and writes nothing, which arrives indistinguishable from any other
# unreadable image. See the note in ``ecg_pipeline.contract.failure_reason``.
FAILURE_UNREADABLE_IMAGE = "unreadable-image"
FAILURE_GRID_NOT_DETECTED = "grid-not-detected"
# Read and laid out, but the trace came back too fragmented to interpret: no lead carries
# signal, or none was printed at full length. ecg_pipeline.contract decides it.
FAILURE_TRACE_INCOMPLETE = "trace-incomplete"
FAILURE_UNSUPPORTED_MOUNT = "unsupported-mount"
FAILURE_SERVER_ERROR = "server-error"
FAILURE_UNEXPECTED = "unexpected"

ANALYSIS_FAILURE_REASONS = frozenset(
    {
        FAILURE_UNREADABLE_IMAGE,
        FAILURE_GRID_NOT_DETECTED,
        FAILURE_TRACE_INCOMPLETE,
        FAILURE_UNSUPPORTED_MOUNT,
        FAILURE_SERVER_ERROR,
        FAILURE_UNEXPECTED,
    }
)

# A study that has failed this many times is not going to succeed on the next identical
# attempt. Retries exist for a worker killed mid-run, not for a bad image.
MAX_ATTEMPTS = 3


class Analysis(models.Model):
    """The digitization and interpretation of one study."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.OneToOneField(Study, on_delete=models.CASCADE, related_name="analysis")

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)

    # The EcgAnalysis body as ecg_pipeline.contract emitted it. Null until a run finishes.
    payload = models.JSONField(null=True, blank=True)

    failure = models.CharField(max_length=32, null=True, blank=True)

    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    # Everything the pipeline said that the app's contract has no field for: the
    # digitization warnings, the layout match cost, a traceback. NEVER served -- some of
    # it is internal detail and all of it is prose, which is what the cause-not-message
    # agreement exists to keep off a screen. It is here so that a study that came back
    # 'unexpected' can be explained without re-running it.
    diagnostics = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [models.Index(fields=["status", "requested_at"])]
        verbose_name_plural = "analyses"

    def __str__(self) -> str:
        return f"{self.study_id} [{self.status}]"

    @property
    def is_finished(self) -> bool:
        return self.status in (STATUS_READY, STATUS_FAILED)

    def to_body(self) -> dict[str, Any]:
        """The app's ``EcgAnalysis``.

        A finished analysis serves the stored payload. An unfinished one is built here,
        and every field it cannot yet answer is null or empty rather than absent: the app's
        type has no optional members, and a missing key would land as ``undefined`` where
        it expects ``null``.
        """
        if self.payload is not None:
            return dict(self.payload)
        return {
            "studyId": str(self.study_id),
            "status": self.status,
            "signal": None,
            "measurements": None,
            "observations": [],
            "failure": self.failure,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
        }

    def requeue(self) -> bool:
        """Put a failed analysis back in the queue, at the user's request.

        The app offers a retry on a failed study and had nothing to reach: POST is
        idempotent, so pressing it returned the same failed record and the button looked
        broken. This is the one transition that idempotency was never protecting. A
        queued or processing row must not be touched, because that would be the second
        run the comment on the view warns about, and a ready one must not be discarded.
        A failed row is neither.

        The failure and the timestamps go with it. Leaving the old ones would describe a
        run that is no longer the current one, and ``to_body`` would serve a queued
        analysis carrying the reason it failed last time.

        Attempts go back to zero. What the user is asking for is another try, and the
        counter exists to stop a study that kills the worker from being reclaimed
        forever, not to ration deliberate requests.

        The write is conditional on the row still being failed, for the same reason
        ``claim_next`` is: the app's queue retries, so two of these can arrive together.
        With a plain save, the second would land after the first had requeued the row and a
        worker had claimed it, and put it back to queued with attempts at zero while that
        worker was still running it. A third worker would then claim it again, which is the
        second run the view's comment promises never happens. Returns whether this call was
        the one that requeued it; a False means someone else already did, which for the
        caller is the same outcome.
        """
        requeued = (
            type(self)
            .objects.filter(pk=self.pk, status=STATUS_FAILED)
            .update(
                status=STATUS_QUEUED,
                failure=None,
                payload=None,
                attempts=0,
                started_at=None,
                completed_at=None,
                requested_at=timezone.now(),
                # The previous run's diagnostics go with its failure. They describe why
                # that run failed, and left in place they would sit on a queued row and
                # then, since mark_finished only overwrites them when given some, on a
                # ready one.
                diagnostics={},
            )
        )
        self.refresh_from_db()
        return bool(requeued)

    def mark_processing(self) -> None:
        self.status = STATUS_PROCESSING
        self.started_at = timezone.now()
        self.attempts += 1
        self.save(update_fields=["status", "started_at", "attempts"])

    @classmethod
    def claim_next(cls) -> "Analysis | None":
        """Take the oldest queued analysis, atomically. None when the queue is empty.

        The claim is a conditional update -- 'set processing WHERE status is still queued'
        -- and the row count decides the winner. That is a compare-and-swap, which is what
        makes two workers safe on SQLite, where ``SELECT ... FOR UPDATE SKIP LOCKED`` is
        not available. Without it both would read the same row as queued and run the same
        study twice.
        """
        while True:
            candidate = cls.objects.filter(status=STATUS_QUEUED).order_by("requested_at").first()
            if candidate is None:
                return None
            claimed = cls.objects.filter(pk=candidate.pk, status=STATUS_QUEUED).update(
                status=STATUS_PROCESSING, started_at=timezone.now(), attempts=models.F("attempts") + 1
            )
            if claimed:
                candidate.refresh_from_db()
                return candidate
            # Another worker took it between the read and the update. Look again.

    @classmethod
    def reclaim_stale(cls, older_than: datetime) -> int:
        """Return long-running 'processing' rows to the queue.

        A worker killed mid-study leaves its row claimed forever, and nothing else will
        ever pick it up. Rows past ``MAX_ATTEMPTS`` are left alone: at that point the study
        is failing repeatably and requeueing it just burns a CPU core on the same crash.
        """
        return cls.objects.filter(
            status=STATUS_PROCESSING, started_at__lt=older_than, attempts__lt=MAX_ATTEMPTS
        ).update(status=STATUS_QUEUED)

    def mark_finished(self, payload: dict[str, Any], diagnostics: dict[str, Any] | None = None) -> None:
        """Record a completed run.

        The status is taken from the payload rather than passed separately, because
        ``ecg_pipeline.contract.to_analysis`` has already made that judgement: a degraded
        result comes back 'failed' there, and deciding it a second time here is how the two
        would eventually disagree.
        """
        self.payload = payload
        self.status = payload.get("status", STATUS_FAILED)
        self.failure = payload.get("failure")
        completed = payload.get("completedAt")
        self.completed_at = timezone.now()
        if completed is None and self.status == STATUS_READY:
            # to_analysis only stamps completedAt on a ready result; keep them consistent.
            self.payload["completedAt"] = self.completed_at.isoformat()
        if diagnostics:
            self.diagnostics = diagnostics
        self.save(update_fields=["payload", "status", "failure", "completed_at", "diagnostics"])

    def mark_failed(self, reason: str, diagnostics: dict[str, Any] | None = None) -> None:
        """Record a run that never produced a contract body at all."""
        if reason not in ANALYSIS_FAILURE_REASONS:
            raise ValueError(f"{reason!r} is not an AnalysisFailureReason; see analysis/models.py")
        self.status = STATUS_FAILED
        self.failure = reason
        self.completed_at = timezone.now()
        self.payload = {
            "studyId": str(self.study_id),
            "status": STATUS_FAILED,
            "signal": None,
            "measurements": None,
            "observations": [],
            "failure": reason,
            "completedAt": None,
        }
        if diagnostics:
            self.diagnostics = diagnostics
        self.save(update_fields=["payload", "status", "failure", "completed_at", "diagnostics"])
