"""What the app calls a mount, and what the digitizer calls the same thing.

The app asks the user which mount they are photographing (``MountId`` in
``camera/mounts.ts``) before the shutter, because the framing guide depends on it. The
digitizer identifies the layout itself, from the image. Those are two independent answers
to the same question, and this module is where they are reconciled.

Only one mount needs the digitizer to be told anything. The right-sided 3x3 print is not
in the digitizer's default layout set at all, so without the override it cannot be
identified -- and ``ecg_pipeline.contract`` relabels V4/V5/V6 to V4R/V5R/V6R only when
that specific layout name comes back. Get this wrong and a right-sided trace is served
under a left-sided name, which is a lie about which side of the heart was recorded.

For every other mount the digitizer identifies the layout unaided, and the declared mount
is kept as a cross-check rather than as an instruction: see ``layout_disagrees_with_mount``.
"""

from __future__ import annotations

STANDARD_3X4 = "standard-3x4"
RHYTHM_3X4 = "rhythm-3x4"
RIGHT_3X3 = "right-3x3"
SIX_2 = "six-2"
TWELVE_1 = "twelve-1"

# Exactly the app's ``MountId`` union, in its order.
MOUNT_IDS = (STANDARD_3X4, RHYTHM_3X4, RIGHT_3X3, SIX_2, TWELVE_1)

MOUNT_CHOICES = [
    (STANDARD_3X4, "3x4 standard"),
    (RHYTHM_3X4, "3x4 with rhythm strip"),
    (RIGHT_3X3, "3x3 right-sided"),
    (SIX_2, "6x2"),
    (TWELVE_1, "12x1"),
]

# Lead-layout file from ecg-pipeline's ``configs/``, passed to the digitizer as an
# override. None means "let the digitizer identify it from its full default set".
LEAD_LAYOUT_FILE = {
    STANDARD_3X4: None,
    RHYTHM_3X4: None,
    RIGHT_3X3: "lead_layouts_limbaug_right.yml",
    SIX_2: None,
    TWELVE_1: None,
}

# Layout names the digitizer may legitimately return for each mount. Used only to notice
# a disagreement, never to overrule the digitizer: it looked at the image and the user
# looked at a menu, and of the two the image is the evidence.
#
# These are FAMILIES, not exact matches, and the difference is the whole point. What makes
# a disagreement dangerous is a layout that puts *different lead names in the same grid
# positions* -- read a 3x4 as a 6x2 and every trace is served under the wrong name. Whether
# the print also carries a rhythm strip along the bottom changes none of that: the grid is
# the same grid, the names are the same names, and the strip is extra signal rather than
# different signal.
#
# So 'standard-3x4' accepts the whole 3x4 family. A first version of this insisted on an
# exact match and refused a perfectly readable 3x4-with-rhythm-strip that the user had
# framed as a plain 3x4 -- an ordinary thing to do, since the app's mount menu is a framing
# guide and does not ask whether a strip is present.
_THREE_BY_FOUR = frozenset({"standard_3x4", "standard_3x4_with_r1", "standard_3x4_with_r2", "standard_3x4_with_r3"})

EXPECTED_LAYOUTS = {
    STANDARD_3X4: _THREE_BY_FOUR,
    RHYTHM_3X4: _THREE_BY_FOUR,
    SIX_2: frozenset({"standard_6x2", "standard_6x2_with_r1"}),
    # Cabrera orders the limb leads differently from the standard sequence, but the
    # digitizer identifies which of the two it is looking at, and names the leads
    # accordingly. Either identification is a correct reading of a 12x1 print.
    TWELVE_1: frozenset({"standard_12x1", "cabrera_12x1"}),
    # Alone, and it must stay alone. The digitizer fills the V4/V5/V6 slots by position,
    # so on this layout those traces are the right-sided leads, and
    # ``ecg_pipeline.contract`` relabels them to V4R/V5R/V6R only when this exact name
    # comes back. Any confusion here serves a right-sided trace under a left-sided name.
    RIGHT_3X3: frozenset({"limb_aug_right_3x3"}),
}


def lead_layout_for(mount: str) -> str | None:
    """The layout override to pass to ``ecg_pipeline``, or None to let it identify."""
    return LEAD_LAYOUT_FILE.get(mount)


def layout_disagrees_with_mount(mount: str, identified: str | None) -> bool:
    """Did the digitizer read a different mount than the one the user declared?

    A disagreement is worth surfacing rather than suppressing: either the user picked the
    wrong guide and the framing was off, or the layout was misidentified and the leads are
    about to be named wrongly. Both are reasons to distrust the reading.

    An unidentified layout is not a disagreement -- ``ecg_pipeline`` already degrades that
    case on its own, and reporting it twice would say the same thing in two vocabularies.
    """
    if not identified or identified == "Unknown layout":
        return False
    expected = EXPECTED_LAYOUTS.get(mount)
    if expected is None:
        return False
    return identified not in expected
