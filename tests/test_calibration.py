"""The calibration correction.

These are the tests that matter most in this repository. An error here does not crash
anything: it produces a complete, well-formed, confidently-drawn ECG that is wrong about
how fast a heart was beating or how large its complexes were. Nothing downstream can
detect that, because by then the numbers are just numbers.

The structural-gap rule is tested explicitly, and it is not decoration. ``signal.ts``
in the app builds its whole signal model around it: on a 3x4 print each grid lead exists
for 2.5 of the 10 seconds, and a line drawn across the other 7.5 is a credible-looking
claim about a heart nobody recorded. A resampler run over a whole lead draws exactly that
line, silently.
"""

from __future__ import annotations

import numpy as np
from django.test import SimpleTestCase

from analysis.calibration import amplitude_factor, correct_canonical, is_standard, time_factor, write_canonical_csv


class FactorTests(SimpleTestCase):
    def test_standard_calibration_scales_nothing(self) -> None:
        self.assertEqual(amplitude_factor(10.0), 1.0)
        self.assertEqual(time_factor(25.0), 1.0)
        self.assertTrue(is_standard(25.0, 10.0))

    def test_double_amplitude_paper_halves_the_reading(self) -> None:
        # At 20 mm/mV the paper draws twice as many millimetres per millivolt, so the
        # digitizer -- which assumes 10 -- reads twice the real voltage.
        self.assertEqual(amplitude_factor(20.0), 0.5)

    def test_half_amplitude_paper_doubles_the_reading(self) -> None:
        self.assertEqual(amplitude_factor(5.0), 2.0)

    def test_double_speed_paper_halves_the_duration(self) -> None:
        # At 50 mm/s ten seconds of heart occupy twice the paper, so the record is really
        # half as long as the digitizer believed -- and the rate half what it reported.
        self.assertEqual(time_factor(50.0), 0.5)

    def test_half_speed_paper_doubles_the_duration(self) -> None:
        self.assertEqual(time_factor(12.5), 2.0)

    def test_the_three_speeds_and_gains_the_app_offers_are_all_covered(self) -> None:
        # CALIBRATION_SPEEDS and CALIBRATION_GAINS in the app's capture/study.ts.
        for speed in (12.5, 25.0, 50.0):
            self.assertGreater(time_factor(speed), 0)
        for gain in (5.0, 10.0, 20.0):
            self.assertGreater(amplitude_factor(gain), 0)


class AmplitudeTests(SimpleTestCase):
    def test_a_standard_record_comes_back_unchanged(self) -> None:
        canonical = np.array([[1.0, 2.0, 3.0, 4.0]])

        corrected = correct_canonical(canonical, 25.0, 10.0)

        np.testing.assert_array_equal(corrected, canonical)

    def test_amplitude_is_scaled_exactly(self) -> None:
        canonical = np.array([[100.0, -200.0, 300.0, 0.0]])

        corrected = correct_canonical(canonical, 25.0, 20.0)

        np.testing.assert_array_equal(corrected, np.array([[50.0, -100.0, 150.0, 0.0]]))

    def test_amplitude_scaling_does_not_touch_the_sample_count(self) -> None:
        canonical = np.zeros((12, 5000))

        self.assertEqual(correct_canonical(canonical, 25.0, 5.0).shape, (12, 5000))

    def test_missing_samples_stay_missing_under_amplitude_scaling(self) -> None:
        canonical = np.array([[1.0, np.nan, 3.0]])

        corrected = correct_canonical(canonical, 25.0, 20.0)

        self.assertTrue(np.isnan(corrected[0, 1]))


class TimeRescalingTests(SimpleTestCase):
    def test_double_speed_halves_the_sample_count(self) -> None:
        canonical = np.zeros((1, 5000))

        self.assertEqual(correct_canonical(canonical, 50.0, 10.0).shape, (1, 2500))

    def test_half_speed_doubles_the_sample_count(self) -> None:
        canonical = np.zeros((1, 2500))

        self.assertEqual(correct_canonical(canonical, 12.5, 10.0).shape, (1, 5000))

    def test_a_sine_keeps_its_shape_and_loses_half_its_cycles(self) -> None:
        # The substantive check: at 50 mm/s the digitizer reported 10 cycles over what it
        # thought was 10 s. The truth is 10 cycles over 5 s -- the same waveform, correctly
        # dated. Amplitude must survive the resampling untouched.
        samples = np.linspace(0, 10 * 2 * np.pi, 5000)
        canonical = np.sin(samples).reshape(1, -1)

        corrected = correct_canonical(canonical, 50.0, 10.0)

        self.assertEqual(corrected.shape, (1, 2500))
        self.assertAlmostEqual(float(np.nanmax(corrected)), 1.0, places=2)
        self.assertAlmostEqual(float(np.nanmin(corrected)), -1.0, places=2)

    def test_speed_and_gain_corrections_compose(self) -> None:
        canonical = np.full((1, 1000), 8.0)

        corrected = correct_canonical(canonical, 50.0, 20.0)

        self.assertEqual(corrected.shape, (1, 500))
        # 8 uV read at an assumed 10 mm/mV on paper printed at 20 mm/mV is really 4.
        np.testing.assert_allclose(corrected[0], np.full(500, 4.0), rtol=1e-6)


class StructuralGapTests(SimpleTestCase):
    """A gap is the absence of a recording, and it must survive as one."""

    def test_a_gap_is_not_interpolated_across(self) -> None:
        # Lead recorded for the first half, absent for the second -- the shape of every
        # grid lead on a 3x4 print.
        canonical = np.full((1, 1000), np.nan)
        canonical[0, :500] = 1.0

        corrected = correct_canonical(canonical, 50.0, 10.0)

        self.assertEqual(corrected.shape, (1, 500))
        self.assertFalse(np.isnan(corrected[0, :250]).any(), "the recorded half should survive")
        self.assertTrue(np.isnan(corrected[0, 250:]).all(), "the gap must still be a gap")

    def test_an_interior_gap_keeps_its_position_and_width(self) -> None:
        canonical = np.zeros((1, 1000))
        canonical[0, 400:600] = np.nan

        corrected = correct_canonical(canonical, 50.0, 10.0)

        self.assertTrue(np.isnan(corrected[0, 200:300]).all())
        self.assertFalse(np.isnan(corrected[0, :200]).any())
        self.assertFalse(np.isnan(corrected[0, 300:]).any())

    def test_the_gap_fraction_is_preserved(self) -> None:
        # Rescaling time uniformly cannot change how much of a record was recorded. If
        # this ratio moves, samples were invented or dropped.
        rng = np.random.default_rng(0)
        canonical = rng.normal(size=(1, 2000))
        canonical[0, 700:1300] = np.nan

        corrected = correct_canonical(canonical, 12.5, 10.0)

        before = np.isnan(canonical).mean()
        after = np.isnan(corrected).mean()
        self.assertAlmostEqual(before, after, places=2)

    def test_several_segments_on_one_lead_all_move_together(self) -> None:
        # A 3x4 lead printed in three separate column windows.
        canonical = np.full((1, 1200), np.nan)
        canonical[0, 0:200] = 1.0
        canonical[0, 500:700] = 2.0
        canonical[0, 1000:1200] = 3.0

        corrected = correct_canonical(canonical, 50.0, 10.0)

        self.assertEqual(corrected.shape, (1, 600))
        np.testing.assert_allclose(corrected[0, 0:100], 1.0, rtol=1e-3)
        np.testing.assert_allclose(corrected[0, 250:350], 2.0, rtol=1e-3)
        np.testing.assert_allclose(corrected[0, 500:600], 3.0, rtol=1e-3)
        self.assertTrue(np.isnan(corrected[0, 120:230]).all())

    def test_a_lead_with_no_signal_at_all_stays_empty(self) -> None:
        canonical = np.full((2, 1000), np.nan)
        canonical[0, :] = 1.0

        corrected = correct_canonical(canonical, 50.0, 10.0)

        self.assertTrue(np.isnan(corrected[1]).all())


class CsvRoundTripTests(SimpleTestCase):
    """The corrected file has to be readable as if the digitizer had written it."""

    def test_a_corrected_csv_loads_back_with_its_leads_and_gaps(self) -> None:
        import tempfile

        from ecg_pipeline.interpret.waveform import load_canonical_csv

        canonical = np.array([[1.0, 2.0, np.nan], [4.0, np.nan, 6.0]])
        names = ["I", "II"]

        with tempfile.NamedTemporaryFile(suffix=".csv") as handle:
            write_canonical_csv(handle.name, canonical, names)
            loaded, loaded_names = load_canonical_csv(handle.name)

        self.assertEqual(loaded_names, names)
        np.testing.assert_allclose(loaded, canonical)
        self.assertTrue(np.isnan(loaded[0, 2]))
        self.assertTrue(np.isnan(loaded[1, 1]))
