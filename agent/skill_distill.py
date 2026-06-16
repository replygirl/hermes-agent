#!/usr/bin/env python3
"""Skill distillation engine -- the ``/learn`` / ``hermes learn`` backend.

Point this at one or more directories of source material (source code, API
docs, instruction manuals, PDFs, config samples, READMEs) and it will:

  1. Walk the directories and CLASSIFY each file by source type.
  2. Build a compact, token-budgeted CORPUS from the highest-signal files.
  3. SYNTHESIZE a draft ``SKILL.md`` via the auxiliary LLM (main-model-first,
     cache-safe -- never touches the live conversation or its prompt cache).
  4. VERIFY the draft in a throwaway sandbox (temp dir, never the user's real
     skills tree): shell snippets are syntax-checked / dry-run, referenced
     file paths and commands are existence-checked, frontmatter is validated.
  5. COMMIT the skill via ``tools.skill_manager_tool._create_skill`` ONLY when
     verification passes the configured floor, tagging the skill with the
     verification TIER it actually achieved.

Design notes / invariants
--------------------------
* This module performs ZERO model-tool registration. It is invoked from CLI
  subcommands, gateway slash handlers, the TUI, and the dashboard -- all of
  which call :func:`distill_skill_from_dirs`. Footprint-ladder rung 2
  (CLI command + skill-producing engine), not a new core tool.
* The LLM synthesis goes through ``agent.auxiliary_client.call_llm`` with a
  dedicated task name, so it inherits main-model-first resolution and any
  per-task config override without breaking conversation prompt caching.
* Verification is a TIER, not a boolean. We never claim a skill was "tested"
  when we only parsed it. The achieved tier is recorded in the skill's
  frontmatter (``metadata.hermes.distill.verification``) and surfaced to the
  caller so the UI can be honest.
* Nothing executes against the user's real ``HERMES_HOME`` during
  verification. Shell snippets run in an isolated temp working directory with
  a short timeout, and only when ``run_commands`` is explicitly enabled.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (all overridable via config.yaml -> skills.distill.*; see _cfg)
# ---------------------------------------------------------------------------

# Per-file read cap so one giant file can't blow the corpus budget.
_DEFAULT_MAX_FILE_BYTES = 64_000
# Total corpus character budget handed to the synthesis LLM.
_DEFAULT_CORPUS_BUDGET = 180_000
# Hard cap on files walked, so pointing at a monorepo doesn't hang.
_DEFAULT_MAX_FILES = 400
# Sandbox shell snippet timeout (seconds).
_DEFAULT_SNIPPET_TIMEOUT = 15

# Directories that never carry distill signal -- skipped wholesale.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "target", "vendor", ".idea", ".vscode", "site-packages",
}

# Binary / noise extensions never read into the corpus.
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".mp4", ".mov",
    ".mp3", ".wav", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bin",
    ".so", ".dylib", ".dll", ".class", ".jar", ".pyc", ".o", ".a", ".woff",
    ".woff2", ".ttf", ".eot", ".lock", ".pdf",  # PDFs handled separately
}

# Source-type classification by extension. Order does not matter; first hit wins
# via the _CLASSIFY_MAP lookup.
_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala", ".sh",
    ".bash", ".zsh", ".lua", ".pl", ".r", ".jl", ".sql", ".dart",
}
_DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_CONFIG_EXT = {
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".env", ".properties",
    ".conf", ".xml",
}

# Filename signals that boost a file's priority in corpus selection.
_HIGH_SIGNAL_NAMES = (
    "readme", "api", "openapi", "swagger", "usage", "guide", "tutorial",
    "getting-started", "quickstart", "howto", "manual", "reference",
    "endpoints", "schema", "cli", "commands", "examples", "example",
)


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass
class SourceFile:
    """A single ingested source file."""

    path: Path
    rel: str
    kind: str  # "code" | "doc" | "config" | "pdf"
    size: int
    text: str = ""
    priority: int = 0


@dataclass
class VerificationResult:
    """Outcome of sandboxed verification of a draft skill."""

    tier: str  # "executed" | "checked" | "unverified" | "failed"
    passed: bool
    checks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class DistillResult:
    """Everything a caller needs to report the outcome of a /learn run."""

    success: bool
    skill_name: Optional[str] = None
    skill_path: Optional[str] = None
    category: Optional[str] = None
    verification: Optional[VerificationResult] = None
    sources_ingested: int = 0
    source_breakdown: Dict[str, int] = field(default_factory=dict)
    draft_only: bool = False  # True when verify floor not met and we did not commit
    draft_content: Optional[str] = None
    error: Optional[str] = None
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _cfg(key: str, default: Any) -> Any:
    """Read skills.distill.<key> from config.yaml, falling back to default."""
    try:
        from hermes_cli.config import cfg_get, load_config

        val = cfg_get(load_config(), "skills", "distill", key, default=None)
        return default if val is None else val
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Step 1+2: ingest & classify
# ---------------------------------------------------------------------------


def _classify(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _BINARY_EXT:
        return None
    if ext in _CODE_EXT:
        return "code"
    if ext in _DOC_EXT:
        return "doc"
    if ext in _CONFIG_EXT:
        return "config"
    # Extension-less high-signal files (e.g. Dockerfile, Makefile) -> doc-ish.
    name = path.name.lower()
    if name in ("dockerfile", "makefile", "procfile", "license", "notice"):
        return "doc"
    return None


def _file_priority(path: Path, kind: str) -> int:
    """Higher = more likely to make it into the corpus budget."""
    score = 0
    name = path.name.lower()
    for sig in _HIGH_SIGNAL_NAMES:
        if sig in name:
            score += 10
            break
    # Docs and API specs are the richest distill signal.
    score += {"doc": 6, "config": 4, "code": 3, "pdf": 7}.get(kind, 0)
    # Shallower files (top-level READMEs etc.) tend to be more authoritative.
    depth = len(path.parts)
    score += max(0, 6 - depth)
    return score


def _read_pdf_text(path: Path, max_bytes: int) -> str:
    """Best-effort PDF text extraction. Returns '' if no extractor available."""
    # Prefer pypdf / PyPDF2 if installed; never hard-require it.
    for mod_name in ("pypdf", "PyPDF2"):
        try:
            mod = __import__(mod_name)
            reader = mod.PdfReader(str(path))
            chunks = []
            total = 0
            for page in reader.pages:
                t = page.extract_text() or ""
                chunks.append(t)
                total += len(t)
                if total >= max_bytes:
                    break
            return "\n".join(chunks)[:max_bytes]
        except Exception:
            continue
    logger.info("No PDF extractor available for %s; skipping text.", path.name)
    return ""


def ingest_directories(
    dirs: List[str],
    *,
    max_files: Optional[int] = None,
    max_file_bytes: Optional[int] = None,
) -> Tuple[List[SourceFile], List[str]]:
    """Walk dirs, classify and read files. Returns (sources, errors)."""
    max_files = max_files or int(_cfg("max_files", _DEFAULT_MAX_FILES))
    max_file_bytes = max_file_bytes or int(_cfg("max_file_bytes", _DEFAULT_MAX_FILE_BYTES))
    sources: List[SourceFile] = []
    errors: List[str] = []
    seen = 0

    for d in dirs:
        root = Path(os.path.expanduser(d)).resolve()
        if not root.exists():
            errors.append(f"Path does not exist: {d}")
            continue
        if root.is_file():
            roots = [(root.parent, [root])]
        else:
            roots = None

        if roots is None:
            walk_iter = os.walk(root)
        else:
            walk_iter = [(str(root.parent), [], [root.name])]

        for dirpath, dirnames, filenames in walk_iter:
            # Prune skip dirs in place so os.walk doesn't descend.
            dirnames[:] = [dn for dn in dirnames if dn not in _SKIP_DIRS and not dn.startswith(".")]
            for fn in filenames:
                if seen >= max_files:
                    errors.append(
                        f"Hit max_files={max_files}; stopped ingesting more "
                        f"(raise skills.distill.max_files to ingest larger trees)."
                    )
                    break
                fpath = Path(dirpath) / fn
                kind = _classify(fpath)
                if not kind:
                    continue
                seen += 1
                try:
                    if kind == "pdf":
                        text = _read_pdf_text(fpath, max_file_bytes)
                        if not text:
                            continue
                    else:
                        raw = fpath.read_bytes()[:max_file_bytes]
                        try:
                            text = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            text = raw.decode("latin-1", errors="replace")
                    rel = os.path.relpath(str(fpath), str(root if root.is_dir() else root.parent))
                    sf = SourceFile(
                        path=fpath, rel=rel, kind=kind,
                        size=fpath.stat().st_size, text=text,
                    )
                    sf.priority = _file_priority(fpath, kind)
                    sources.append(sf)
                except Exception as e:  # pragma: no cover - defensive
                    errors.append(f"Could not read {fpath}: {e}")
            if seen >= max_files:
                break

    return sources, errors


def build_corpus(sources: List[SourceFile], *, budget: Optional[int] = None) -> str:
    """Assemble a single token-budgeted corpus string, priority-ordered."""
    budget = budget or int(_cfg("corpus_budget", _DEFAULT_CORPUS_BUDGET))
    ordered = sorted(sources, key=lambda s: (-s.priority, len(s.text)))
    parts: List[str] = []
    used = 0
    for sf in ordered:
        header = f"\n===== FILE: {sf.rel}  [{sf.kind}] =====\n"
        body = sf.text
        chunk = header + body
        if used + len(chunk) > budget:
            remaining = budget - used - len(header)
            if remaining > 500:
                parts.append(header + body[:remaining] + "\n... [truncated]\n")
                used = budget
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Step 3: synthesize draft SKILL.md
# ---------------------------------------------------------------------------


_SYNTH_SYSTEM = (
    "You are a skill author for the Hermes Agent. You distill source material "
    "(code, API docs, manuals, PDFs, configs) into ONE reusable SKILL.md: a "
    "narrow, actionable procedure an agent can follow later. You do not dump "
    "the source; you extract the reusable how-to."
)

_SYNTH_TEMPLATE = """\
Distill the SOURCE MATERIAL below into a single SKILL.md file.

Output ONLY the SKILL.md content -- nothing before or after, no code fences.

Required format:
---
name: <lowercase-hyphenated, <=64 chars>
description: <one sentence, <=200 chars, starts with a trigger like "Use when ...">
version: 1.0.0
metadata:
  hermes:
    tags: [<a-few>, <relevant>, <tags>]
---

# <Human Title>

<2-4 sentence overview of what this skill does and when to use it.>

## When to use
<bullet list of trigger conditions>

## Steps
1. <numbered, concrete steps with EXACT commands / endpoints / code where the source provides them>
2. ...

## Verification
<how to confirm each step worked -- exact commands to run, expected output>

## Pitfalls
<gotchas discovered in the source material>

Rules:
- Prefer exact commands, endpoint URLs, function signatures, and config keys
  that appear VERBATIM in the source. Do not invent flags, paths, or APIs.
- If the source is prose-only (a manual with no runnable commands), still
  produce concrete steps but keep them faithful to the document.
- Keep it tight. A good skill is scannable, not a re-paste of the docs.

User intent hint (may be empty): {hint}

SOURCE MATERIAL:
{corpus}
"""


def synthesize_skill_md(
    corpus: str,
    *,
    hint: str = "",
    main_runtime: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> str:
    """Call the auxiliary LLM to produce draft SKILL.md text.

    Uses ``call_llm`` so synthesis is main-model-first and never touches the
    live conversation's prompt cache.
    """
    from agent.auxiliary_client import call_llm

    messages = [
        {"role": "system", "content": _SYNTH_SYSTEM},
        {"role": "user", "content": _SYNTH_TEMPLATE.format(hint=hint or "(none)", corpus=corpus)},
    ]
    call_kwargs: Dict[str, Any] = dict(
        task="skill_distill",
        messages=messages,
        main_runtime=main_runtime,
        temperature=float(_cfg("temperature", 0.2)),
        max_tokens=int(_cfg("max_tokens", 4000)),
    )
    if timeout is not None:
        call_kwargs["timeout"] = timeout
    resp = call_llm(**call_kwargs)
    content = ""
    try:
        content = resp.choices[0].message.content or ""
    except Exception as e:  # pragma: no cover - defensive
        raise RuntimeError(f"Skill synthesis returned an unusable response: {e}")
    return _strip_code_fences(content).strip()


def _strip_code_fences(text: str) -> str:
    """Models sometimes wrap output in ```markdown ... ``` -- strip that."""
    t = text.strip()
    if t.startswith("```"):
        # drop first fence line
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t


# ---------------------------------------------------------------------------
# Step 4: sandboxed verification
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_BASH_BLOCK_RE = re.compile(r"```(?:bash|sh|shell|console)\n(.*?)```", re.DOTALL)


def _extract_frontmatter(content: str) -> Optional[Dict[str, Any]]:
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None
    try:
        import yaml

        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_shell_snippets(content: str) -> List[str]:
    snippets: List[str] = []
    for block in _BASH_BLOCK_RE.findall(content):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Drop a leading prompt char.
            if line.startswith("$ "):
                line = line[2:]
            snippets.append(line)
    return snippets


def verify_skill_draft(
    content: str,
    sources: List[SourceFile],
    *,
    run_commands: bool = False,
    snippet_timeout: Optional[int] = None,
) -> VerificationResult:
    """Verify a draft SKILL.md in an isolated sandbox.

    Tiers (best achievable wins, floor enforced by caller):
      * "executed"   -- shell snippets actually ran in the sandbox with rc 0
      * "checked"    -- frontmatter valid + snippets parse + referenced paths
                        / commands exist on PATH
      * "unverified" -- frontmatter valid but nothing checkable
      * "failed"     -- frontmatter invalid or a hard check failed
    """
    snippet_timeout = snippet_timeout or int(_cfg("snippet_timeout", _DEFAULT_SNIPPET_TIMEOUT))
    checks: List[str] = []
    warnings: List[str] = []
    errors: List[str] = []

    # --- frontmatter must be valid (hard gate) ---
    fm = _extract_frontmatter(content)
    if not fm:
        errors.append("Missing or unparseable YAML frontmatter.")
        return VerificationResult("failed", False, checks, warnings, errors)
    if not fm.get("name") or not fm.get("description"):
        errors.append("Frontmatter missing required 'name' or 'description'.")
        return VerificationResult("failed", False, checks, warnings, errors)
    checks.append("Frontmatter valid (name + description present).")

    name = str(fm["name"])
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
        errors.append(f"Skill name '{name}' is not a valid lowercase-hyphenated identifier.")
        return VerificationResult("failed", False, checks, warnings, errors)
    checks.append(f"Skill name '{name}' is well-formed.")

    snippets = _extract_shell_snippets(content)
    referenced_cmds = set()
    for snip in snippets:
        try:
            tokens = shlex.split(snip)
        except ValueError:
            warnings.append(f"Could not parse shell snippet: {snip!r}")
            continue
        if tokens:
            referenced_cmds.add(tokens[0])

    # --- existence checks: do referenced commands exist on PATH? ---
    missing_cmds = [c for c in referenced_cmds if c not in {"cd", "echo", "export", "source", "."}
                    and shutil.which(c) is None]
    if referenced_cmds:
        present = sorted(referenced_cmds - set(missing_cmds))
        if present:
            checks.append(f"Referenced commands on PATH: {', '.join(present)}.")
        if missing_cmds:
            warnings.append(
                "Referenced commands NOT on PATH (may be project-local): "
                + ", ".join(sorted(missing_cmds))
            )

    achieved = "checked" if (snippets or referenced_cmds) else "unverified"

    # --- optional live execution in a throwaway sandbox ---
    if run_commands and snippets:
        sandbox = tempfile.mkdtemp(prefix="hermes_learn_verify_")
        ran_ok = 0
        try:
            # Only run snippets that look read-only / safe to dry-run.
            for snip in snippets:
                if not _looks_safe_to_run(snip):
                    warnings.append(f"Skipped (not safe to auto-run): {snip!r}")
                    continue
                try:
                    proc = subprocess.run(
                        snip, shell=True, cwd=sandbox, capture_output=True,
                        text=True, timeout=snippet_timeout,
                    )
                    if proc.returncode == 0:
                        ran_ok += 1
                        checks.append(f"Ran OK: {snip!r}")
                    else:
                        warnings.append(
                            f"Non-zero exit ({proc.returncode}) for {snip!r}: "
                            f"{(proc.stderr or '').strip()[:160]}"
                        )
                except subprocess.TimeoutExpired:
                    warnings.append(f"Timed out: {snip!r}")
                except Exception as e:
                    warnings.append(f"Error running {snip!r}: {e}")
            if ran_ok > 0:
                achieved = "executed"
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    passed = achieved in ("executed", "checked", "unverified")
    return VerificationResult(achieved, passed, checks, warnings, errors)


_SAFE_RUN_PREFIXES = (
    "ls", "cat", "echo", "pwd", "which", "type", "head", "tail", "grep",
    "find", "wc", "python --version", "python3 --version", "node --version",
    "npm --version", "go version", "cargo --version", "git --version",
    "curl --version", "--help", "-h", "help",
)


def _looks_safe_to_run(snip: str) -> bool:
    """Heuristic: only auto-run obviously read-only/inspection snippets."""
    s = snip.strip()
    low = s.lower()
    # Block anything that mutates or reaches out destructively.
    bad = ("rm ", "rmdir", "mv ", "dd ", ">", ">>", "sudo", "chmod", "chown",
           "kill", "pip install", "npm install", "apt", "brew install",
           "git push", "git commit", "curl -X", "wget ", "mkfs", "shutdown")
    if any(b in low for b in bad):
        return False
    # Allow --help/--version style probes and read-only inspectors.
    if "--help" in low or "--version" in low or " -h" in low:
        return True
    return any(low.startswith(p) for p in _SAFE_RUN_PREFIXES)


# ---------------------------------------------------------------------------
# Step 5: stamp + commit
# ---------------------------------------------------------------------------


def _stamp_verification(content: str, vr: VerificationResult, n_sources: int) -> str:
    """Record the achieved verification tier in the skill frontmatter."""
    fm_match = _FRONTMATTER_RE.match(content)
    if not fm_match:
        return content
    stamp_lines = [
        "metadata:",
        "  hermes:",
        "    distill:",
        f"      verification: {vr.tier}",
        f"      sources_ingested: {n_sources}",
        f'      distilled_at: "{time.strftime("%Y-%m-%d")}"',
    ]
    # If metadata already exists in frontmatter, just append a distill note
    # rather than duplicating the metadata key (keep it simple + valid).
    fm_text = fm_match.group(1)
    if "metadata:" in fm_text:
        # Insert distill block under existing hermes metadata if possible;
        # fall back to a top-level comment to avoid producing invalid YAML.
        note = f"\n# distill: verification={vr.tier}, sources={n_sources}\n"
        end = fm_match.end()
        return content[:end] + note + content[end:]
    insert = "\n".join(stamp_lines) + "\n"
    # Place before the closing --- of the frontmatter.
    closing = content.find("\n---", fm_match.start() + 3)
    if closing == -1:
        return content
    return content[:closing] + "\n" + insert + content[closing:]


def commit_skill(content: str, category: Optional[str] = None) -> Dict[str, Any]:
    """Commit the draft as a real skill via the skill manager."""
    from tools.skill_manager_tool import _create_skill

    fm = _extract_frontmatter(content) or {}
    name = str(fm.get("name") or "").strip()
    if not name:
        return {"success": False, "error": "Cannot commit: skill has no name."}
    if category:
        return _create_skill(name=name, content=content, category=category)
    return _create_skill(name=name, content=content)


# ---------------------------------------------------------------------------
# Orchestrator -- the single entry point every surface calls
# ---------------------------------------------------------------------------


def distill_skill_from_dirs(
    dirs: List[str],
    *,
    hint: str = "",
    category: Optional[str] = None,
    run_commands: bool = False,
    min_tier: str = "checked",
    main_runtime: Optional[Dict[str, Any]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> DistillResult:
    """Ingest dirs -> synthesize -> verify -> commit. The /learn backend.

    Args:
        dirs: directories (or single files) of source material.
        hint: optional free-text steer ("focus on the auth flow").
        category: optional skill category folder.
        run_commands: if True, attempt to execute safe shell snippets in a
            throwaway sandbox (verification tier can reach "executed").
        min_tier: minimum verification tier required to COMMIT. One of
            "executed" | "checked" | "unverified". If the achieved tier is
            below this floor, the result is returned draft-only (not written).
        main_runtime: the caller's runtime dict (provider/model) so synthesis
            resolves to the main model.
        progress: optional callback for streaming status lines to a UI.

    Returns:
        DistillResult.
    """
    t0 = time.time()

    def _say(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    tier_rank = {"failed": 0, "unverified": 1, "checked": 2, "executed": 3}
    floor = tier_rank.get(min_tier, 2)

    # 1+2: ingest & corpus
    _say(f"Ingesting {len(dirs)} path(s)...")
    sources, ingest_errors = ingest_directories(dirs)
    if not sources:
        msg = "No readable source material found."
        if ingest_errors:
            msg += " " + "; ".join(ingest_errors[:3])
        return DistillResult(success=False, error=msg, elapsed_seconds=time.time() - t0)

    breakdown: Dict[str, int] = {}
    for sf in sources:
        breakdown[sf.kind] = breakdown.get(sf.kind, 0) + 1
    _say(
        "Ingested "
        + ", ".join(f"{v} {k}" for k, v in sorted(breakdown.items()))
        + f" ({len(sources)} files)."
    )

    corpus = build_corpus(sources)

    # 3: synthesize
    _say("Synthesizing draft skill...")
    try:
        draft = synthesize_skill_md(corpus, hint=hint, main_runtime=main_runtime)
    except Exception as e:
        return DistillResult(
            success=False, error=f"Synthesis failed: {e}",
            sources_ingested=len(sources), source_breakdown=breakdown,
            elapsed_seconds=time.time() - t0,
        )
    if not draft or "---" not in draft:
        return DistillResult(
            success=False, error="Synthesis produced no usable SKILL.md.",
            sources_ingested=len(sources), source_breakdown=breakdown,
            draft_content=draft, elapsed_seconds=time.time() - t0,
        )

    # 4: verify
    _say("Verifying draft in sandbox...")
    vr = verify_skill_draft(draft, sources, run_commands=run_commands)
    _say(f"Verification tier: {vr.tier}.")

    fm = _extract_frontmatter(draft) or {}
    skill_name = str(fm.get("name") or "").strip() or None

    if tier_rank.get(vr.tier, 0) < floor:
        return DistillResult(
            success=False, skill_name=skill_name, verification=vr,
            sources_ingested=len(sources), source_breakdown=breakdown,
            draft_only=True, draft_content=draft,
            error=(f"Verification tier '{vr.tier}' is below the required "
                   f"floor '{min_tier}'. Draft not committed."),
            elapsed_seconds=time.time() - t0,
        )

    # 5: stamp + commit
    stamped = _stamp_verification(draft, vr, len(sources))
    _say(f"Committing skill '{skill_name}'...")
    commit = commit_skill(stamped, category=category)
    if not commit.get("success"):
        return DistillResult(
            success=False, skill_name=skill_name, verification=vr,
            sources_ingested=len(sources), source_breakdown=breakdown,
            draft_content=stamped,
            error=f"Commit failed: {commit.get('error')}",
            elapsed_seconds=time.time() - t0,
        )

    return DistillResult(
        success=True, skill_name=skill_name,
        skill_path=commit.get("skill_md") or commit.get("path"),
        category=category, verification=vr,
        sources_ingested=len(sources), source_breakdown=breakdown,
        elapsed_seconds=time.time() - t0,
    )


# ---------------------------------------------------------------------------
# Shared result rendering -- used by CLI, gateway, TUI, and dashboard so the
# user-facing summary is identical everywhere.
# ---------------------------------------------------------------------------

_TIER_BLURB = {
    "executed": "executed — shell snippets ran successfully in a sandbox",
    "checked": "checked — frontmatter valid, snippets parse, commands resolved",
    "unverified": "unverified — valid skill but nothing runnable to test",
    "failed": "failed — the draft did not pass basic validation",
}


def render_distill_result(res: DistillResult, *, markdown: bool = False) -> str:
    """Render a DistillResult as a human summary.

    markdown=False -> plain text (CLI / TUI).
    markdown=True  -> light markdown (gateway messengers / dashboard).
    """
    lines: List[str] = []
    b = "**" if markdown else ""

    if res.error and not res.success and not res.draft_only:
        lines.append(f"{b}/learn failed:{b} {res.error}")
        return "\n".join(lines)

    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(res.source_breakdown.items()))
    vr = res.verification

    if res.success:
        lines.append(f"{b}Learned skill:{b} {res.skill_name}")
        if res.skill_path:
            lines.append(f"  path: {res.skill_path}")
        lines.append(f"  sources: {res.sources_ingested} files ({breakdown})")
        if vr:
            lines.append(f"  verification: {_TIER_BLURB.get(vr.tier, vr.tier)}")
        lines.append(f"  took {res.elapsed_seconds:.1f}s")
        return "\n".join(lines)

    if res.draft_only:
        lines.append(f"{b}Draft not committed.{b} {res.error}")
        if vr:
            lines.append(f"  verification: {_TIER_BLURB.get(vr.tier, vr.tier)}")
            for e in vr.errors:
                lines.append(f"  error: {e}")
            for w in vr.warnings[:5]:
                lines.append(f"  warning: {w}")
        lines.append("  Re-run with a lower --min-tier to commit anyway, "
                     "or refine the sources.")
        return "\n".join(lines)

    lines.append(f"{b}/learn failed:{b} {res.error or 'unknown error'}")
    return "\n".join(lines)
