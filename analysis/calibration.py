"""Correcting a record that was not printed at 25 mm/s and 10 mm/mV.

The digitizer measures the millimetre grid and converts millimetres to millivolts and
seconds using the standard calibration, and only the standard one -- ``mv_per_mm`` is a
default argument in ``lead_identifier.py`` and nothing overrides it. The app, correctly,
lets the user say the print was made at half speed or double amplitude, because those are
real settings on a real machine: half speed to fit ten seconds on less paper, half
amplitude when the complex saturates into the row above.

Put those two facts together and an uncorrected 50 mm/s record is reported at twice its
true heart rate, and an uncorrected 20 mm/mV record at half its true amplitude. Both are
wrong about a patient while looking entirely ordinary, which is the failure mode this
whole codebase is organised against. Hence this module.

The arithmetic
--------------
The digitizer resolved a physical distance ``d`` mm on the paper and assumed:

    time      t = d / 25       amplitude  a = d / 10  mV

For a print made at ``S`` mm/s and ``G`` mm/mV the truth is ``d / S`` and ``d / G``, so:

    true time = reported time x (25 / S)        true amplitude = reported amplitude x (10 / G)

Amplitude is a scalar and is exact. Time is a resampling, and it is applied to the signal
rather than left as a different sampling rate for one reason: the interpretation model
reads the canonical CSV as 500 Hz whatever the header says. Rescaling the samples means
everything downstream -- quality assessment, the model, the contract emitted to the app --
sees one consistent record, and no later stage needs to know this happened.

Gaps stay gaps
--------------
Each continuous run of samples is resampled on its own and placed at its own rescaled
start. Nothing is ever interpolated across a NaN. That rule comes from the app's
``signal.ts``: on a 3x4 print each grid lead exists for 2.5 of the 10 seconds, and a line
drawn across the other 7.5 is a credible-looking claim about a heart nobody recorded.
A resampler run over the whole lead would draw exactly that line.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import numpy.typing as npt

STANDARD_SPEED_MM_PER_SECOND = 25.0
STANDARD_GAIN_MM_PER_MILLIVOLT = 10.0

# Resampling ratios are built from a fraction of the speed ratio. A thousand keeps the
# polyphase filter cheap while representing any speed a machine actually prints at.
MAX_RATIO_DENOMINATOR = 1000

# Below this many samples the polyphase filter has less signal than it has filter, and its
# edge behaviour dominates. Linear interpolation is the honest choice for a run that
# short, and a run this size is a fragment of a fragment.
MIN_SAMPLES_FOR_POLYPHASE = 8


def amplitude_factor(gain_mm_per_millivolt: float) -> float:
    """What to multiply the digitizer's microvolts by. 1.0 at standard gain."""
    return STANDARD_GAIN_MM_PER_MILLIVOLT / float(gain_mm_per_millivolt)


def time_factor(speed_mm_per_second: float) -> float:
    """What to multiply the digitizer's durations by. 1.0 at standard speed.

    Below 1 the record is shorter than the digitizer believed (the paper ran fast), above
    1 it is longer.
    """
    return STANDARD_SPEED_MM_PER_SECOND / float(speed_mm_per_second)


def is_standard(speed_mm_per_second: float, gain_mm_per_millivolt: float) -> bool:
    return (
        float(speed_mm_per_second) == STANDARD_SPEED_MM_PER_SECOND
        and float(gain_mm_per_millivolt) == STANDARD_GAIN_MM_PER_MILLIVOLT
    )


def _runs(values: npt.NDArray[np.float64]) -> list[tuple[int, int]]:
    """The half-open [start, end) index ranges of continuous recorded stretches."""
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(valid) > 1) + 1
    return [(int(run[0]), int(run[-1]) + 1) for run in np.split(valid, breaks)]


def _resample_run(run: npt.NDArray[np.float64], target_length: int) -> npt.NDArray[np.float64]:
    """Resample one continuous run to ``target_length`` samples.

    ``padtype='line'`` rather than the default zero padding: an ECG segment rarely starts
    or ends at zero, and zero-padding a polyphase filter pulls the first and last few
    samples toward baseline -- which on a segment boundary looks like the start of a wave.
    """
    if target_length < 1:
        return np.empty(0, dtype=np.float64)
    if run.size == target_length:
        return run.astype(np.float64, copy=True)

    if run.size >= MIN_SAMPLES_FOR_POLYPHASE:
        from scipy.signal import resample_poly

        ratio = Fraction(target_length, run.size).limit_denominator(MAX_RATIO_DENOMINATOR)
        resampled = resample_poly(run, ratio.numerator, ratio.denominator, padtype="line")
    else:
        # Linear interpolation, strictly inside the run. Still never crosses a gap.
        source = np.linspace(0.0, 1.0, run.size)
        target = np.linspace(0.0, 1.0, target_length)
        resampled = np.interp(target, source, run)

    # limit_denominator and the filter's own ceiling can leave the length a sample or two
    # off. Trim or edge-extend to exactly what was asked for.
    if resampled.size > target_length:
        return np.asarray(resampled[:target_length], dtype=np.float64)
    if resampled.size < target_length:
        padding = np.full(target_length - resampled.size, resampled[-1] if resampled.size else np.nan)
        return np.concatenate([resampled, padding]).astype(np.float64)
    return np.asarray(resampled, dtype=np.float64)


def correct_canonical(
    canonical: npt.NDArray[np.float64],
    speed_mm_per_second: float,
    gain_mm_per_millivolt: float,
) -> npt.NDArray[np.float64]:
    """Return ``canonical`` (n_leads, n_samples) as it would have been at 25 mm/s, 10 mm/mV.

    Amplitude is scaled exactly. Time is resampled per continuous run, so the NaN gaps
    that mark 'this lead was not printed here' survive as gaps, moved to their rescaled
    positions.
    """
    corrected = canonical.astype(np.float64, copy=True) * amplitude_factor(gain_mm_per_millivolt)

    factor = time_factor(speed_mm_per_second)
    if factor == 1.0:
        return corrected

    n_samples = corrected.shape[1]
    new_samples = max(1, int(round(n_samples * factor)))
    output = np.full((corrected.shape[0], new_samples), np.nan, dtype=np.float64)

    for lead in range(corrected.shape[0]):
        for start, end in _runs(corrected[lead]):
            new_start = int(round(start * factor))
            new_length = int(round((end - start) * factor))
            if new_length < 1 or new_start >= new_samples:
                # A run that rescales to nothing was a handful of samples to begin with.
                # Dropping it is right: there is no sample to place, and inventing one
                # would put a datum where the resampling said there is none.
                continue
            new_length = min(new_length, new_samples - new_start)
            output[lead, new_start : new_start + new_length] = _resample_run(
                corrected[lead, start:end], new_length
            )

    return output


def write_canonical_csv(path: str | Path, canonical: npt.NDArray[np.float64], names: list[str]) -> Path:
    """Write a canonical CSV in the digitizer's own layout: rows are time, columns leads.

    ``np.savetxt`` writes NaN as the literal ``nan``, which is what
    ``ecg_pipeline.interpret.waveform.load_canonical_csv`` reads back as a missing sample.
    The round trip is what lets a corrected file be handed to the pipeline as if the
    digitizer had produced it.
    """
    path = Path(path)
    np.savetxt(path, canonical.T, delimiter=",", header=",".join(names), comments="", fmt="%.6g")
    return path


def correct_csv(
    source: str | Path,
    destination: str | Path,
    speed_mm_per_second: float,
    gain_mm_per_millivolt: float,
) -> Path:
    """Read a canonical CSV, correct it for calibration, write it to ``destination``."""
    from ecg_pipeline.interpret.waveform import load_canonical_csv

    canonical, names = load_canonical_csv(str(source))
    corrected = correct_canonical(canonical, speed_mm_per_second, gain_mm_per_millivolt)
    return write_canonical_csv(destination, corrected, names)
