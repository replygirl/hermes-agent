"""Tests for the skill distillation engine (``/learn`` backend).

Covers the deterministic, non-LLM machinery: ingest/classify, corpus
assembly, sandboxed verification tiers, frontmatter stamping, and the
tier-floor gating in the orchestrator. The LLM synthesis step is stubbed so
these tests are hermetic (no network, no credentials).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent import skill_distill as sd


def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text(
        "# WidgetAPI\nFetch widgets.\n\n## Usage\n```bash\necho hello\nls\n```\n"
    )
    (src / "client.py").write_text("def get_widget(i):\n    return i\n")
    (src / "api.md").write_text("# Endpoints\nGET /widgets/{id}\n")
    nm = src / "node_modules"
    nm.mkdir()
    (nm / "junk.js").write_text("x" * 500)
    return src


def test_ingest_classifies_and_prunes_skip_dirs(tmp_path):
    src = _make_src(tmp_path)
    sources, errors = sd.ingest_directories([str(src)])
    rels = sorted(s.rel for s in sources)
    kinds = {s.kind for s in sources}
    assert "README.md" in rels
    assert "client.py" in rels
    assert "api.md" in rels
    # node_modules must be pruned, never ingested.
    assert all("node_modules" not in r for r in rels)
    assert kinds == {"code", "doc"}


def test_ingest_missing_path_reports_error(tmp_path):
    sources, errors = sd.ingest_directories([str(tmp_path / "nope")])
    assert sources == []
    assert any("does not exist" in e for e in errors)


def test_build_corpus_prioritizes_docs_over_code(tmp_path):
    src = _make_src(tmp_path)
    sources, _ = sd.ingest_directories([str(src)])
    corpus = sd.build_corpus(sources)
    # README (high-signal doc) should appear before client.py (code).
    assert corpus.find("README.md") < corpus.find("client.py")


def test_build_corpus_respects_budget(tmp_path):
    src = _make_src(tmp_path)
    sources, _ = sd.ingest_directories([str(src)])
    corpus = sd.build_corpus(sources, budget=120)
    assert len(corpus) <= 400  # budget + one header's slack


_VALID_DRAFT = """---
name: widget-api
description: Use when fetching widgets from the WidgetAPI.
version: 1.0.0
---

# Widget API

Use when fetching widgets.

## Steps
1. Inspect:
```bash
echo hello
ls
```
"""


def test_verify_failed_without_frontmatter(tmp_path):
    src = _make_src(tmp_path)
    sources, _ = sd.ingest_directories([str(src)])
    vr = sd.verify_skill_draft("just prose", sources)
    assert vr.tier == "failed"
    assert not vr.passed


def test_verify_checked_tier_without_running(tmp_path):
    src = _make_src(tmp_path)
    sources, _ = sd.ingest_directories([str(src)])
    vr = sd.verify_skill_draft(_VALID_DRAFT, sources, run_commands=False)
    # Snippets parse + commands resolve, but nothing ran.
    assert vr.tier == "checked"
    assert vr.passed


def test_verify_executed_tier_when_running(tmp_path):
    src = _make_src(tmp_path)
    sources, _ = sd.ingest_directories([str(src)])
    vr = sd.verify_skill_draft(_VALID_DRAFT, sources, run_commands=True)
    # echo / ls are safe to auto-run -> at least one ran rc 0.
    assert vr.tier == "executed"
    assert vr.passed


def test_verify_rejects_bad_name():
    bad = _VALID_DRAFT.replace("name: widget-api", "name: Not A Valid Name!")
    vr = sd.verify_skill_draft(bad, [])
    assert vr.tier == "failed"


def test_unsafe_snippets_not_run():
    assert sd._looks_safe_to_run("echo hello") is True
    assert sd._looks_safe_to_run("ls -la") is True
    assert sd._looks_safe_to_run("python3 --version") is True
    assert sd._looks_safe_to_run("rm -rf /") is False
    assert sd._looks_safe_to_run("pip install evil") is False
    assert sd._looks_safe_to_run("curl -X POST http://x") is False
    assert sd._looks_safe_to_run("echo x > file") is False


def test_stamp_records_verification_tier():
    vr = sd.VerificationResult("checked", True)
    stamped = sd._stamp_verification(_VALID_DRAFT, vr, 3)
    assert "checked" in stamped
    # Frontmatter must still be parseable after stamping.
    fm = sd._extract_frontmatter(stamped)
    assert fm and fm.get("name") == "widget-api"


def test_orchestrator_blocks_below_floor(tmp_path, monkeypatch):
    src = _make_src(tmp_path)
    # Stub synthesis to return a draft with no runnable snippets -> 'unverified'.
    draft = (
        "---\nname: prose-skill\n"
        "description: Use when reading a manual with no commands.\n"
        "version: 1.0.0\n---\n\n# Prose Skill\n\nJust prose, no code.\n"
    )
    monkeypatch.setattr(sd, "synthesize_skill_md", lambda *a, **k: draft)
    res = sd.distill_skill_from_dirs([str(src)], min_tier="checked")
    assert res.success is False
    assert res.draft_only is True
    assert res.verification is not None
    assert res.verification.tier == "unverified"


def test_orchestrator_commits_when_floor_met(tmp_path, monkeypatch):
    # Isolated HERMES_HOME so the commit writes nowhere real.
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    src = _make_src(tmp_path)
    monkeypatch.setattr(sd, "synthesize_skill_md", lambda *a, **k: _VALID_DRAFT)
    res = sd.distill_skill_from_dirs(
        [str(src)], min_tier="checked", run_commands=False
    )
    assert res.success is True, res.error
    assert res.skill_name == "widget-api"
    assert res.skill_path and Path(res.skill_path).exists()


def test_render_distill_result_shapes():
    ok = sd.DistillResult(
        success=True, skill_name="x", skill_path="/p",
        verification=sd.VerificationResult("checked", True),
        sources_ingested=2, source_breakdown={"doc": 2},
    )
    out = sd.render_distill_result(ok)
    assert "Learned skill" in out and "checked" in out

    blocked = sd.DistillResult(
        success=False, draft_only=True, skill_name="x",
        verification=sd.VerificationResult("unverified", True),
        error="below floor",
    )
    out2 = sd.render_distill_result(blocked, markdown=True)
    assert "Draft not committed" in out2
