"""Draft a PR title/body and a short branch slug from a run's own artifacts.

Runs once, at `gantry ship` time, after review has approved. Feeds the same
artifact set the reviewer saw (spec/plan/build-summary/evidence) to a cheap
agent call and asks for a conventional-commit title, a PR body in the shape a
human would write by hand, and a short kebab-case branch slug — no mention of
the pipeline that produced it; the PR should read like normal engineering work.

Falls back to the run's own `title` field (deterministic, always available) if
the agent call fails or returns something unusable — ship must never block on
this being fancy.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .backends.registry import get_execution_runner as get_runner
from .config import GantryConfig
from .profiles import profile_for, snapshot_profile
from .state import RunStore, slugify

logger = logging.getLogger(__name__)

SHIP_ARTIFACTS = ["product-spec.md", "build-summary.md", "evidence-report.md"]

_PROMPT = """You are drafting a pull request for a change that is already built, \
tested, and independently reviewed (APPROVE). Do not re-review it — just describe \
it well, the way an engineer writes their own PR.

Read the artifacts below (spec = what/why, build-summary = what actually changed, \
evidence-report = proof it works) and the git diff, then produce:

1. A conventional-commit-style title, one line, under 70 chars \
   (e.g. "fix(payments): retry stuck webhook deliveries").
2. A PR body in GitHub markdown with a `## Summary` (what changed and why, \
   written for a reviewer who hasn't seen the artifacts) and a `## Test plan` \
   (checklist of what was actually verified, from evidence-report.md).
3. A short branch slug: 2-5 lowercase words, hyphen-separated, optionally \
   prefixed with a conventional type (`feat/`, `fix/`, `chore/`, `refactor/`), \
   e.g. `chore/remove-dead-webhook-handler`. No dates, no run IDs, no "gantry".
4. A short rollback note: what CODE/BEHAVIOR-OBSERVABLE sign would indicate this \
   change needs reverting (e.g. "if the /webhooks endpoint starts returning 500s" \
   or "if test_webhook_retry starts flaking"), plus a CONCRETE rollback procedure \
   referencing the actual diff/build-summary.md (e.g. "revert this PR; if a \
   migration was added its down-step is in <file>" — name the real file/step if \
   one exists, say plainly if there is none, e.g. no schema change to revert). Do \
   NOT invent generic boilerplate like "monitor metrics and roll back if needed" \
   — there is no live monitoring here, only what a human can observe in behavior \
   or re-running tests/checks. If you genuinely can't identify anything more \
   specific than "revert this PR", say exactly that — an honest minimal note \
   beats a padded fake one.

Do not mention any pipeline, tool, or agent that produced this change anywhere \
in the title, body, or slug — write as if a human engineer did this themselves.

Reply with ONLY a JSON object, no prose before or after:
{"title": "...", "body": "...", "branch_slug": "...", "rollback_note": "..."}

# Artifacts
"""


def _build_prompt(store: RunStore, run_id: str, cwd: Path, base: str) -> str:
    parts = [_PROMPT]
    for name in SHIP_ARTIFACTS:
        content = store.read_artifact(run_id, name)
        parts.append(f"\n--- {name} ---\n{(content or '<MISSING>')[:12000]}")
    diff = _git_diff(cwd, base)
    parts.append(f"\n\n# Git diff vs {base}\n{diff}")
    return "".join(parts)


def _git_diff(cwd: Path, base: str) -> str:
    import subprocess
    proc = subprocess.run(["git", "diff", base, "--"], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120)
    return proc.stdout[:50000]


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        logger.debug("ship draft JSON extraction failed to parse", exc_info=True)
        return None


def _valid(draft: dict[str, Any] | None) -> bool:
    # rollback_note is intentionally NOT required here — an agent response
    # missing everything else is unusable and falls back wholesale, but a
    # response with a good title/body/branch_slug and a missing/empty
    # rollback_note still ships fine with the fallback rollback_note alone
    # (see draft_ship_meta's return below); ship must never block on the
    # rollback note specifically being present.
    if not draft:
        return False
    return bool(draft.get("title")) and bool(draft.get("body")) and bool(draft.get("branch_slug"))


def _slugify_branch(slug: str) -> str:
    """Normalize a model-provided branch slug: keep an optional type/ prefix,
    slugify the rest, cap length so it stays readable in `git branch -a`."""
    prefix = ""
    rest = slug
    if "/" in slug:
        prefix, rest = slug.split("/", 1)
        prefix = re.sub(r"[^a-z]", "", prefix.lower()) or "chore"
        prefix += "/"
    body = slugify(rest)[:50] or "change"
    return f"{prefix}{body}"


def draft_ship_meta(store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path) -> dict[str, str]:
    """Best-effort PR title/body/branch-slug(/rollback_note) draft. Always
    returns usable values — falls back to the run's stored title (and a
    minimal honest rollback fallback) on any failure so ship never blocks."""
    fallback_title = store.state(run_id).get("title", run_id)
    fallback = {
        "title": fallback_title,
        "body": f"## Summary\n\n{fallback_title}",
        "branch_slug": slugify(fallback_title),
        # No agent call happened (or it failed/was unusable) — the only
        # honest rollback note available without one is the generic "revert
        # this PR"; no fabricated trigger-condition/procedure detail.
        "rollback_note": "Revert this PR to roll back.",
    }

    try:
        prompt = _build_prompt(store, run_id, cwd, cfg.git.base_branch)
        profile = profile_for("ship-metadata", cfg)
        if profile.prompt_preamble:
            prompt = f"{profile.prompt_preamble}\n\n{prompt}"
        store.write_log(run_id, "ship-prompt.md", prompt)
        store.write_log(
            run_id, "ship-profile.json", json.dumps(snapshot_profile(profile), indent=2))
        runner = get_runner(profile.backend)
        result = runner.run(
            cwd=cwd, prompt=prompt, model=profile.model,
            session_id=None, plan_mode=False,
            skip_permissions=profile.permissions == "allow",
            output_format="json", session_name=f"{run_id}-ship",
            max_turns=profile.turn_budget, timeout=profile.timeout,
        )
        from .cost import accumulate as _accumulate_cost
        _accumulate_cost(store, run_id, "ship", result.usage,
                         runner=runner.name, session_id=result.session_id)
        text = result.raw.get("result", "") if isinstance(result.raw, dict) else result.stdout
        draft = _extract_json(str(text)) if result.ok else None
        if not _valid(draft):
            return fallback
        return {
            "title": str(draft["title"]).strip()[:100] or fallback["title"],
            "body": str(draft["body"]).strip() or fallback["body"],
            "branch_slug": _slugify_branch(str(draft["branch_slug"]).strip()) or fallback["branch_slug"],
            "rollback_note": str(draft.get("rollback_note") or "").strip() or fallback["rollback_note"],
        }
    except Exception:
        logger.debug("ship draft generation failed, using fallback title", exc_info=True)
        return fallback
