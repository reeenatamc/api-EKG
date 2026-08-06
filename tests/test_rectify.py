"""Perspective rectification.

The geometry tests are exact, not approximate, and that is deliberate. A transposed or
inverted homography does not raise: it produces a mirrored or folded image that still
looks like a photograph of an ECG, digitizes into a plausible trace, and is wrong in a way
no later stage can see. So the coefficients are checked by mapping the corners through
them and asserting where they land.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase
from PIL import Image

from analysis.rectify import (
    is_full_frame,
    perspective_coefficients,
    rectified_size,
    rectify,
)


def apply(coefficients: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    """Map an output pixel back to its input pixel, the way Pillow does."""
    a, b, c, d, e, f, g, h = coefficients
    denominator = g * x + h * y + 1.0
    return ((a * x + b * y + c) / denominator, (d * x + e * y + f) / denominator)


class CoefficientTests(SimpleTestCase):
    def test_the_destination_corners_map_onto_the_source_corners(self) -> None:
        # The whole contract of the transform, in one assertion. The order is the app's:
        # top-left, top-right, bottom-right, bottom-left.
        source = [(10.0, 20.0), (300.0, 5.0), (310.0, 220.0), (0.0, 200.0)]
        destination = [(0.0, 0.0), (300.0, 0.0), (300.0, 200.0), (0.0, 200.0)]

        coefficients = perspective_coefficients(destination, source)

        for (dx, dy), (sx, sy) in zip(destination, source):
            mapped_x, mapped_y = apply(coefficients, dx, dy)
            self.assertAlmostEqual(mapped_x, sx, places=6)
            self.assertAlmostEqual(mapped_y, sy, places=6)

    def test_an_identity_quad_gives_an_identity_transform(self) -> None:
        corners = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)]

        coefficients = perspective_coefficients(corners, corners)

        for x, y in [(0.0, 0.0), (50.0, 25.0), (100.0, 50.0), (17.0, 3.0)]:
            mapped_x, mapped_y = apply(coefficients, x, y)
            self.assertAlmostEqual(mapped_x, x, places=6)
            self.assertAlmostEqual(mapped_y, y, places=6)

    def test_the_transform_is_not_its_own_inverse(self) -> None:
        # Guards the direction. Pillow maps output back to input, so the coefficients must
        # take the destination rectangle onto the source quad -- the opposite of what the
        # app's preview computes. Swapping the arguments is silent and produces a
        # convincing image of the wrong region.
        source = [(50.0, 10.0), (250.0, 40.0), (240.0, 190.0), (30.0, 160.0)]
        destination = [(0.0, 0.0), (200.0, 0.0), (200.0, 150.0), (0.0, 150.0)]

        forward = perspective_coefficients(destination, source)
        backward = perspective_coefficients(source, destination)

        self.assertNotAlmostEqual(forward[0], backward[0], places=3)
        # Mapping the destination's top-left through the *wrong* one must not land on the
        # source's top-left.
        self.assertNotAlmostEqual(apply(backward, 0.0, 0.0)[0], source[0][0], places=3)

    def test_a_collapsed_quad_is_refused_rather_than_producing_nans(self) -> None:
        collapsed = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)]
        destination = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]

        with self.assertRaises(ValueError):
            perspective_coefficients(destination, collapsed)


class SizeTests(SimpleTestCase):
    def test_a_rectangle_keeps_its_dimensions(self) -> None:
        quad = [(0.0, 0.0), (400.0, 0.0), (400.0, 300.0), (0.0, 300.0)]

        self.assertEqual(rectified_size(quad), (400, 300))

    def test_opposite_sides_are_averaged(self) -> None:
        # Perspective shortens the far edge, so neither of a pair of opposite sides is the
        # paper's true dimension; averaging spreads the error.
        quad = [(0.0, 0.0), (400.0, 0.0), (300.0, 300.0), (0.0, 300.0)]

        width, _ = rectified_size(quad)

        self.assertEqual(width, 350)

    def test_a_degenerate_quad_still_yields_a_usable_size(self) -> None:
        quad = [(0.0, 0.0), (0.4, 0.0), (0.4, 0.4), (0.0, 0.4)]

        self.assertEqual(rectified_size(quad), (1, 1))


class FullFrameTests(SimpleTestCase):
    def test_the_whole_image_is_recognised_as_no_crop(self) -> None:
        quad = [(0.0, 0.0), (640.0, 0.0), (640.0, 480.0), (0.0, 480.0)]

        self.assertTrue(is_full_frame(quad, 640, 480))

    def test_a_corner_off_by_rounding_is_still_no_crop(self) -> None:
        quad = [(0.5, 0.0), (640.0, 1.0), (639.0, 480.0), (0.0, 479.5)]

        self.assertTrue(is_full_frame(quad, 640, 480))

    def test_a_real_crop_is_not_the_full_frame(self) -> None:
        quad = [(20.0, 20.0), (620.0, 25.0), (615.0, 460.0), (25.0, 455.0)]

        self.assertFalse(is_full_frame(quad, 640, 480))


class RectifyFileTests(SimpleTestCase):
    def make_image(self, directory: Path, width: int = 200, height: int = 100) -> Path:
        path = directory / "source.png"
        Image.new("RGB", (width, height), (255, 255, 255)).save(path)
        return path

    def test_a_full_frame_quad_passes_the_pixels_through_untouched(self) -> None:
        # The entire reason this work happens on the server is that resampling costs
        # signal. A user who framed the paper edge to edge must not pay for a warp.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = self.make_image(directory)
            quad = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (0.0, 100.0)]

            output = rectify(source, quad, directory / "out.png")

            with Image.open(output) as image:
                self.assertEqual(image.size, (200, 100))

    def test_a_crop_produces_an_image_of_the_estimated_paper_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = self.make_image(directory, 400, 300)
            quad = [(10.0, 10.0), (210.0, 20.0), (200.0, 170.0), (20.0, 160.0)]

            output = rectify(source, quad, directory / "out.png")

            with Image.open(output) as image:
                self.assertEqual(image.size, rectified_size(quad))

    def test_the_rectified_content_is_the_region_the_quad_marked(self) -> None:
        # A red square inside a white image; the quad frames only the square. If the
        # transform direction were wrong the output would be white.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "source.png"
            image = Image.new("RGB", (400, 400), (255, 255, 255))
            for x in range(100, 300):
                for y in range(100, 300):
                    image.putpixel((x, y), (255, 0, 0))
            image.save(source)

            quad = [(100.0, 100.0), (300.0, 100.0), (300.0, 300.0), (100.0, 300.0)]
            output = rectify(source, quad, directory / "out.png")

            with Image.open(output) as warped:
                self.assertEqual(warped.getpixel((100, 100)), (255, 0, 0))
