"""Straightening the photographed paper, at native resolution.

This is the server half of a split the app makes deliberately. ``camera/homography.ts``
computes the same transform for its preview but does not apply it to the pixels it
uploads: it sends the four corners instead. Its reasoning, quoted from that file --
correcting on the phone means resampling on a mid-range GPU with whatever filter is
available, and resampling is exactly where a one-millimetre trace disappears. The server
has the image at native resolution and better filters. So the warp happens here.

Formulation
-----------
Pillow's ``Image.transform(..., PERSPECTIVE, coeffs)`` maps *output* pixels back to input
pixels: for an output (x, y) it samples the input at

    u = (a x + b y + c) / (g x + h y + 1)
    v = (d x + e y + f) / (g x + h y + 1)

So the eight coefficients are the homography from the destination rectangle to the source
quadrilateral -- the inverse of the direction the app's preview computes. Getting that
backwards does not raise: it produces a plausible-looking image of the wrong region.

Each corner pair contributes two linear equations in the eight unknowns, giving a square
system solved directly. The app uses Heckbert's unit-square factorisation to avoid the
8x8 solve; there is no reason to avoid it here, where numpy is already loaded.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

# A corner within this many pixels of the image corner it belongs to is not a crop. See
# ``is_full_frame``.
FULL_FRAME_TOLERANCE_PX = 2.0

# Bicubic is the best filter Pillow offers for a perspective transform -- Lanczos exists
# only for ``resize``. Both beat the bilinear a phone would have used.
RESAMPLE = Image.BICUBIC

# The rectified image is written as PNG, which is lossless. It is the digitizer's only
# input, and JPEG's chroma subsampling averages colour over 2x2 blocks -- the size of the
# fine grid lines the pixel-size finder measures. The file is a work artifact, so the disk
# it costs is reclaimed with the rest of the run.
RECTIFIED_SUFFIX = ".png"


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def rectified_size(quad: list[tuple[float, float]]) -> tuple[int, int]:
    """How large the paper was before perspective foreshortened it.

    Perspective shortens the far edge, so neither member of a pair of opposite sides is
    the paper's true dimension. Averaging each pair spreads that error, which is the same
    estimate ``estimateRectifiedAspect`` makes in the app for its preview. It is not a
    metric reconstruction -- that would need the camera's focal length -- but the exact
    scale does not matter here: the digitizer measures the millimetre grid in whatever
    image it is given, so an output a few percent off is rescaled away downstream.

    At least one pixel in each direction, so that a caller never receives a zero-sized
    image for a quad that survived validation but collapsed under rounding.
    """
    top_left, top_right, bottom_right, bottom_left = quad
    width = (_distance(top_left, top_right) + _distance(bottom_left, bottom_right)) / 2.0
    height = (_distance(top_left, bottom_left) + _distance(top_right, bottom_right)) / 2.0
    return max(1, int(round(width))), max(1, int(round(height)))


def is_full_frame(quad: list[tuple[float, float]], width: int, height: int) -> bool:
    """True when the quad is the whole image and no warp is called for.

    Worth detecting rather than warping anyway: an identity perspective transform still
    resamples every pixel, and the entire reason this work happens on the server is that
    resampling costs signal. A user who framed the paper edge to edge gets their pixels
    untouched.
    """
    corners = [(0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height))]
    return all(_distance(q, c) <= FULL_FRAME_TOLERANCE_PX for q, c in zip(quad, corners))


def perspective_coefficients(
    destination: list[tuple[float, float]], source: list[tuple[float, float]]
) -> tuple[float, ...]:
    """The eight coefficients taking ``destination`` corners onto ``source`` corners.

    Both lists are in the app's fixed corner order: top-left, top-right, bottom-right,
    bottom-left. That order is part of the app's contract -- ``quad.ts`` says so -- and a
    permuted quad produces a rotated or mirrored result rather than an error.
    """
    matrix = []
    targets = []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1.0, 0.0, 0.0, 0.0, -x * u, -y * u])
        matrix.append([0.0, 0.0, 0.0, x, y, 1.0, -x * v, -y * v])
        targets.append(u)
        targets.append(v)

    a = np.array(matrix, dtype=np.float64)
    b = np.array(targets, dtype=np.float64)
    try:
        solution = np.linalg.solve(a, b)
    except np.linalg.LinAlgError as exc:
        # Validation rejects self-crossing and collapsed quads before this, so a singular
        # system here means a quad that is degenerate in a way the area and convexity
        # checks did not catch. Refuse rather than hand Pillow a matrix of NaNs.
        raise ValueError(f"quad does not define an invertible perspective transform: {exc}") from exc
    return tuple(float(value) for value in solution)


def rectify(image_path: str | Path, quad: list[tuple[float, float]], output_path: str | Path) -> Path:
    """Write the perspective-corrected crop of ``image_path`` to ``output_path``.

    Returns the path written. When the quad is the full frame the original is copied
    through unchanged, pixel for pixel.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(image_path) as image:
        # EXIF orientation is deliberately NOT applied. The quad's coordinates are the
        # image's raw pixels -- the upload serialiser proves it, by refusing any study
        # whose declared imageWidth/imageHeight differ from what Pillow reads, and Pillow
        # reports raw dimensions without consulting the tag. Transposing here would swap
        # the axes out from under the four corners and rectify the wrong region. Which way
        # is up is then decided by the corner order, which the app fixes as
        # top-left, top-right, bottom-right, bottom-left.
        image = image.convert("RGB")

        if is_full_frame(quad, image.width, image.height):
            image.save(output_path)
            return output_path

        width, height = rectified_size(quad)
        destination = [(0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height))]
        coefficients = perspective_coefficients(destination, quad)

        image.transform((width, height), Image.PERSPECTIVE, coefficients, resample=RESAMPLE).save(output_path)

    return output_path
