# api-EKG

The HTTP service between the app and the models.

```
[app-EKG]  ──HTTP──>  [api-EKG]  ──imports──>  [ecg-pipeline]  ──subprocess──>  [Open-ECG-Digitizer]
 Expo / React Native   this repo               digitize + interpret            third party (CC BY-SA)
```

It owns accounts, uploads, and the job queue. It owns **no clinical logic**: what an ECG
means is decided in `ecg-pipeline`, and how a result is worded is decided in the app. Two
things happen here that belong nowhere else, and both are about the photograph rather than
the diagnosis, perspective rectification and calibration correction. See
[What this service actually computes](#what-this-service-actually-computes).

---

## Environment

**Everything runs in one interpreter, and it is the digitizer's virtualenv.** That is not a
shortcut. The worker imports `ecg_pipeline`, which imports torch and numpy under a pinned
ABI: torch 2.2.x is built against the numpy 1.x C ABI and fails at import under numpy 2.x.
Relax one bound and you must relax the other. The validated stack is Python 3.12, numpy
1.26, torch 2.2.2, CPU, on Intel macOS.

Django, DRF and django-cors-headers are pure Python with no binary dependencies, which is
why adding them to that environment cannot disturb it.

```bash
VENV=../Open-ECG-Digitizer/venv/bin/python

$VENV -m pip install -r requirements.txt
$VENV -m pip install -e ../ecg-pipeline --no-deps   # --no-deps: everything is already there
```

`--no-deps` matters. Without it pip re-resolves torch and numpy and can quietly move the
pinned pair.

The version this service is built against is the tag CI installs, `v0.1.0` in
`.github/workflows/ci.yml`. An editable checkout on another commit works, but a reading
made with it is not one the suite here vouched for; every analysis records the pipeline
version it ran under in its `diagnostics`, so a stored result can always be traced back.

Verify:

```bash
cd /tmp && $VENV -c "import numpy, torch, django, ecg_pipeline; print(numpy.__version__, torch.__version__)"
# 1.26.4 2.2.2
```

Run it from `/tmp` on purpose: from inside `ecg-pipeline` the import succeeds through the
working directory whether or not it is installed, which is how you convince yourself of
something that is not true.

If installing `ecg-pipeline` is not an option on some machine, set `ECG_PIPELINE_HOME` to
its checkout and `config/settings.py` will put it on `sys.path`. That is a fallback, not
the route.

### Weights

The pipeline needs the ECGFounder checkpoints in `ecg-pipeline/weights/` and the
digitizer's own in `Open-ECG-Digitizer/weights/`. Neither is in any repository. The
digitizer checkout is found via `$OPEN_ECG_DIGITIZER_HOME`, or as a sibling directory.

---

## Running

```bash
cp .env.example .env
$VENV manage.py migrate
$VENV manage.py createsuperuser        # optional, for /admin

$VENV manage.py runserver 0.0.0.0:8000  # terminal 1: the API
$VENV manage.py run_worker              # terminal 2: the models
```

Two processes on purpose. Reading one ECG is tens of seconds of CPU with a 370 MB model
resident; doing it inside a request blocks a worker thread for the whole of it and times
out the phone that asked. The app is built to poll precisely so it does not have to wait.

`0.0.0.0` because a phone reaches the laptop by LAN address. Set `API_BASE_URL` in the
app's `.env` to that address, `10.0.2.2` for the Android emulator.

In development, verification codes are printed to the terminal running the server: the
email backend is Django's console backend. Wiring real SMTP is a deployment setting
(`EMAIL_BACKEND`, `EMAIL_HOST_*`), not a code change.

### The worker

```bash
$VENV manage.py run_worker                    # run until interrupted
$VENV manage.py run_worker --once             # one study, then exit
$VENV manage.py run_worker --study <uuid>     # a specific study
$VENV manage.py run_worker --reclaim-stale    # requeue studies a dead worker left claimed
```

The queue is the `Analysis` table, not Celery, because the states the app polls, queued,
processing, ready, failed, are already rows in it. A broker on top would be a second copy
of the same state that can disagree with the first. `Analysis.claim_next` is a
compare-and-swap, so several workers against one database are safe, including on SQLite
where `SELECT ... FOR UPDATE SKIP LOCKED` is unavailable. If this ever needs to fan out
across machines, that claim is the seam to replace and nothing above it changes.

---

## Endpoints

Each one serves a method on a contract in the app. The paths are named after the methods so
the adapter reads as a transcription.

### `AuthService`

| Method | Endpoint | Success |
|---|---|---|
| `register` | `POST /auth/register/` | `{email, expiresInSeconds}` |
| `verifyCode` | `POST /auth/verify/` | `{token, session:{userId,email,role}}` |
| `signIn` | `POST /auth/sign-in/` | `{token, session:{...}}` |
| `requestPasswordReset` | `POST /auth/password-reset/` | `{email, expiresInSeconds}` |
| `signOut` | `POST /auth/sign-out/` | `204` |
| n/a | `GET /auth/session/` | `{session:{...}}` |

`GET /auth/session/` has no counterpart in the contract: `restoreSession` reads the app's
own secure storage. It is there so the adapter can discover that a stored session is dead
 signed out on another device, instead of finding out on the next upload.

The token travels beside `session` rather than inside it because `Session` has no field for
one. The app models what its screens need, and no screen needs a credential; storing it is
the adapter's job. Send it back as `Authorization: Token <key>`.

### `UploadService`

`POST /studies/`, multipart: `image`, `imageWidth`, `imageHeight`, `metadata` (JSON, the
app's `StudyMetadata` verbatim). Returns the app's `UploadReceipt`: `{remoteId, receivedAt}`.

### `EcgAnalysisService`

| Method | Endpoint |
|---|---|
| `request` | `POST /studies/{id}/analysis/` |
| `get` | `GET /studies/{id}/analysis/`, `404` is the contract's `null` |

Both answer a complete `EcgAnalysis`. `POST` is idempotent: the app's queue retries, and a
second request must not start a second run or discard a finished one.

### Refusals are causes, not messages

Every refusal is `{"reason": "<one of the app's failure reasons>"}`. This is the server
half of an agreement the app states in `AuthService.ts`: the service returns a cause and
the interface decides how it is told, so no "Error 401" can reach a screen and the wording
lives in one place.

The vocabularies are in `accounts/failures.py`, `studies/failures.py` and
`analysis/models.py`. Each refuses to emit a string outside its set, a typo would
otherwise arrive as an unrecognised reason, render as generic copy, and hide the real
cause behind a shrug. Tests assert each set is a subset of the app's union, so a reason
added on one side without the other fails the suite rather than QA.

Two reasons are never produced here. `network-unreachable` belongs to the app's own
adapter: a request that arrived is evidence the network worked. `grid-not-detected` is
`ecg-pipeline`'s doing, when the digitizer cannot find the grid it raises per-image and
writes nothing, which arrives indistinguishable from any other unreadable image.

---

## What this service actually computes

Two things, both about the photograph.

### Perspective rectification (`analysis/rectify.py`)

The app sends the four corners of the paper, not a corrected image, and says why in
`camera/homography.ts`: correcting on the phone means resampling on a mid-range GPU, and
resampling is exactly where a one-millimetre trace disappears. The server has the image at
native resolution and better filters, so the warp happens here, bicubic, written out as
lossless PNG, because JPEG's chroma subsampling averages colour over 2×2 blocks and that is
the size of the fine grid lines the digitizer measures.

A quad that is already the whole frame is passed through untouched. Warping it anyway would
resample every pixel to achieve nothing, and not resampling is the entire point.

The quad is validated hard in `studies/serializers.py`, bounds, convexity, minimum area 
because a homography from a self-crossing or misplaced quad **does not fail**. It rectifies
the wrong region into something that still looks like an ECG and digitizes into a plausible
trace. No later stage notices, so this is the stage that has to.

### Calibration correction (`analysis/calibration.py`)

The digitizer assumes 25 mm/s and 10 mm/mV, `mv_per_mm` is a default argument upstream and
nothing overrides it. The app correctly lets the user say otherwise, because half speed and
half amplitude are real settings on real machines.

Uncorrected, a 50 mm/s record is reported at twice its true heart rate and a 20 mm/mV
record at half its true amplitude. Measured on `normal_input.png`, declaring 50 mm/s and
20 mm/mV:

| | as standard | corrected for 50 mm/s, 20 mm/mV |
|---|---|---|
| duration | 10.0 s | 5.0 s |
| lead I window | 0.00–2.48 s | 0.00–1.24 s |
| top reading | SINUS RHYTHM 0.993 | SINUS TACHYCARDIA 0.934 |

The reading changes, and the changed one is right: the same paper at double speed means a
heart beating at double the rate. Uncorrected, the model calls a tachycardic patient normal.

The correction is applied to the signal before interpretation, not to the emitted result,
so quality assessment, the model and the contract all see one consistent record. Amplitude
is an exact scalar. Time is a resampling, applied **per continuous run**, never across a
NaN. That rule is from the app's `signal.ts`: on a 3×4 print each grid lead exists for 2.5
of the 10 seconds, and a line drawn across the other 7.5 is a credible-looking claim about
a heart nobody recorded. A resampler run over a whole lead draws exactly that line.

This is why `runner.py` does not simply call `pipeline.run`: the correction has to land
between digitization and interpretation, and that function does both in one pass.

### The mount cross-check

Two independent answers exist to "which mount is this": the user picked one from a menu
before the shutter, and the digitizer read one off the image. When they disagree across
*families* the reading is refused as `unsupported-mount`, because a layout that puts
different lead names in the same grid positions means every trace is about to be served
under the wrong name. The case that matters most is the right-sided 3×3: the digitizer
fills the V4/V5/V6 slots by position, and `ecg_pipeline.contract` relabels them to
V4R/V5R/V6R only for that exact layout.

Within a family it is not a disagreement. Whether the print also carries a rhythm strip
changes no lead name, and the mount menu is a framing guide that never asked. An earlier
version of this check insisted on an exact match and refused a perfectly readable
3×4-with-rhythm-strip framed as a plain 3×4.

---

## Privacy

**There is no patient name field, and its absence is the design.** `study.ts` states the
reason: a photograph of an ECG with a name beside it stops being an anonymous clinical
datum and becomes a medical record, with everything that drags in. The app never asks. This
schema has nowhere to put one. `anonymous_id` is the user's own case code, which is what
replaces it. A test asserts the absence, because a nullable `patient_name` added "just in
case" would be filled by somebody eventually.

Uploaded images and pipeline artifacts are gitignored. They are clinical images of real
recordings and must never reach a remote.

---

## Known gaps

Real limits, not TODOs. Each needs a decision rather than a patch.

**`measurements` is always `null`.** Rate, PR, QRS, QT, QTc, axis. The pipeline does not
delineate waves, and the app's `EcgMeasurements` is all-or-nothing, so there is no honest
partial answer, filling PR and QT with anything would be inventing measurements of a
patient. Closing this means wave delineation in `ecg-pipeline`, not a change here.

**`AnalysisFailureReason` has no case for "digitized, but too poor to read".** A degraded
result currently lands on `unexpected`, which is not what happened. The fix is a new reason
in the app's union; the server side is one constant.

**There is no set-a-new-password endpoint.** `AuthService` has one `verifyCode` and no
method for submitting a password, so a reset code is redeemed through the same call and the
answer is a session, code-based sign-in rather than a reset. Adding a change-password
screen means adding an endpoint for it, not overloading `verify`.

**Sign-in distinguishes `account-not-found` from `credentials-mismatch`,** which is the
app's union as written and lets the screen offer to register instead. It also tells an
anonymous caller whether an address is registered. `ACCOUNT_ENUMERATION_IS_ACCEPTABLE` in
`accounts/views.py` turns it off in one line, with no change on the app's side. Password
reset never leaks regardless, it answers identically either way and is the endpoint an
attacker would actually probe.

**Interpretation runs whatever pathway `ECG_PATHWAY` names**, defaulting to `rhythm`. Read
`ecg-pipeline`'s README before changing it: `12lead` is known to over-call pathology and is
degraded unconditionally there.

---

## Tests

```bash
$VENV manage.py test           # 117 tests, ~2s
```

They cover the failure vocabularies, quad validation, the homography's direction, the
calibration arithmetic and its gap handling, the queue's compare-and-swap, and the mount
cross-check. What they do not cover is a real image through the digitizer and ECGFounder:
that needs both sets of weights and tens of seconds per case, which makes it an integration
test. `PipelineRunner._interpret` is patched where the merge logic is exercised.

The suite runs with MD5 password hashing (`config/test_runner.py`). PBKDF2 took it from
two seconds to half a minute, and a suite that slow stops being run.
