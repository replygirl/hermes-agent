"""Map agent activity → a :class:`PetState`.

This is the one place the "what is the agent doing right now?" → "which
animation row?" decision lives.  Each surface feeds it the signals it already
tracks:

- CLI    — ``KawaiiSpinner`` waiting/thinking state + tool outcomes.
- TUI    — gateway ``tool.start/complete`` + ``message.delta/complete`` events.
- Desktop — the ``$busy``/``$awaitingResponse``/tool-event nanostores
            (re-implemented in TS, but mirroring this priority order).

Keeping the priority order here (and documenting it) lets the TypeScript
mirror stay faithful without a second design.
"""

from __future__ import annotations

from agent.pet.constants import PetState


def derive_pet_state(
    *,
    busy: bool = False,
    awaiting_input: bool = False,
    error: bool = False,
    celebrate: bool = False,
    just_completed: bool = False,
    tool_running: bool = False,
    reasoning: bool = False,
) -> PetState:
    """Resolve the animation state from coarse activity signals.

    Priority (highest first) — only one row can show at a time, so the most
    salient signal wins:

    1. ``error``          → ``FAILED``  (a tool/turn just failed)
    2. ``celebrate``      → ``JUMP``    (explicit success beat, e.g. todos done)
    3. ``just_completed`` → ``WAVE``    (turn finished cleanly / greeting)
    4. ``tool_running``   → ``RUN``     (a tool is executing)
    5. ``reasoning``      → ``REVIEW``  (model is thinking / reading)
    6. ``busy``           → ``RUN``     (turn in flight, unspecified work)
    7. otherwise          → ``IDLE``    (incl. ``awaiting_input``)

    ``awaiting_input`` is accepted for symmetry with the surfaces but maps to
    ``IDLE`` — a pet waiting on the user should rest, not run.
    """
    if error:
        return PetState.FAILED
    if celebrate:
        return PetState.JUMP
    if just_completed:
        return PetState.WAVE
    if tool_running:
        return PetState.RUN
    if reasoning:
        return PetState.REVIEW
    if busy:
        return PetState.RUN
    return PetState.IDLE
