"""Independent LLM review stage.

Runs after evidence, as a genuine agentic investigation — not a single prompt
stuffed with truncated artifact snippets. The reviewing agent runs inside the
run's own worktree (same `cwd` build/evidence used) with its normal file/shell
tools, and is pointed at the full, untruncated artifacts (which live in the
target repo's .agent-runs/<run_id>/, not the worktree) plus instructed to run
its own `git diff` and cross-reference the plan/acceptance-criteria against
what was actually implemented. Ideally a different model family than the
builder. Verdicts:

  APPROVE          -> review_approved
  REQUEST_CHANGES  -> review_changes_requested (+ review-comments.md; resume build)
  ESCALATE         -> review_escalated (human decision)

Two-axis mode (cfg.review.two_axis, default True): instead of one investigation
producing one verdict, review runs TWO independent axes in parallel, each its
own agent session, neither sharing the other's reasoning:

  - Spec axis       (review-spec.md)      — does the diff satisfy the spec/
                                            acceptance criteria, match the
                                            architecture, stay in scope, and
                                            do evidence's claims hold up?
  - Standards axis  (review-standards.md) — does the diff follow this repo's
                                            own documented conventions plus a
                                            fixed code-smell baseline?

A run only proceeds toward review_approved if BOTH axes APPROVE. Either axis
REQUEST_CHANGES -> combined REQUEST_CHANGES (both axes' findings surfaced).
Either axis ESCALATE -> combined ESCALATE (escalation always wins). This is
roughly 2x the LLM calls of single-axis review — the cost/latency tradeoff is
deliberate: catching a spec-conformant-but-badly-built diff (or vice versa)
that a single merged verdict would gloss over is worth the extra call. Set
cfg.review.two_axis = False to restore the exact legacy single-verdict,
single-session behavior (see `_run_review_single`).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import herdr as _herdr
from .config import GantryConfig
from .redact import proxy_secrets, redact_secrets
from .runners import get_runner, resolve_proxy_env
from .state import RunStore
from .status import Status

logger = logging.getLogger(__name__)

REVIEW_ARTIFACTS = [
    "intake.md", "product-spec.md", "architecture-design.md",
    "implementation-plan.md", "build-summary.md", "evidence-report.md",
    "scope.json", "checks.json",
]

# RunStore.save_session/write_result do a read-modify-write over one shared
# JSON file per run (sessions.json, cost.json) with no file locking of their
# own — fine for the existing serial call pattern every other stage uses, but
# the two axes run truly concurrently (ThreadPoolExecutor, real OS threads)
# and both call save_session (their own session key) and cost.accumulate
# (their own stage key) against the SAME underlying file. Two threads racing
# read-modify-write on the same file corrupts it (one thread's write clobbers
# the other's, or interleaves invalid JSON) — this lock serializes just those
# calls so each read-modify-write cycle completes atomically relative to the
# other axis, without limiting the actual LLM-call concurrency the two axes
# get from the runner subprocess itself (which releases the GIL while blocked).
_SHARED_STATE_LOCK = threading.Lock()

VALID_FINDING_ACTIONS = {"blocking", "ask-user", "no-op"}
# Fail-closed default (Task 2/CONFIRMED decisions): a finding with a missing,
# empty, or unrecognized `action` value is NEVER silently dropped or treated
# as a no-op — it defaults to "ask-user" so a human sees it.
FAIL_CLOSED_ACTION = "ask-user"


_EVIDENCE_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
# Same fenced-```json-block extraction technique as evidence's structured
# summary (_EVIDENCE_JSON_BLOCK_RE / _structured_evidence_summary) — mirrored
# here for per-axis findings rather than sharing the same compiled regex
# object, so each parser's "what shape am I looking for" stays independent
# and neither can be accidentally tightened/loosened by the other's needs.
_FINDINGS_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


def _structured_evidence_summary(store: RunStore, run_id: str) -> dict[str, Any] | None:
    """Parse the trailing ```json block evidence.md writes when
    [evidence].output_format = "structured" (see
    Engine._evidence_output_directive). Returns None if evidence-report.md
    has no such block (prose-only evidence, the default — no behavior change)
    or if what's there doesn't parse as valid JSON with the expected keys."""
    evidence = store.read_artifact(run_id, "evidence-report.md")
    if not evidence:
        return None
    # Last match, not first: an "append ## Pass N" resumed evidence report can
    # have older JSON blocks from earlier passes still in the file — only the
    # most recent pass's summary reflects the current state.
    matches = _EVIDENCE_JSON_BLOCK_RE.findall(evidence)
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "pass_count" not in data:
        return None
    return data


def _parse_findings(text: str) -> list[dict[str, str]] | None:
    """Parse the trailing ```json findings block an axis's response is
    required to include (see review-spec.md/review-standards.md's "Required
    output" section). Mirrors _structured_evidence_summary's exact approach:
    take the LAST fenced ```json block (a resumed axis session's response may
    theoretically contain more than one such block across its own reasoning),
    parse it, validate the expected `findings` key.

    Returns None (not an empty list) when no block is present or it fails to
    parse/validate — this is the signal `_derive_axis_result` uses to fall
    back to keyword verdict parsing instead of silently treating "couldn't
    parse structured findings" the same as "the reviewer found nothing".

    Fail-closed per finding: a finding whose `action` is missing, empty, or
    not one of blocking/ask-user/no-op is coerced to "ask-user" — a finding
    is NEVER silently dropped just because its action tag was malformed, and
    is NEVER defaulted to the more lenient "no-op"."""
    matches = _FINDINGS_JSON_BLOCK_RE.findall(text or "")
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "findings" not in data:
        return None
    findings = data.get("findings")
    if not isinstance(findings, list):
        return None
    out: list[dict[str, str]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action not in VALID_FINDING_ACTIONS:
            # Fail-closed: missing/empty/unrecognized action -> ask-user,
            # never dropped, never silently no-op.
            action = FAIL_CLOSED_ACTION
        out.append({
            "severity": str(item.get("severity") or "Suggestion"),
            "action": action,
            "location": str(item.get("location") or ""),
            "description": str(item.get("description") or ""),
            "recommendation": str(item.get("recommendation") or ""),
        })
    return out


def _findings_verdict(findings: list[dict[str, str]]) -> str:
    """Per CONFIRMED decisions: any `blocking` finding -> REQUEST_CHANGES.
    Zero blocking (whether zero findings, only no-op, or only ask-user) ->
    APPROVE is still possible for this axis — ask-user findings are surfaced
    prominently in the combined notification (see advance.py) regardless of
    whether they blocked the verdict."""
    if any(f["action"] == "blocking" for f in findings):
        return "REQUEST_CHANGES"
    return "APPROVE"


def _derive_axis_result(text: str, cfg: GantryConfig, ok: bool) -> dict[str, Any]:
    """Combine structured findings (preferred) with the existing keyword-based
    `_parse_verdict` fallback for one axis's raw response text.

    - Runner call itself failed (ok=False): ESCALATE, no findings to parse.
    - Structured findings JSON present and parses: derive the verdict from
      findings (see `_findings_verdict`) UNLESS the reviewer's own prose
      explicitly declared ESCALATE (via the same keyword matching as before)
      — an explicit escalation declaration always wins over a findings-derived
      APPROVE/REQUEST_CHANGES, matching the "escalation always wins"
      philosophy applied at the combined-verdict level too.
    - Structured findings JSON missing/unparseable: fall back to the EXISTING
      `_parse_verdict` keyword-matching logic, and log clearly that this
      happened — the axis never silently escalates OR silently approves with
      no signal that structured findings were unavailable for this pass.
    """
    if not ok:
        return {"verdict": "ESCALATE", "findings": [], "findings_source": "runner_failed"}

    text_verdict = _parse_verdict(text, cfg)
    findings = _parse_findings(text)
    if findings is None:
        logger.warning(
            "review axis response had no parseable structured findings JSON block — "
            "falling back to keyword-based verdict parsing for this pass "
            "(verdict=%s)", text_verdict)
        return {"verdict": text_verdict, "findings": [], "findings_source": "keyword_fallback"}

    if text_verdict == "ESCALATE":
        return {"verdict": "ESCALATE", "findings": findings, "findings_source": "structured"}
    return {"verdict": _findings_verdict(findings), "findings": findings, "findings_source": "structured"}


def _combine_axis_verdicts(spec_verdict: str, standards_verdict: str) -> str:
    """CONFIRMED combination rule: either axis ESCALATE -> ESCALATE (wins over
    everything, matching this codebase's conservative-default philosophy).
    Else either axis REQUEST_CHANGES -> REQUEST_CHANGES. Only BOTH APPROVE ->
    APPROVE."""
    if spec_verdict == "ESCALATE" or standards_verdict == "ESCALATE":
        return "ESCALATE"
    if spec_verdict == "REQUEST_CHANGES" or standards_verdict == "REQUEST_CHANGES":
        return "REQUEST_CHANGES"
    return "APPROVE"


def _rebuild_diff_context(store: RunStore, run_id: str) -> str:
    """On a resumed review (this run went REQUEST_CHANGES -> rebuild ->
    evidence -> review again), tell the reviewer what changed since its own
    last verdict — without this, a resumed reviewer has no visibility into
    what the rebuild actually did differently and can end up repeating
    feedback the rebuild already addressed, or missing that its own prior
    concern was fixed. Uses the previous review-result.json's own recorded
    text (there's always exactly one prior verdict to compare against by the
    time a second review call happens) plus whatever review-comments.md said
    was requested — cheaper and more reliable than diffing two
    build-summary.md snapshots, since build-summary.md already documents its
    own pass-over-pass changes via its own "## Pass N" append convention.

    Handles both review-result.json shapes: the legacy flat
    {"verdict": ...} (two_axis=False) and the two-axis
    {"two_axis": true, "combined_verdict": ...} shape — either way, only a
    prior REQUEST_CHANGES triggers this context, and review-comments.md
    already carries BOTH axes' feedback when it was a two-axis run (see
    `_combined_comments_md`), so every axis's resumed session sees the full
    picture regardless of which axis raised the original concern."""
    prior_result = store.read_result(run_id, "review-result.json")
    prior_verdict = (prior_result.get("combined_verdict") if prior_result.get("two_axis")
                      else prior_result.get("verdict"))
    if not prior_result or prior_verdict != "REQUEST_CHANGES":
        return ""
    comments = store.read_artifact(run_id, "review-comments.md") or ""
    return (
        f"\n# This is a RE-review after your own prior REQUEST_CHANGES verdict\n"
        f"Your previous feedback (from review-comments.md, what you asked to be fixed):\n"
        f"{comments}\n\n"
        f"Check specifically whether build-summary.md's latest `## Pass N` section "
        f"addresses each point above before evaluating anything else — don't repeat "
        f"feedback that pass already addressed, and don't assume it was addressed "
        f"without checking.\n"
    )


def _checklist_items_section(items: list[str], heading: str = "Required checklist") -> str:
    if not items:
        return ""
    lines = "\n".join(f"- {item}" for item in items)
    return f"\n# {heading} — address EACH item explicitly in your response\n{lines}\n"


def _checklist_section(cfg: GantryConfig) -> str:
    return _checklist_items_section(cfg.review.checklist)


def _high_risk_section(high_risk_files: list[str]) -> str:
    """Wires in ScopeConfig.high_risk_paths (checks.py::run_scope_guard's
    scope.json `high_risk_files`) — when this run touched any project-declared
    high-risk path, both axes get an explicit extra-scrutiny instruction, and
    are asked to say so in their Verification Story (Task 4)."""
    if not high_risk_files:
        return ""
    files = "\n".join(f"- `{f}`" for f in high_risk_files)
    return (
        f"\n# High-risk files — give these extra scrutiny\n"
        f"The following changed file(s) matched this project's configured "
        f"`[scope].high_risk_paths` and warrant deeper investigation than the "
        f"rest of the diff (do not just skim them). State explicitly in your "
        f"Verification Story how you gave them extra scrutiny.\n{files}\n"
    )


def _build_prompt(store: RunStore, run_id: str, cwd: Path, base: str, template: str,
                  cfg: GantryConfig | None = None) -> str:
    """Legacy single-axis prompt builder — used ONLY by `_run_review_single`
    (cfg.review.two_axis == False). Unchanged from before two-axis review
    existed; do not add two-axis-only concerns (high-risk section, per-axis
    checklist) here, they belong in `_build_axis_prompt`."""
    run_dir = store.run_dir(run_id)
    artifact_lines = []
    for name in REVIEW_ARTIFACTS:
        path = store.artifact_path(run_id, name)
        artifact_lines.append(f"- {path} {'(exists)' if path.exists() else '(missing)'}")
    parts = [
        template.replace("{RUN_ID}", run_id),
        "\n\n# Investigation instructions\n",
        f"You are running inside the implementation worktree at {cwd}. The run's "
        f"planning/evidence artifacts live in a separate directory, {run_dir} "
        f"(NOT inside this worktree) — read them directly with your file tools:\n",
        "\n".join(artifact_lines),
        f"\n\nTo see the actual code changes, run `git diff {base} --` yourself in "
        f"this worktree — do not rely on any diff text pasted into this prompt, "
        f"there isn't one; read the real files and the real diff.\n"
        f"\nCross-reference: does the diff satisfy every acceptance criterion in "
        f"product-spec.md? Does it match the scope and approach in "
        f"implementation-plan.md? Do the claims in evidence-report.md hold up "
        f"against what you can independently verify (read the actual test files, "
        f"re-run tests/checks if useful)? Investigate as deeply as needed before "
        f"deciding.\n",
    ]
    structured = _structured_evidence_summary(store, run_id)
    if structured:
        parts.append(
            f"\n# Evidence stage's own structured summary (pre-digested, still verify "
            f"independently — do not treat this as ground truth on its own)\n"
            f"```json\n{json.dumps(structured, indent=2)}\n```\n"
        )
    parts.append(_rebuild_diff_context(store, run_id))
    if cfg is not None:
        parts.append(_checklist_section(cfg))
    return "".join(parts)


def _build_axis_prompt(store: RunStore, run_id: str, cwd: Path, base: str, template: str,
                       cfg: GantryConfig, checklist_items: list[str],
                       high_risk_files: list[str]) -> str:
    """Two-axis prompt builder — appends shared, deterministic context (which
    artifacts exist, evidence's structured summary, re-review context, this
    axis's own checklist, high-risk callout) to whichever axis template
    (review-spec.md / review-standards.md) was already loaded. Each axis
    template owns its own "Investigation instructions"/"What to evaluate"
    prose (they read very differently — see the templates themselves), so
    unlike `_build_prompt` this does not inject a cross-reference paragraph."""
    run_dir = store.run_dir(run_id)
    artifact_lines = []
    for name in REVIEW_ARTIFACTS:
        path = store.artifact_path(run_id, name)
        artifact_lines.append(f"- {path} {'(exists)' if path.exists() else '(missing)'}")
    parts = [
        template.replace("{RUN_ID}", run_id),
        "\n\n# Artifact locations\n",
        f"You are running inside the implementation worktree at {cwd}. The run's "
        f"planning/evidence artifacts live in a separate directory, {run_dir} "
        f"(NOT inside this worktree) — read them directly with your file tools:\n",
        "\n".join(artifact_lines),
        f"\n\nDiff base branch: `{base}`. Run `git diff {base} --` yourself in this "
        f"worktree — do not rely on any diff text pasted into this prompt, there "
        f"isn't one; read the real files and the real diff.\n",
    ]
    structured = _structured_evidence_summary(store, run_id)
    if structured:
        parts.append(
            f"\n# Evidence stage's own structured summary (pre-digested, still verify "
            f"independently — do not treat this as ground truth on its own)\n"
            f"```json\n{json.dumps(structured, indent=2)}\n```\n"
        )
    parts.append(_rebuild_diff_context(store, run_id))
    parts.append(_checklist_items_section(checklist_items))
    parts.append(_high_risk_section(high_risk_files))
    return "".join(parts)


def _line_start_match(keywords: list[str], text: str) -> bool:
    """True if any keyword is the first token of some line in text (allowing
    common markdown/emphasis prefixes like `**`, `#`, `-`, whitespace before
    it) — used by keyword_mode="line_start" to require a real verdict
    declaration rather than the word merely appearing somewhere in prose."""
    for line in text.splitlines():
        stripped = line.strip().lstrip("#*_- ").strip()
        for kw in keywords:
            if stripped.upper().startswith(kw.upper()):
                return True
    return False


def _parse_verdict(text: str, cfg: GantryConfig) -> str:
    text = text or ""
    upper = text.upper()
    mode = cfg.review.keyword_mode
    matches = (lambda kws: _line_start_match(kws, text)) if mode == "line_start" else (
        lambda kws: any(k.upper() in upper for k in kws))
    if matches(cfg.review.request_changes_keywords):
        return "REQUEST_CHANGES"
    if matches(cfg.review.escalate_keywords):
        return "ESCALATE"
    if matches(cfg.review.approve_keywords):
        return "APPROVE"
    return "ESCALATE"


def _report_herdr(cfg: GantryConfig, run_id: str, status: str) -> None:
    """Best-effort herdr sidebar report; never raises, mirrors Engine._set_status."""
    try:
        _herdr.report_state(run_id, status, enabled=cfg.herdr.enabled and cfg.herdr.report_state)
    except Exception:
        logger.debug("herdr report_state failed for run %s (%s)", run_id, status, exc_info=True)


def _high_risk_files_for(store: RunStore, run_id: str) -> list[str]:
    """Reads checks.json's scope.high_risk_files (written by
    checks.py::run_scope_guard from ScopeConfig.high_risk_paths matches).
    Empty list when checks.json is absent/malformed/has no such key — no
    behavior change for a project that never configured high_risk_paths."""
    checks = store.read_result(run_id, "checks.json") or {}
    if not isinstance(checks, dict):
        return []
    scope = checks.get("scope") or {}
    if not isinstance(scope, dict):
        return []
    files = scope.get("high_risk_files") or []
    return [f for f in files if isinstance(f, str)]


_FALLBACK_TEMPLATES = {
    "spec": (
        "# Independent review — Spec Conformance axis\n\nInvestigate the run's "
        "artifacts and this worktree's actual diff for spec/acceptance-criteria "
        "conformance (see instructions below). Reply with exactly one of APPROVE, "
        "REQUEST_CHANGES, or ESCALATE, a Verification Story section, a fenced "
        "```json findings block ({\"findings\": [...]}, each with severity/action/"
        "location/description/recommendation — action defaults to ask-user if "
        "unclear), followed by your reasoning.\n"
    ),
    "standards": (
        "# Independent review — Standards/Quality axis\n\nInvestigate this "
        "worktree's actual diff for code-quality and this repo's own documented "
        "conventions (see instructions below). Reply with exactly one of APPROVE, "
        "REQUEST_CHANGES, or ESCALATE, a Verification Story section, a fenced "
        "```json findings block ({\"findings\": [...]}, each with severity/action/"
        "location/description/recommendation — action defaults to ask-user if "
        "unclear), followed by your reasoning.\n"
    ),
}


def _load_axis_template(prompts_dir: Path, axis: str) -> str:
    """Per-axis template lookup with graceful fallback: prefer
    review-<axis>.md; if a project's .gantry/prompts/ only has the OLD single
    review.md (not yet migrated to per-axis templates), reuse that for both
    axes rather than falling all the way back to the generic built-in text;
    only fall back to the built-in fallback text when neither file exists."""
    axis_path = prompts_dir / f"review-{axis}.md"
    if axis_path.exists():
        return axis_path.read_text()
    legacy_path = prompts_dir / "review.md"
    if legacy_path.exists():
        return legacy_path.read_text()
    return _FALLBACK_TEMPLATES[axis]


def _resolve_prompts_dir(cfg: GantryConfig, cwd: Path) -> Path:
    prompts_dir = Path(cfg.prompts_dir)
    return prompts_dir if prompts_dir.is_absolute() else (cwd / prompts_dir)


def _run_one_axis(axis: str, store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path,
                  base: str, high_risk_files: list[str]) -> dict[str, Any]:
    """Runs ONE axis (spec|standards) as a fully independent `runner.run()`
    call: its own prompt, its own session (Task 3: "review_spec"/
    "review_standards" — see RunStore.save_session/get_session_id, which
    accept arbitrary string stage-keys already), its own logs. Called via
    ThreadPoolExecutor from `_run_review_two_axis` — mirrors the exact
    parallel-command pattern checks.py uses for parallel=true check commands
    (same library, same "submit to a pool, collect .result() per future"
    shape), just applied to two review axes instead of N check commands."""
    session_key = f"review_{axis}"
    prompts_dir = _resolve_prompts_dir(cfg, cwd)
    template = _load_axis_template(prompts_dir, axis)
    checklist_items = cfg.review.checklist if axis == "spec" else cfg.review.standards_checklist

    prompt = _build_axis_prompt(store, run_id, cwd, base, template, cfg, checklist_items, high_risk_files)
    store.write_log(run_id, f"review-{axis}-prompt.md", prompt)

    runner = get_runner(cfg.review.runner)

    from .mcp import ensure_mcp_for_stage
    mcp_results = ensure_mcp_for_stage(cfg, "review", runner.name, cwd)
    if mcp_results:
        store.write_log(run_id, f"review-{axis}-mcp.json", json.dumps(mcp_results, indent=2))

    with _SHARED_STATE_LOCK:
        session_id = store.get_session_id(run_id, session_key)
        store.save_session(run_id, session_key, model=cfg.review.model, runner=runner.name)

    proxy = cfg.proxy.get(runner.name)
    result = runner.run(
        cwd=cwd, prompt=prompt, model=cfg.review.model,
        session_id=session_id, plan_mode=False, skip_permissions=cfg.agent.skip_permissions,
        output_format="json", session_name=f"{run_id}-review-{axis}",
        max_turns=cfg.review.max_turns, timeout=900,
        env=resolve_proxy_env(runner.name, proxy), proxy=proxy,
    )

    secrets = proxy_secrets(cfg)
    store.write_log(run_id, f"review-{axis}.stdout", redact_secrets(result.stdout, extra_secrets=secrets))
    store.write_log(run_id, f"review-{axis}.stderr", redact_secrets(result.stderr, extra_secrets=secrets))
    from .cost import accumulate as _accumulate_cost
    with _SHARED_STATE_LOCK:
        store.save_session(run_id, session_key, session_id=result.session_id,
                           model=cfg.review.model, runner=runner.name)
        _accumulate_cost(store, run_id, session_key, result.usage,
                         runner=runner.name, session_id=result.session_id)

    text = result.raw.get("result", "") if isinstance(result.raw, dict) else result.stdout
    text = str(text) or result.stdout
    derived = _derive_axis_result(text, cfg, result.ok)

    return {
        "axis": axis,
        "verdict": derived["verdict"],
        "findings": derived["findings"],
        "findings_source": derived["findings_source"],
        "verification_story_included": "verification story" in text.lower(),
        "ok": result.ok,
        "model": cfg.review.model,
        "session_id": result.session_id,
        "result": text[:8000],
    }


def _combined_comments_md(spec: dict[str, Any], standards: dict[str, Any]) -> str:
    """review-comments.md body for a two-axis REQUEST_CHANGES verdict —
    surfaces BOTH axes' verdict + findings + prose so a resumed build (or a
    human reading the notification) sees the full picture, not just whichever
    axis happened to be the blocking one."""
    def _axis_section(name: str, axis: dict[str, Any]) -> str:
        lines = [f"## {name} axis — verdict: {axis['verdict']}\n"]
        findings = axis.get("findings") or []
        if findings:
            lines.append("### Findings\n")
            for f in findings:
                lines.append(
                    f"- **[{f['severity']}/{f['action']}]** {f['location']}: "
                    f"{f['description']} — _{f['recommendation']}_\n")
        lines.append(f"\n### Full response\n\n{axis['result']}\n")
        return "".join(lines)

    return (
        "# Review: changes requested (two-axis)\n\n"
        + _axis_section("Spec", spec) + "\n" + _axis_section("Standards", standards)
    )


def _run_review_two_axis(store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path) -> dict[str, Any]:
    """cfg.review.two_axis == True path (the default). See module docstring."""
    base = cfg.git.base_branch

    store.update_state(run_id, status=Status.REVIEW_RUNNING, current_stage="review")
    _report_herdr(cfg, run_id, "review_running")

    high_risk_files = _high_risk_files_for(store, run_id)

    from .engine import _start_heartbeat, _stop_heartbeat
    stop_hb, hb_thread = _start_heartbeat(store, run_id)
    try:
        # ThreadPoolExecutor over the two axes — same concurrency pattern as
        # checks.py::run_repo_checks' parallel=true commands (subprocess.run
        # inside each axis releases the GIL while the agent CLI subprocess is
        # running, so this is real wall-clock parallelism for the ~2x LLM
        # calls two_axis costs, not just concurrency theater).
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(_run_one_axis, "spec", store, run_id, cfg, cwd, base, high_risk_files): "spec",
                pool.submit(_run_one_axis, "standards", store, run_id, cfg, cwd, base, high_risk_files): "standards",
            }
            axis_results = {futures[future]: future.result() for future in futures}
    finally:
        _stop_heartbeat(stop_hb, hb_thread)

    spec = axis_results["spec"]
    standards = axis_results["standards"]
    combined_verdict = _combine_axis_verdicts(spec["verdict"], standards["verdict"])

    out = {
        "two_axis": True,
        # "verdict" kept at top level (mirrors the legacy flat shape's key) so
        # any generic caller reading out["verdict"] (e.g. advance.py's action
        # dicts) doesn't need a two_axis branch just to get the bottom line.
        "verdict": combined_verdict,
        "combined_verdict": combined_verdict,
        "ok": bool(spec["ok"] and standards["ok"]),
        "model": cfg.review.model,
        "high_risk_files": high_risk_files,
        "spec": spec,
        "standards": standards,
    }
    store.write_result(run_id, "review-result.json", out)

    status = {"APPROVE": Status.REVIEW_APPROVED, "REQUEST_CHANGES": Status.REVIEW_CHANGES_REQUESTED,
              "ESCALATE": Status.REVIEW_ESCALATED}[combined_verdict]
    store.update_state(run_id, status=status, review_verdict=combined_verdict)
    _report_herdr(cfg, run_id, status)

    if combined_verdict == "REQUEST_CHANGES":
        store.artifact_path(run_id, "review-comments.md").write_text(
            _combined_comments_md(spec, standards))
    return out


def _run_review_single(store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path) -> dict[str, Any]:
    """cfg.review.two_axis == False path — BYTE-IDENTICAL to review.py's
    behavior before two-axis review existed. Do not modify this function's
    behavior when changing the two-axis path above; it is the opt-out
    regression guard (see tests/test_review.py's two_axis=False tests)."""
    base = cfg.git.base_branch
    prompts_dir = Path(cfg.prompts_dir)
    prompts_dir = prompts_dir if prompts_dir.is_absolute() else (cwd / prompts_dir)
    tmpl_path = prompts_dir / "review.md"
    template = tmpl_path.read_text() if tmpl_path.exists() else (
        "# Independent review\n\nInvestigate the run's artifacts and this worktree's actual "
        "diff (see instructions below). Reply with exactly one of APPROVE, REQUEST_CHANGES, "
        "or ESCALATE, followed by your reasoning.\n")

    prompt = _build_prompt(store, run_id, cwd, base, template, cfg)
    store.write_log(run_id, "review-prompt.md", prompt)

    # Unlike engine.run_agent_stage's plan/build/evidence stages, this used
    # to only report "review_running" to herdr's sidebar without ever
    # writing it to state.json — `gantry status`/`gantry watch` kept
    # showing the run's prior status (e.g. "evidence_complete") for the
    # entire review duration, then jumped straight to the final verdict
    # with no visible "in progress" state at all.
    store.update_state(run_id, status=Status.REVIEW_RUNNING, current_stage="review")
    _report_herdr(cfg, run_id, "review_running")
    runner = get_runner(cfg.review.runner)

    # Register any enabled MCP servers for review (e.g. codebase-memory), same
    # as engine.run_agent_stage does for plan/build/evidence.
    from .mcp import ensure_mcp_for_stage
    mcp_results = ensure_mcp_for_stage(cfg, "review", runner.name, cwd)
    if mcp_results:
        store.write_log(run_id, "review-mcp.json", json.dumps(mcp_results, indent=2))

    session_id = store.get_session_id(run_id, "review")
    # Save runner/model BEFORE invoking (mirrors engine.run_agent_stage) so
    # `gantry watch`'s AGENT/MODEL columns aren't blank for the whole duration
    # of review_running — session_id isn't known until the agent returns, but
    # which runner/model is driving it right now is, and that's what the
    # live-status columns are for.
    store.save_session(run_id, "review", model=cfg.review.model, runner=runner.name)
    # This is now an agentic investigation (the reviewer reads files, runs git
    # diff, re-checks tests itself), so it needs the same headless auto-approve
    # the other stages get, and more turns than a single-shot prompt needed.
    from .engine import _start_heartbeat, _stop_heartbeat
    stop_hb, hb_thread = _start_heartbeat(store, run_id)
    try:
        proxy = cfg.proxy.get(runner.name)
        result = runner.run(
            cwd=cwd, prompt=prompt, model=cfg.review.model,
            session_id=session_id, plan_mode=False, skip_permissions=cfg.agent.skip_permissions,
            output_format="json", session_name=f"{run_id}-review", max_turns=cfg.review.max_turns, timeout=900,
            env=resolve_proxy_env(runner.name, proxy), proxy=proxy,
        )
    finally:
        _stop_heartbeat(stop_hb, hb_thread)
    # Unlike run_agent_stage, this never logged raw stdout/stderr — a
    # failed review (result.ok=False, empty result text) left literally no
    # diagnostic trail beyond review-result.json's bare {"ok": false,
    # "verdict": "ESCALATE"}, no way to tell WHY the runner call failed.
    # Redact known-sensitive values (GH_TOKEN/TFY_API_KEY, this config's proxy
    # api_key_env/headers values) before persisting subprocess output to disk
    # — see redact.py's module docstring for the leak vector this closes.
    secrets = proxy_secrets(cfg)
    store.write_log(run_id, "review.stdout", redact_secrets(result.stdout, extra_secrets=secrets))
    store.write_log(run_id, "review.stderr", redact_secrets(result.stderr, extra_secrets=secrets))
    store.save_session(run_id, "review", session_id=result.session_id,
                       model=cfg.review.model, runner=runner.name)
    from .cost import accumulate as _accumulate_cost
    _accumulate_cost(store, run_id, "review", result.usage,
                     runner=runner.name, session_id=result.session_id)

    text = result.raw.get("result", "") if isinstance(result.raw, dict) else result.stdout
    verdict = _parse_verdict(str(text) or result.stdout, cfg) if result.ok else "ESCALATE"

    out = {"verdict": verdict, "ok": result.ok, "model": cfg.review.model,
           "session_id": result.session_id, "result": str(text)[:8000]}
    store.write_result(run_id, "review-result.json", out)

    status = {"APPROVE": Status.REVIEW_APPROVED, "REQUEST_CHANGES": Status.REVIEW_CHANGES_REQUESTED,
              "ESCALATE": Status.REVIEW_ESCALATED}[verdict]
    store.update_state(run_id, status=status, review_verdict=verdict)
    _report_herdr(cfg, run_id, status)
    if verdict == "REQUEST_CHANGES":
        store.artifact_path(run_id, "review-comments.md").write_text(
            f"# Review: changes requested\n\n{text}\n")
    return out


def run_review(store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path) -> dict[str, Any]:
    """Entry point unchanged for every existing caller (advance.py, cmd_review).
    Dispatches on cfg.review.two_axis: True (default) runs both Spec and
    Standards axes in parallel and combines their verdicts (see
    `_run_review_two_axis`); False restores the exact legacy single-verdict,
    single-session behavior (see `_run_review_single`) for projects that want
    to opt out of the ~2x LLM call cost."""
    if cfg.review.two_axis:
        return _run_review_two_axis(store, run_id, cfg, cwd)
    return _run_review_single(store, run_id, cfg, cwd)
