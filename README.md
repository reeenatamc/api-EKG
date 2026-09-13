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

The version this service is built against is the tag CI installs, `v0.1.4` in
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
| n/a | `DELETE /auth/account/` | `204` |

`GET /auth/session/` has no counterpart in the contract: `restoreSession` reads the app's
own secure storage. It is there so the adapter can discover that a stored session is dead
 signed out on another device, instead of finding out on the next upload.

The token travels beside `session` rather than inside it because `Session` has no field for
one. The app models what its screens need, and no screen needs a credential; storing it is
the adapter's job. Send it back as `Authorization: Token <key>`.

`DELETE /auth/account/` has no counterpart in the contract either, and needs one added to
the app: Google Play requires that an app offering in-app account creation also let a user
erase it from inside the app. `Authorization: Token <key>` is the only thing this needs --
no body, no confirmation beyond the token itself, the same proof `signOut` already trusts.
It deletes the account and, cascading from it, every one of the user's studies and analyses,
removing their image and work files from disk on the way out (`studies/signals.py`). A
failure answers the same shape as every other refusal here, `{"reason": "unexpected"}`, and
leaves the account untouched.

### Health check

`GET /healthz/`, no credential, no throttle. `{"status": "ok"}` (200) when the database
answers, `{"status": "error"}` (503) when it does not. Nothing else in the app's contract
calls it; it exists for Docker's own healthcheck (`docker-compose.yml`) and a free external
uptime monitor (see "Despliegue").

### `UploadService`

`POST /studies/`, multipart: `image`, `imageWidth`, `imageHeight`, `metadata` (JSON, the
app's `StudyMetadata` verbatim). Returns the app's `UploadReceipt`: `{remoteId, receivedAt}`.

The image is capped at `STUDY_MAX_UPLOAD_SIZE_BYTES` (25 MB by default, above the 4-15 MB a
phone photo actually runs) and `STUDY_MAX_UPLOAD_PIXELS` (50 megapixels), both checked in
`studies/serializers.py` and both refused as `payload-rejected`. Neither limit is Pillow's
own -- Pillow's `DecompressionBombError` only fires around 178 megapixels, a backstop for
arbitrary callers rather than this contract, and it has nothing to say about bytes at all.

### `EcgAnalysisService`

| Method | Endpoint |
|---|---|
| `request` | `POST /studies/{id}/analysis/` |
| `get` | `GET /studies/{id}/analysis/`, `404` is the contract's `null` |
| n/a | `GET /studies/{id}/signal/` |

Both `request` and `get` answer a complete `EcgAnalysis`. `POST` is idempotent: the app's
queue retries, and a second request must not start a second run or discard a finished one.

`GET /studies/{id}/signal/` has no counterpart in the contract either, same reason as
`GET /auth/session/` above: it is a convenience for a client that only wants the trace, not
the whole `EcgAnalysis` shape. The signal it answers is exactly `EcgAnalysis.signal`, and it
exists well before the rest of the body does -- digitization is the ~15 second stage,
interpretation the ~30 to 55 second one, so `analysis/get` keeps returning a `processing`
body with a populated `signal` while this and that agree, and this stays populated on a
failure that happened after digitization. It 404s until a signal is stored, the same owner
filtering as `analysis/get`.

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

### Rate limiting

`register`, `verify_code`, `sign_in` and `request_password_reset` take no credential to
prove who is asking. `verify_code` already caps how many wrong guesses one code tolerates
(`MAX_VERIFICATION_ATTEMPTS`), but nothing else stopped a client from hammering `sign_in`
with passwords or making `register`/`request_password_reset` send unlimited email, so all
four are throttled by IP (`accounts/throttling.py`), in two scopes set by
`DEFAULT_THROTTLE_RATES` and overridable with `AUTH_THROTTLE_RATE` and
`AUTH_EMAIL_THROTTLE_RATE`. A throttled request still answers a cause, not DRF's default
`{"detail": "..."}` (`accounts/exceptions.py`) -- there is no reason in the app's union for
"you are rate limited", so it is `unexpected`.

DRF keeps the request history behind this in the default cache. `LocMemCache` is fine for
the one process this service runs as today; a deployment that runs several needs `CACHES`
pointed at something shared (Redis, Memcached) or each process only ever sees its own share
of the traffic.

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
$VENV manage.py test           # 174 tests, ~3s
```

They cover the failure vocabularies, quad validation, the homography's direction, the
calibration arithmetic and its gap handling, the queue's compare-and-swap, the mount
cross-check, the throttling and upload-size limits above, the health check, and that
deleting a study or an account takes its files with it. What they do not cover is a
real image through the digitizer and ECGFounder: that needs both sets of weights and tens
of seconds per case, which makes it an integration test. `PipelineRunner._interpret` is
patched where the merge logic is exercised.

The suite runs with MD5 password hashing (`config/test_runner.py`). PBKDF2 took it from
two seconds to half a minute, and a suite that slow stops being run. The same runner turns
the auth throttle rates off by default, for the same reason: the suite calls those
endpoints from one address far more than the production rate allows within the minute a
run takes. Tests that exercise throttling itself turn a rate back on with
`override_settings`. The same runner also points MEDIA_ROOT and WORK_ROOT at a fresh
temporary directory for the run, so a test never writes under this checkout's `media/` or
`work/`.

---

## Despliegue

Un solo servidor Linux, Docker Compose, cuatro contenedores: `caddy` (HTTPS automático),
`api` (gunicorn), `worker` (réplicas del pipeline) y `db` (Postgres 16). `Dockerfile` en la
raíz construye una única imagen para `api` y `worker`; `docker-compose.yml`, `Caddyfile` y
`.env.example` están junto a él. Pensado para el piloto de un mes descrito en el informe de
despliegue: una VM de 8 vCPU y 16 GB (UTPL o Hetzner CX43) sostiene dos workers.

### En el servidor (Ubuntu 24.04 o similar)

1. Docker Engine y el plugin Compose (`docker compose version` debe dar 2.20 o más nuevo,
   para que `deploy.replicas` y `deploy.resources.limits` de `docker-compose.yml` se
   apliquen fuera de un Swarm).
2. DNS: el dominio o subdominio del piloto apuntando a la IP del servidor, y los puertos
   80 y 443 abiertos en el firewall (Caddy los necesita para el reto HTTP-01 de Let's
   Encrypt).
3. Clonar el repositorio y pararse en la rama que se vaya a desplegar:

   ```bash
   git clone <url-del-repo> api-EKG && cd api-EKG
   ```

4. Configurar el entorno:

   ```bash
   cp .env.example .env
   ```

   Editar `.env`: `SECRET_KEY` (generarla con el comando que el propio archivo indica),
   `DEBUG=0` (o dejarlo sin definir, que es lo mismo), `ALLOWED_HOSTS` y
   `CSRF_TRUSTED_ORIGINS` con el dominio, `DATABASE_ENGINE=postgresql` y las `POSTGRES_*`,
   `DOMAIN` (lo usan Caddy y Django), `ECGFOUNDER_WEIGHTS_DIR=/data/weights`, las
   `EMAIL_*` de Resend (o el proveedor SMTP que se use), y `WORKER_REPLICAS` /
   `WORKER_MEMORY_LIMIT` / `OMP_NUM_THREADS` según los vCPU de la máquina (ver los
   comentarios de cada variable en `.env.example`, que son la referencia completa).

5. Construir y levantar:

   ```bash
   docker compose build
   docker compose up -d
   ```

   El primer arranque tarda: `worker` descarga los pesos de ECGFounder (~700 MB) al
   volumen `weights_data` con el script de `ecg-pipeline` (`docker/entrypoint-worker.sh`),
   y `api` corre `migrate` y `collectstatic` antes de servir (`docker/entrypoint-api.sh`).
   Seguir el progreso con `docker compose logs -f worker api`.

6. Crear la superusuaria del admin, una sola vez:

   ```bash
   docker compose exec api python manage.py createsuperuser
   ```

7. Verificar:

   ```bash
   curl -f https://$DOMINIO/healthz/          # {"status": "ok"}
   docker compose ps                          # todo "healthy" o "running"
   ```

   Un monitor externo gratuito (UptimeRobot o similar) sobre `GET /healthz/` avisa si el
   servicio o la base de datos caen; el endpoint no pide credencial ni cuenta contra
   ningún límite de peticiones (ver "Health check" arriba).

### Worker con GPU

Opcional y sin plataforma decidida. `ECG_DEVICE=cuda` hace que el worker corra todo el
análisis en la GPU: el digitalizador (sus dos redes) y ECGFounder. Requiere ecg-pipeline
0.1.5 o posterior y un torch con CUDA. Si el worker no ve la GPU, `run_worker` se niega a
arrancar con la causa, en vez de tomar un estudio y fallarlo.

- `docker/Dockerfile.worker-gpu` construye solo el worker: CUDA 12.1 sobre Ubuntu 22.04,
  Python 3.12, torch 2.2.2 con CUDA 12.1 (la misma versión que la imagen CPU),
  ecg-pipeline `v0.1.5` y `ECG_DEVICE=cuda` por defecto. La API sigue usando la imagen CPU.

  ```bash
  docker build -f docker/Dockerfile.worker-gpu -t api-ekg-worker-gpu .
  ```

- `docker-compose.gpu.yml` es un override para una sola máquina con GPU propia (driver de
  NVIDIA y NVIDIA Container Toolkit instalados); una plataforma que asigna la GPU por su
  cuenta usa la imagen directamente:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
  ```

Medido en Colab con una T4 (notebook `scripts/medir_gpu_colab.ipynb` de ecg-pipeline): unos
17 s por estudio en caliente y 26 s en frío, frente a 95 s en la CPU de esa máquina, con la
misma calidad de digitalización en 24 de 24 imágenes. La imagen no se ha construido ni
probado en una GPU real todavía.

### Actualizar una versión desplegada

```bash
git pull
docker compose build
docker compose up -d --no-deps api worker
```

`api` corre sus migraciones al arrancar; `worker` termina el estudio que tenga en curso
antes de detenerse (`stop_grace_period: 3m` en `docker-compose.yml`, y ver el docstring de
`run_worker` sobre por qué SIGTERM no lo interrumpe a mitad de análisis).

### Copias de seguridad

```bash
./scripts/backup.sh /ruta/fuera/del/disco/de/la/vm     # pg_dump + tar de media/
```

Guarda un volcado de Postgres comprimido y un `tar` de `media/` (las imágenes originales;
`work/` son intermedios, prescindibles). El resultado sale sin cifrar: cifrarlo con `age`
o `restic` antes de sacarlo de la máquina es responsabilidad de quien lo programe, porque
la clave no debe vivir en este repositorio. Una entrada de `cron` diaria más ese cifrado es
la copia que el informe de despliegue pide.

Restaurar, y **probarlo antes de necesitarlo de verdad** (contra un proyecto Compose
descartable, no el que está en producción):

```bash
./scripts/restore.sh db-STAMP.sql.gz media-STAMP.tar.gz
```

Para de un momento `api` y `worker`, restaura Postgres y `media/`, y los vuelve a arrancar.
Ver los comentarios del propio script para el detalle.

### Notas y cosas a decidir en el servidor real

- **`WORKER_REPLICAS=2` por defecto, pero `--reclaim-stale` corre en cada réplica al
  arrancar** (`docker/entrypoint-worker.sh`): requeue de estudios atascados en
  `processing` hace más de 30 minutos (`DEFAULT_STALE_MINUTES` en
  `analysis/management/commands/run_worker.py`). Con un análisis de 60 a 130 s medido en
  el informe de despliegue, 30 minutos da margen amplio; solo sería un problema si una
  réplica reinicia mientras otra lleva un estudio real anormalmente lento. Bajar a
  `WORKER_REPLICAS=1` si se prefiere eliminar el riesgo por completo, al costo de la
  concurrencia.
- **HSTS queda apagado (`SECURE_HSTS_SECONDS=0`)** hasta que el dominio definitivo esté
  decidido: es una promesa que hay que poder mantener en todos los despliegues futuros de
  ese dominio. Subirlo una vez esté firme (ver el comentario en `.env.example`).
- **El admin no está restringido por IP** en el `Caddyfile` de este repositorio; hay un
  bloque comentado para hacerlo. Decidirlo con la IP real de quien investiga, o poner
  Cloudflare Access delante.
- **Los pesos del digitalizador de 12 derivaciones** (`12_lead_ECGFounder.pth`) se
  descargan igual que el de 1 derivación porque `download_weights.sh` (en `ecg-pipeline`)
  todavía no permite bajar solo uno; con `ECG_PATHWAY=rhythm` (el valor por defecto) no se
  usa, y no hace daño dejarlo en el volumen.
- **Nada de esto se probó levantando contenedores de verdad** (ver informe de despliegue):
  se validó `docker compose config` (sin variables faltantes) y `docker buildx build
  --check` (linter del Dockerfile, sin construir la imagen). La primera corrida real, con
  la imagen construida y los pesos descargados, hay que hacerla en el servidor.
