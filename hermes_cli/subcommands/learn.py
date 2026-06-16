"""``hermes learn`` subcommand parser.

Distills a reusable skill from one or more directories of source material
(source code, API docs, instruction manuals, PDFs, configs). Backed by
``agent.skill_distill.distill_skill_from_dirs``.

Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_learn_parser(subparsers, *, cmd_learn: Callable) -> None:
    """Attach the ``learn`` subcommand to ``subparsers``."""
    learn_parser = subparsers.add_parser(
        "learn",
        help="Distill a reusable skill from directories of source material",
        description=(
            "Point Hermes at one or more directories (source code, API docs, "
            "instruction manuals, PDFs, configs). It ingests the material, "
            "synthesizes a draft SKILL.md, verifies it in a sandbox, and "
            "commits the skill only when verification passes."
        ),
    )
    learn_parser.add_argument(
        "paths",
        nargs="+",
        help="One or more directories (or files) of source material",
    )
    learn_parser.add_argument(
        "--hint",
        default="",
        help="Free-text steer for the distillation (e.g. 'focus on the auth flow')",
    )
    learn_parser.add_argument(
        "--category",
        default="",
        help="Category folder to place the new skill under",
    )
    learn_parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "Attempt to execute safe, read-only shell snippets from the draft "
            "in a throwaway sandbox (verification can reach the 'executed' tier). "
            "Off by default."
        ),
    )
    learn_parser.add_argument(
        "--min-tier",
        default="checked",
        choices=["executed", "checked", "unverified"],
        help=(
            "Minimum verification tier required to commit the skill "
            "(default: checked). Drafts below the floor are shown but not saved."
        ),
    )
    learn_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON instead of a human summary",
    )
    learn_parser.set_defaults(func=cmd_learn)
