"""Decode a pet spritesheet and encode frames for a terminal.

Shared by the base CLI (writes the escape bytes to its own stdout) and the
TUI (``tui_gateway`` ships the encoded bytes to Ink, which writes them) so the
decode + capability-detection + protocol-encoding logic exists exactly once.

Supported output modes, in fidelity order:

- ``kitty``   — the kitty graphics protocol (kitty, Ghostty, WezTerm).
- ``iterm``   — iTerm2 inline images (iTerm2, WezTerm, VS Code terminal).
- ``sixel``   — DEC sixel (xterm -ti vt340, foot, mlterm, WezTerm, …).
- ``unicode`` — 24-bit half-block downscale; works in any truecolor terminal.

Frame decoding requires Pillow (a core Hermes dependency).  If Pillow or the
spritesheet is unavailable the renderer degrades to ``unicode`` text or an
empty string rather than raising.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from agent.pet.constants import (
    DEFAULT_SCALE,
    FRAME_H,
    FRAME_W,
    FRAMES_PER_STATE,
    PetState,
    state_row_index,
)

logger = logging.getLogger(__name__)

# Public render-mode names accepted by ``display.pet.render_mode``.
RENDER_MODES = ("auto", "kitty", "iterm", "sixel", "unicode", "off")


# ─────────────────────────────────────────────────────────────────────────
# Terminal capability detection
# ─────────────────────────────────────────────────────────────────────────

def detect_terminal_graphics() -> str:
    """Best-effort detection of the richest graphics protocol available.

    Env-based (non-blocking — we never issue a DA1/terminal query that could
    hang a pipe).  Returns one of ``kitty`` / ``iterm`` / ``sixel`` /
    ``unicode``.  Conservative: unknown terminals get ``unicode``, which works
    anywhere with truecolor.
    """
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()

    # kitty graphics protocol
    if os.environ.get("KITTY_WINDOW_ID") or "kitty" in term or "ghostty" in term:
        return "kitty"
    if term_program in {"ghostty"}:
        return "kitty"

    # WezTerm speaks both kitty and iterm; prefer kitty (richer placement).
    if term_program == "wezterm" or os.environ.get("WEZTERM_PANE"):
        return "kitty"

    # iTerm2 inline images
    if term_program == "iterm.app" or os.environ.get("ITERM_SESSION_ID"):
        return "iterm"
    if term_program == "vscode":
        return "iterm"

    # sixel-capable terminals (env heuristics only)
    if term_program in {"mintty"} or "foot" in term or "mlterm" in term:
        return "sixel"
    if "sixel" in term:
        return "sixel"

    return "unicode"


def resolve_mode(configured: str | None, *, stream=None) -> str:
    """Resolve the effective render mode from config + the environment.

    ``configured`` is ``display.pet.render_mode`` (``auto`` → detect).  Returns
    ``off`` when not attached to a TTY (no point emitting graphics into a pipe
    or logfile).
    """
    mode = (configured or "auto").strip().lower()
    if mode not in RENDER_MODES:
        mode = "auto"
    if mode == "off":
        return "off"

    stream = stream or sys.stdout
    try:
        if not (hasattr(stream, "isatty") and stream.isatty()):
            return "off"
    except (ValueError, OSError):
        return "off"

    if mode == "auto":
        return detect_terminal_graphics()
    return mode


# ─────────────────────────────────────────────────────────────────────────
# Frame decoding
# ─────────────────────────────────────────────────────────────────────────

def _open_sheet(path: Path):
    from PIL import Image

    img = Image.open(path)
    return img.convert("RGBA")


@lru_cache(maxsize=8)
def _frames_for(
    sheet_path: str,
    state_value: str,
    frame_w: int,
    frame_h: int,
    frames_per_state: int,
    scale_w: int,
    scale_h: int,
):
    """Return a list of RGBA PIL frames for one state row, scaled.

    Cached by every argument so repeated frame requests during animation are
    free.  Returns ``[]`` on any decode failure.
    """
    try:
        from PIL import Image

        sheet = _open_sheet(Path(sheet_path))
        cols = max(1, sheet.width // frame_w)
        n = min(frames_per_state, cols)
        row = state_row_index(state_value)
        top = row * frame_h
        # Clamp the row to the sheet (some pets ship fewer rows than the 8 the
        # taxonomy reserves).
        if top + frame_h > sheet.height:
            top = max(0, sheet.height - frame_h)

        frames = []
        for i in range(n):
            left = i * frame_w
            box = (left, top, left + frame_w, top + frame_h)
            frame = sheet.crop(box)
            if (scale_w, scale_h) != (frame_w, frame_h):
                frame = frame.resize((scale_w, scale_h), Image.LANCZOS)
            frames.append(frame)
        return frames
    except Exception as exc:  # noqa: BLE001 - cosmetic feature, never fatal
        logger.debug("pet frame decode failed (%s, %s): %s", sheet_path, state_value, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────
# Encoders
# ─────────────────────────────────────────────────────────────────────────

def _png_bytes(frame) -> bytes:
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    return buf.getvalue()


def _encode_kitty(frame, *, cell_cols: int | None = None, cell_rows: int | None = None) -> str:
    """Encode one frame via the kitty graphics protocol (transmit + display).

    Splits the base64 PNG into ≤4096-byte chunks per the protocol, using
    ``a=T`` (transmit & display at the cursor).  ``c``/``r`` request a display
    box in terminal cells so successive frames overwrite the same area.
    """
    data = base64.standard_b64encode(_png_bytes(frame)).decode("ascii")
    extra = "f=100,a=T,q=2"
    if cell_cols:
        extra += f",c={cell_cols}"
    if cell_rows:
        extra += f",r={cell_rows}"

    chunk = 4096
    out: list[str] = []
    if len(data) <= chunk:
        out.append(f"\x1b_G{extra},m=0;{data}\x1b\\")
    else:
        first = data[:chunk]
        out.append(f"\x1b_G{extra},m=1;{first}\x1b\\")
        rest = data[chunk:]
        while rest:
            piece, rest = rest[:chunk], rest[chunk:]
            more = 1 if rest else 0
            out.append(f"\x1b_Gm={more};{piece}\x1b\\")
    return "".join(out)


def _encode_iterm(frame, *, cell_cols: int | None = None, cell_rows: int | None = None) -> str:
    """Encode one frame as an iTerm2 inline image (OSC 1337 File)."""
    payload = base64.standard_b64encode(_png_bytes(frame)).decode("ascii")
    size = len(payload)
    args = [f"inline=1", f"size={size}", "preserveAspectRatio=1"]
    if cell_cols:
        args.append(f"width={cell_cols}")
    if cell_rows:
        args.append(f"height={cell_rows}")
    return f"\x1b]1337;File={';'.join(args)}:{payload}\x07"


def _encode_sixel(frame) -> str:
    """Encode one frame as DEC sixel.

    Quantizes to an adaptive palette (≤255 colors) and emits the sixel band
    stream.  Pillow has no sixel writer, so this is a compact hand-rolled
    encoder.  Transparent pixels render as background (color register skipped).
    """
    from PIL import Image

    rgba = frame
    # Composite onto transparent-as-skip: track alpha to decide background.
    pal = rgba.convert("RGB").quantize(colors=255, method=Image.MEDIANCUT)
    palette = pal.getpalette() or []
    px = pal.load()
    alpha = rgba.getchannel("A").load()
    w, h = pal.size

    out = ["\x1bP0;1;0q", '"1;1;%d;%d' % (w, h)]
    # Color register definitions (sixel uses 0..100 scale).
    used = sorted({px[x, y] for y in range(h) for x in range(w)})
    for idx in used:
        r = palette[idx * 3] if idx * 3 < len(palette) else 0
        g = palette[idx * 3 + 1] if idx * 3 + 1 < len(palette) else 0
        b = palette[idx * 3 + 2] if idx * 3 + 2 < len(palette) else 0
        out.append("#%d;2;%d;%d;%d" % (idx, r * 100 // 255, g * 100 // 255, b * 100 // 255))

    # Emit in 6-row bands.
    for band in range(0, h, 6):
        for color_idx in used:
            line = ["#%d" % color_idx]
            run_char = None
            run_len = 0

            def flush():
                nonlocal run_char, run_len
                if run_char is None:
                    return
                if run_len > 3:
                    line.append("!%d%s" % (run_len, run_char))
                else:
                    line.append(run_char * run_len)
                run_char, run_len = None, 0

            for x in range(w):
                bits = 0
                for bit in range(6):
                    y = band + bit
                    if y < h and alpha[x, y] > 32 and px[x, y] == color_idx:
                        bits |= 1 << bit
                ch = chr(63 + bits)
                if ch == run_char:
                    run_len += 1
                else:
                    flush()
                    run_char, run_len = ch, 1
            flush()
            out.append("".join(line) + "$")  # carriage return within band
        out.append("-")  # next band
    out.append("\x1b\\")
    return "".join(out)


_HALF_BLOCK = "▀"

# A single half-block cell: top pixel + bottom pixel as (r, g, b, a) tuples.
Cell = tuple[tuple[int, int, int, int], tuple[int, int, int, int]]


def _downscale_cells(frame, *, target_cols: int) -> list[list[Cell]]:
    """Downscale a frame to a grid of half-block cells.

    Each cell pairs a top and bottom pixel so one terminal row encodes two
    pixel rows.  Returns rows of ``((tr,tg,tb,ta),(br,bg,bb,ba))`` — the
    framework-neutral representation shared by the ANSI encoder (CLI) and the
    structured ``cells`` API (Ink).
    """
    from PIL import Image

    target_cols = max(4, target_cols)
    aspect = frame.height / max(1, frame.width)
    target_rows = max(2, int(round(target_cols * aspect * 0.5)) * 2)
    small = frame.resize((target_cols, target_rows), Image.LANCZOS).convert("RGBA")
    px = small.load()

    grid: list[list[Cell]] = []
    for y in range(0, target_rows, 2):
        row: list[Cell] = []
        for x in range(target_cols):
            top = px[x, y]
            bottom = px[x, y + 1] if y + 1 < target_rows else (0, 0, 0, 0)
            row.append((top, bottom))
        grid.append(row)
    return grid


def _encode_unicode(frame, *, target_cols: int) -> str:
    """Downscale to truecolor ANSI half-blocks (one char = 2 vertical pixels)."""
    lines: list[str] = []
    for row in _downscale_cells(frame, target_cols=target_cols):
        cells: list[str] = []
        for (tr, tg, tb, ta), (br, bg, bb, ba) in row:
            if ta < 32 and ba < 32:
                cells.append("\x1b[0m ")  # fully transparent → blank
                continue
            cells.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m{_HALF_BLOCK}")
        lines.append("".join(cells) + "\x1b[0m")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Public renderer
# ─────────────────────────────────────────────────────────────────────────

class PetRenderer:
    """Holds a pet's spritesheet and yields encoded frames per (state, index).

    Construct once per pet, then call :meth:`frame` on an animation timer.
    Cheap to call repeatedly — decoded frames are cached.
    """

    def __init__(
        self,
        spritesheet: str | Path,
        *,
        mode: str = "unicode",
        scale: float = DEFAULT_SCALE,
        unicode_cols: int = 20,
        frame_w: int = FRAME_W,
        frame_h: int = FRAME_H,
        frames_per_state: int = FRAMES_PER_STATE,
    ) -> None:
        self.spritesheet = str(spritesheet)
        self.mode = mode if mode in RENDER_MODES else "unicode"
        self.scale = scale
        self.unicode_cols = unicode_cols
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.frames_per_state = frames_per_state

    @property
    def available(self) -> bool:
        return self.mode != "off" and Path(self.spritesheet).is_file()

    def frame_count(self, state: PetState | str) -> int:
        return len(self._frames(state))

    def _frames(self, state: PetState | str):
        value = state.value if isinstance(state, PetState) else str(state)
        scale_w = max(1, int(self.frame_w * self.scale))
        scale_h = max(1, int(self.frame_h * self.scale))
        return _frames_for(
            self.spritesheet,
            value,
            self.frame_w,
            self.frame_h,
            self.frames_per_state,
            scale_w,
            scale_h,
        )

    def cells(self, state: PetState | str, index: int, *, cols: int | None = None) -> list[list[Cell]]:
        """Return one frame as a half-block cell grid (framework-neutral).

        Used by the TUI, which renders the grid with native Ink color props
        instead of raw ANSI.  Returns ``[]`` when no frame is available.
        """
        frames = self._frames(state)
        if not frames:
            return []
        frame = frames[index % len(frames)]
        return _downscale_cells(frame, target_cols=cols or self.unicode_cols)

    def frame(self, state: PetState | str, index: int) -> str:
        """Return the encoded escape string for one frame, or ``""``.

        ``index`` is taken modulo the available frame count so callers can pass
        a free-running counter.
        """
        if self.mode == "off":
            return ""
        frames = self._frames(state)
        if not frames:
            return ""
        frame = frames[index % len(frames)]

        # Display box in cells for graphics protocols (≈ scaled px / cell size,
        # assuming a ~8×16 cell; terminals re-fit anyway).
        cell_cols = max(1, frame.width // 8)
        cell_rows = max(1, frame.height // 16)

        try:
            if self.mode == "kitty":
                return _encode_kitty(frame, cell_cols=cell_cols, cell_rows=cell_rows)
            if self.mode == "iterm":
                return _encode_iterm(frame, cell_cols=cell_cols, cell_rows=cell_rows)
            if self.mode == "sixel":
                return _encode_sixel(frame)
            return _encode_unicode(frame, target_cols=self.unicode_cols)
        except Exception as exc:  # noqa: BLE001 - degrade silently
            logger.debug("pet frame encode failed (mode=%s): %s", self.mode, exc)
            return ""


def build_renderer(
    spritesheet: str | Path,
    *,
    configured_mode: str | None = None,
    scale: float = DEFAULT_SCALE,
    unicode_cols: int = 20,
    stream=None,
) -> PetRenderer:
    """Convenience factory: resolve the mode from config+env, then construct."""
    mode = resolve_mode(configured_mode, stream=stream)
    return PetRenderer(
        spritesheet,
        mode=mode,
        scale=scale,
        unicode_cols=unicode_cols,
    )
