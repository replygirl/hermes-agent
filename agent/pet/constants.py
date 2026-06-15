"""Pet sprite geometry + animation-state taxonomy.

These values are *constants of the petdex format*, not per-pet data — the
real ``pet.json`` only carries ``id``/``displayName``/``description``/
``spritesheetPath``.  The official petdex web app and desktop client both
hardcode 192×208 frames, 6 frames per state, a 1100ms loop, and a 0.7 render
scale; we match them so installed pets animate identically.
"""

from __future__ import annotations

from enum import Enum

# Frame geometry (pixels).  A standard petdex spritesheet is a 1536×1872 grid
# → 8 columns × 9 rows of these frames.
FRAME_W = 192
FRAME_H = 208

# Frames consumed per animation state (the petdex web app uses CSS
# ``steps(6)``).  A sheet may physically contain more columns; we only step
# through the first ``FRAMES_PER_STATE``.
FRAMES_PER_STATE = 6

# Full-loop duration for one state, milliseconds (petdex default).
LOOP_MS = 1100

# Default on-screen scale relative to native frame size (petdex desktop uses
# 0.7).  Surfaces may override via ``display.pet.scale``.
DEFAULT_SCALE = 0.7


class PetState(str, Enum):
    """Animation state a pet can be shown in.

    Values are the petdex spritesheet *row names*.  Membership maps directly
    onto :data:`STATE_ROWS` (row index = position in that list).
    """

    IDLE = "idle"
    WAVE = "wave"
    RUN = "run"
    FAILED = "failed"
    REVIEW = "review"
    JUMP = "jump"


# Row order in the spritesheet (top → bottom).  Index of a state name here is
# the pixel row it occupies: ``row_y = STATE_ROWS.index(state) * FRAME_H``.
# ``extra1``/``extra2`` are reserved petdex rows we don't drive yet but keep so
# row math stays correct for sheets that include them.
STATE_ROWS: list[str] = [
    PetState.IDLE.value,
    PetState.WAVE.value,
    PetState.RUN.value,
    PetState.FAILED.value,
    PetState.REVIEW.value,
    PetState.JUMP.value,
    "extra1",
    "extra2",
]


def state_row_index(state: "PetState | str") -> int:
    """Return the spritesheet row index for *state* (clamped, never raises)."""
    value = state.value if isinstance(state, PetState) else str(state)
    try:
        return STATE_ROWS.index(value)
    except ValueError:
        return 0  # fall back to the idle row
