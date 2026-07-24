"""Gantry configuration: load, validate, and provide defaults for gantry.toml.

gantry.toml lives in the *target repo* and declares how Gantry should operate on
that project: which agent runner to use, per-stage models, which stages run,
scope guards, and the repo's own check commands.

Nothing in Gantry's engine hardcodes a project, model, or tool — it all comes
from here (or the documented defaults below).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for <3.11
    import tomli as tomllib  # type: ignore

CONFIG_FILENAME = "gantry.toml"

# The ordered pipeline used when no gantry.toml [stages] is set (or no config
# file exists at all — see load_config's bare-repo fallback). spec/design are
# NOT included by default: adding them to an existing project's pipeline is a
# behavior change (a fresh run now stops at awaiting_spec first), so it's
# opt-in via gantry.toml's [stages] list, not silently applied to configs that
# predate the spec/design execution path (see templates/prompts/spec.md and
# design.md, and templates/gantry.toml's [stages] comment for how to opt in).
DEFAULT_STAGES = ["plan", "build", "evidence", "review"]

DOC_STAGES = {"spec", "design", "investigation", "research"}  # human-authored/agent-drafted, human-gated
AGENT_STAGES = {"plan", "build", "evidence"}  # invoke the agent runner
REVIEW_STAGE = "review"                    # independent LLM review

STAGE_ARTIFACTS = {
    "spec": "product-spec.md",
    "design": "architecture-design.md",
    "investigation": "investigation-report.md",
    "research": "research-report.md",
    "plan": "implementation-plan.md",
    "build": "build-summary.md",
    "evidence": "evidence-report.md",
    "review": "review-result.json",
}

# The 5 fixed, generic queues gantry ships with (see gantry/linear.py's
# QUEUE_TAGS) — every project gets these stage lists for free with no
# gantry.toml [queues.*] config required. A project's own [queues.<tag>]
# section overrides just that tag, same merge-on-top pattern as
# DEFAULT_MCP_SERVERS below (see load_config).
DEFAULT_QUEUE_STAGES: dict[str, list[str]] = {
    "feature": ["spec", "design", "plan", "build", "evidence", "review"],
    "bug": ["investigation", "plan", "build", "evidence", "review"],
    "hotfix": ["build", "evidence"],  # no review — ships direct to staging, tested there
    "research": ["research"],
    "chore": ["plan", "build", "evidence", "review"],
}


@dataclass
class AgentConfig:
    """Which agent backend drives the plan/build/evidence stages."""
    runner: str = "cursor-sdk"        # SDK default; legacy CLI runner names remain valid
    skip_permissions: bool = True       # pass the runner's auto-approve flag
    output_format: str = "json"
    max_concurrent: int = 0    # cap on concurrently-running agent subprocesses
                                 # across advance_all's per-tick sweep. 0 = unlimited
                                 # (today's behavior: runs are processed one at a
                                 # time in a single Python process anyway, so an
                                 # unbounded cap changes nothing until advance_all
                                 # actually parallelizes its per-run loop).


# Per-runner install commands for agent skill libraries (e.g. superpowers).
# Clean, inspectable commands only — never a piped remote shell script.
# doctor verifies presence; `init --with-skills` runs the command for the active runner.
DEFAULT_SKILL_INSTALLERS = {
    "superpowers": {
        "claude-code": "claude plugin install superpowers@claude-plugins-official",
        "cursor-cli": "npx skills add obra/superpowers -a cursor -y",
        # skills CLI agent id is "codex" (not "codex-cli"); -g installs into
        # ~/.codex/skills so headless `codex exec` sessions pick them up.
        "codex-cli": "npx skills add obra/superpowers -a codex -g -y",
    },
}


@dataclass
class SkillsConfig:
    """Agent skill libraries mandated for the build/evidence stages.

    `enabled` names skills to require; `installers` maps skill -> {runner -> command}.
    Only the active runner's command is ever used. Scoped to build/evidence in the
    prompts so it augments execution discipline without fighting Gantry's own
    spec/design/plan stages.
    """
    enabled: list[str] = field(default_factory=list)
    installers: dict[str, dict[str, str]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_SKILL_INSTALLERS.items()}
    )
    evidence_directive: str = ""  # override text for the evidence stage's mandated-skills
                                    # block. Empty = evidence gets a verification-focused
                                    # default ("confirm the plan was executed correctly; do
                                    # not re-implement") instead of build's TDD/execution-
                                    # focused directive — evidence and build need different
                                    # framing even when they share the same `enabled` skills.

    def install_command(self, skill: str, runner: str) -> str | None:
        return (self.installers.get(skill) or {}).get(runner)


@dataclass
class StageModel:
    model: str
    max_turns: int = 60
    plan_mode: bool = False
    runner: str = ""    # "" = inherit [agent].runner; else "claude-code"|"cursor-cli"|"codex-cli"
    timeout: int = 900  # subprocess wall-clock cap in seconds; raise for slower stages (e.g. evidence)


@dataclass
class ReviewConfig:
    """Independent LLM review after evidence. Separate model family recommended."""
    enabled: bool = True
    runner: str = "cursor-sdk"            # defaults to the local Cursor SDK backend
    model: str = ""                       # e.g. a strong reviewing model
    approve_keywords: list[str] = field(default_factory=lambda: ["APPROVE"])
    request_changes_keywords: list[str] = field(default_factory=lambda: ["REQUEST_CHANGES"])
    escalate_keywords: list[str] = field(default_factory=lambda: ["ESCALATE"])
    max_turns: int = 10   # review is an investigation, not open-ended implementation —
                            # needs far fewer turns than build/evidence. Previously
                            # silently inherited StageModel's generic default (60)
                            # because "review" is rarely declared in [models].
    checklist: list[str] = field(default_factory=list)  # project-specific items the Spec
                                                          # axis must explicitly address
                                                          # (e.g. "confirm no secrets committed").
                                                          # Scoped to Spec axis only (two_axis=True)
                                                          # because spec conformance is where project-
                                                          # specific acceptance rules belong; applies
                                                          # to the single-axis review when two_axis=False.
    standards_checklist: list[str] = field(default_factory=list)  # project-specific items the
                                                                    # Standards axis must explicitly
                                                                    # address (two_axis=True only).
                                                                    # e.g. ["all public functions have
                                                                    # docstrings", "no print() in prod code"]
    keyword_mode: str = "anywhere"  # "anywhere" (default, current behavior) | "line_start" —
                                     # "line_start" requires the verdict keyword
                                     # (APPROVE/REQUEST_CHANGES/ESCALATE) to be the first
                                     # token of a line, eliminating false ESCALATE matches
                                     # from prose that merely mentions the word.
    two_axis: bool = True  # When True (default), runs two independent review axes in parallel:
                            #   - Axis A (Spec): does the diff satisfy every acceptance criterion,
                            #     match the architecture, stay within scope, do evidence claims hold?
                            #   - Axis B (Standards): does the diff follow this repo's documented
                            #     conventions and language-agnostic code-quality baselines?
                            # Each axis gets its own dedicated LLM session (separate session keys
                            # "review_spec"/"review_standards" in sessions.json — never shared).
                            # Combined verdict: both APPROVE required for review_approved; either
                            # REQUEST_CHANGES → review_changes_requested (both axes' findings
                            # surfaced); either ESCALATE → review_escalated (escalation wins).
                            # Cost/latency tradeoff: roughly 2x LLM calls compared to single-axis.
                            # Set two_axis = false to restore the exact legacy single-verdict/
                            # single-session behavior byte-for-byte (the opt-out path for latency-
                            # sensitive or cost-sensitive projects).


@dataclass
class ScopeConfig:
    """Gantry's built-in deterministic guard. Not the repo's linters."""
    forbid_paths: list[str] = field(default_factory=lambda: [".env", "**/*.pem", "**/secrets/**"])
    enforce_plan_scope: bool = True       # flag files changed outside the plan's stated scope
    mode: str = "block"    # "block" | "warn" | "off" — how enforce_plan_scope's
                            # violations are treated. "block" (default) preserves
                            # today's behavior: an unexpected file fails checks.
                            # "warn" surfaces unexpected files in scope.json's
                            # `warnings` but still passes — for a build that
                            # legitimately discovers it needs a file the plan
                            # never mentioned and didn't declare via the
                            # build-summary.md "## Scope additions" section (see
                            # checks._allowed_paths). "off" disables the plan-scope
                            # check entirely (forbid_paths still always applies).
    require_declared_additions: bool = True  # in "block"/"warn" mode, a new file
                                              # is only added to the allowlist
                                              # (no warning/failure at all) if the
                                              # build stage declared it under a
                                              # "## Scope additions" section in
                                              # build-summary.md. False allows any
                                              # new file through with a warning
                                              # (in "warn" mode) with no
                                              # declaration required.
    high_risk_paths: list[str] = field(default_factory=list)  # project-configurable
                                              # glob list (same matching semantics as
                                              # forbid_paths, via checks._matches_any)
                                              # of paths sensitive enough that touching
                                              # them should always force a human-gated
                                              # status (checks_high_risk_escalated),
                                              # regardless of auto_approve_docs/
                                              # auto_ship/auto_resolve. Empty by
                                              # default — no project-agnostic guessing
                                              # at what's "sensitive" for an arbitrary
                                              # repo; each project opts in via its own
                                              # gantry.toml, e.g.
                                              # ["**/auth/**", "**/migrations/**"].


@dataclass
class CheckCommand:
    """One repo check command, with its own optional timeout/parallel override.
    `commands` accepts a bare string (wrapped into this with defaults) or a
    table `{command, timeout, parallel}` — see _coerce_check_command."""
    command: str
    timeout: int | None = None   # None = fall back to ChecksConfig.timeout
    parallel: bool = False        # run concurrently with other parallel=true commands


def _coerce_check_command(item: Any) -> CheckCommand:
    if isinstance(item, CheckCommand):
        return item
    if isinstance(item, str):
        return CheckCommand(command=item)
    return CheckCommand(command=item["command"], timeout=item.get("timeout"),
                        parallel=bool(item.get("parallel", False)))


@dataclass
class ChecksConfig:
    """Delegate house rules to the repo's own toolchain. Gantry runs these and
    gates on exit code. Works on any repo/language."""
    # Each entry is a bare string (simple case) or a CheckCommand/table (own
    # timeout/parallel). run_repo_checks coerces every entry via
    # _coerce_check_command, so direct dataclass construction with plain
    # strings (existing behavior, existing tests) keeps working unchanged.
    commands: list[Any] = field(default_factory=list)  # e.g. ["npm run lint", "npm run build"]
    timeout: int = 900
    max_parallel: int = 4  # cap on concurrently-running parallel=true commands
    retry_checks: int = 3  # auto-resume build with failure feedback this many times
                           # before escalating to a human (checks_escalated)
    auto_resolve: bool = False   # if True, checks_escalated spawns a dedicated
                                  # resolver agent instead of dead-ending at a
                                  # human. Opt-in, same reasoning as auto_ship/
                                  # auto_merge: appropriate for a solo/local
                                  # project willing to trade a human review gate
                                  # for full unattended operation, not a sane
                                  # default for team repos.
    resolve_attempts: int = 2   # cap on resolver-agent attempts before giving
                                 # up for real (resolve_escalated) — a genuine
                                 # backstop so a broken resolver can't loop
                                 # forever either, same shape as retry_checks.
    flaky_retry_attempts: int = 0  # 0 = disabled (today's exact behavior: a
                                 # failing check command fails immediately,
                                 # zero retry attempts). When >0, a failing
                                 # command is re-run bare (no code changes, no
                                 # agent call) up to this many additional
                                 # times BEFORE falling into the existing
                                 # agent-involved retry-with-feedback loop
                                 # (advance.py's `blocked` handling). If any
                                 # re-run passes, the check is treated as
                                 # flaky rather than a real failure — recorded
                                 # in the target repo's flake log, not
                                 # escalated to the agent. Opt-in, same
                                 # reasoning as auto_resolve: not every check
                                 # command is safe to blindly re-run (side
                                 # effects, non-idempotent commands), so a
                                 # project must deliberately turn this on.


@dataclass
class E2eAppConfig:
    """One app's e2e config. `apps` accepts a bare string (wrapped into this
    with defaults) or a table `{command, spec_glob, retry}` — see
    _coerce_e2e_app."""
    command: str
    spec_glob: str = ""   # "" = fall back to E2eConfig.spec_glob
    retry: int = 0         # retry a failing app's e2e run this many times before
                            # including it in the failure report — scoped per-app
                            # since e2e flakiness is usually app-specific, unlike
                            # checks.retry_checks which retries the whole build.


def _coerce_e2e_app(item: Any) -> E2eAppConfig:
    if isinstance(item, E2eAppConfig):
        return item
    if isinstance(item, str):
        return E2eAppConfig(command=item)
    return E2eAppConfig(command=item["command"], spec_glob=item.get("spec_glob", ""),
                        retry=int(item.get("retry", 0)))


@dataclass
class E2eConfig:
    """Deterministic, non-LLM e2e test step run between checks and evidence.

    Runs each touched app's e2e command directly (no agent involved) and writes
    a JSON report the evidence-stage prompt reads instead of re-running the
    suite itself — decouples slow, restart-safe test execution from the
    expensive, hard-to-resume LLM evidence turn. Empty `apps` = step is a no-op
    (evidence stage falls back to running e2e itself, old behavior)."""
    enabled: bool = False
    # app dir name (under apps/<name>) -> bare command string or
    # {command, spec_glob, retry} table. run_e2e_tests coerces every value via
    # _coerce_e2e_app, so direct dataclass construction with plain strings
    # (existing behavior, existing tests) keeps working unchanged.
    apps: dict[str, Any] = field(default_factory=dict)
    # glob (relative to the app dir) used to detect whether this run touched
    # that app's e2e-relevant surface at all — skip apps with no matching spec
    spec_glob: str = "tests/e2e/*.spec.ts"
    timeout: int = 1800


@dataclass
class PlanConfig:
    """Context injection + depth for the plan stage's rendered prompt."""
    include_git_log: bool = False   # prepend the last N `git log --oneline` lines
                                      # as a "## Recent history" section
    git_log_lines: int = 20
    context_files: list[str] = field(default_factory=list)  # paths (relative to the
                                                               # target repo) whose
                                                               # contents get prepended
                                                               # as a "## Referenced files"
                                                               # section
    depth: str = "detailed"   # "brief" | "detailed" — selects prompts/plan-brief.md
                                # instead of prompts/plan.md when set to "brief" and
                                # that template exists; falls back to the single
                                # existing template otherwise (no behavior change for
                                # a project that never adds a brief variant).


@dataclass
class BuildConfig:
    """Pre-build setup hook, run once in the worktree before the build stage's
    first (non-resumed) agent invocation for a run."""
    pre_hook: str = ""   # shell command, e.g. "npm ci && make seed-db". Empty = no-op.
    pre_hook_required: bool = False  # False (default): a failing pre_hook is logged
                                       # but does not block build from starting, mirroring
                                       # git._install_deps_if_npm_project's best-effort
                                       # philosophy (a missing/failed setup step surfaces
                                       # clearly later in whichever check actually needed
                                       # it, rather than hard-failing upfront on something
                                       # that might not even matter for this run).
                                       # True: a failing pre_hook fails the build stage
                                       # immediately instead of proceeding.


@dataclass
class EvidenceConfig:
    """Evidence stage output shape."""
    output_format: str = "prose"   # "prose" (default, current behavior) | "structured" —
                                     # "structured" asks the evidence prompt for a trailing
                                     # fenced JSON block (pass_count, fail_count,
                                     # coverage_pct, scope_summary) that review.py can parse
                                     # deterministically instead of re-deriving it from prose.


@dataclass
class GitConfig:
    base_branch: str = "main"             # diff base for scope/review (was origin/staging)
    auto_ship: bool = False                # if True, advance_run ships automatically
                                            # on review_approved — no human `gantry ship`
                                            # call required. Opt-in: a failed/misjudged
                                            # ship opens a real PR with zero human review.
    auto_merge: bool = False               # if True (and auto_ship is True), also squash-merge
                                            # + delete-branch the PR ship_run just opened —
                                            # for solo/local projects with no external review
                                            # gate, where the independent LLM review stage is
                                            # the approval step. Has no effect if auto_ship is
                                            # False (there's no PR yet to merge).
    auto_approve_docs: bool = False        # if True, advance_run auto-approves spec_complete
                                            # and design_complete itself (same effect as a human
                                            # calling `gantry approve --stage spec/design`) — for
                                            # a fully hands-off spec-to-PR run with no doc-gate
                                            # pause. Opt-in, same reasoning as auto_ship: the
                                            # agent's own spec/design output ships without a
                                            # human ever reading it first.
    ship_retry_attempts: int = 2           # cap on ship_failed auto-retry attempts (advance.py's
                                            # review_approved+auto_ship retry loop). Previously
                                            # borrowed cfg.checks.resolve_attempts as its cap —
                                            # this is its own dedicated field now; default 2
                                            # matches that borrowed value exactly, so a project
                                            # that never sets this explicitly sees zero behavior
                                            # change.


@dataclass
class NotifyConfig:
    backend: str = "none"                 # "none" | "telegram" | "webhook"
    # telegram: reads GANTRY_TELEGRAM_BOT_TOKEN / GANTRY_TELEGRAM_CHAT_ID from env
    # webhook: posts JSON to this url
    webhook_url: str = ""


@dataclass
class DaemonConfig:
    """The background auto-advance job's own per-target guard (see
    gantry/daemon.py). Distinct from any per-stage timeout in [models.*] —
    this bounds how long the daemon tick spends on ONE target before moving
    on to the next, so a hung `load_config`/subprocess check on one repo
    can't silently eat the whole tick's time budget for every other
    registered target. Default (45s) sits comfortably under the 60s default
    daemon interval (see `install_daemon`) so a single slow target still
    leaves room for the rest before the next tick fires."""
    per_target_timeout_seconds: int = 45


@dataclass
class HerdrConfig:
    """Optional herdr (terminal multiplexer) integration. When enabled and Gantry
    runs inside a herdr pane (HERDR_ENV=1), report semantic pipeline state to the
    sidebar and use event-driven waits. Fully opt-in; no-op when herdr absent."""
    enabled: bool = True                  # cheap: only acts when HERDR_ENV=1 anyway
    report_state: bool = True             # push stage state to the herdr sidebar


@dataclass
class MCPServer:
    """An MCP server Gantry attaches to the agent runner.

    `command`/`args` follow the standard MCP client config. `stages` limits which
    stages this server is registered for (empty = all agent stages). `register`
    maps runner -> the CLI command that registers it (Gantry runs it before the
    stage if not already present); if absent, Gantry falls back to writing the
    standard mcpServers JSON where the runner expects it.
    """
    command: str = ""
    args: list[str] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    register: dict[str, str] = field(default_factory=dict)


# Curated defaults: the two vetted servers, with per-runner register commands.
# codex-cli entries are explicit (same shape as claude-code) so doctor / init
# surfaces them; mcp.py's _register_codex also falls back to
# `codex mcp add <name> -- <command> <args...>` when register is empty.
DEFAULT_MCP_SERVERS = {
    "codebase-memory": {
        "command": "codebase-memory-mcp",
        "args": ["serve"],
        "stages": ["plan", "build", "evidence", "review"],
        "register": {
            "claude-code": "claude mcp add codebase-memory --scope user codebase-memory-mcp serve",
            "cursor-cli": "",  # cursor reads project .cursor/mcp.json; init writes it
            "codex-cli": "codex mcp add codebase-memory -- codebase-memory-mcp serve",
        },
    },
    "chrome-devtools": {
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@latest"],
        "stages": ["evidence"],
        "register": {
            "claude-code": "claude mcp add chrome-devtools --scope user npx chrome-devtools-mcp@latest",
            "cursor-cli": "",
            "codex-cli": "codex mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest",
        },
    },
}


@dataclass
class MCPConfig:
    """MCP servers to make available to the agent runner, per stage.
    `enabled` names which servers to activate; `servers` holds their configs
    (curated defaults for codebase-memory / chrome-devtools are built in)."""
    enabled: list[str] = field(default_factory=list)
    servers: dict[str, MCPServer] = field(default_factory=dict)

    def for_stage(self, stage: str) -> dict[str, MCPServer]:
        out = {}
        for name in self.enabled:
            srv = self.servers.get(name)
            if srv and (not srv.stages or stage in srv.stages):
                out[name] = srv
        return out


@dataclass
class ProxyConfig:
    """Per-runner proxy/gateway override — an org-internal LLM gateway sitting
    in front of the vendor API, keyed by runner name under `[proxy.<runner>]`
    (e.g. `[proxy.claude-code]`, `[proxy.codex-cli]`). Independent of and
    additive to docker.py's container env pass-through
    (_pass_env_args / GANTRY_DOCKER_PASS_ENV) — this works bare-metal too,
    not just inside Docker.

    Only claude-code and codex-cli are supported (cursor-cli has no verified
    base-url/headers override mechanism); a `[proxy.cursor-cli]` table is
    ignored with a logged warning. `headers` has no verified passthrough for
    claude-code (the CLI exposes no arbitrary-header mechanism) — configuring
    it there logs a one-line warning instead of silently dropping or crashing.
    """
    base_url: str = ""
    api_key_env: str = ""   # name of the env var (NOT the literal secret) holding the API key/token
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class GantryConfig:
    project_id: str = "project"
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    agent: AgentConfig = field(default_factory=AgentConfig)
    models: dict[str, StageModel] = field(default_factory=dict)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    e2e: E2eConfig = field(default_factory=E2eConfig)
    plan: PlanConfig = field(default_factory=PlanConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    git: GitConfig = field(default_factory=GitConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    herdr: HerdrConfig = field(default_factory=HerdrConfig)
    proxy: dict[str, ProxyConfig] = field(default_factory=dict)  # runner name -> ProxyConfig
    # Specialist role -> additive AgentProfile overrides. Kept as plain
    # mappings here so config.py remains the TOML boundary; profiles.py
    # compiles these on top of legacy [agent]/[models]/[review]/[skills]/[mcp].
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    # prompts dir: where stage prompt templates live (relative to config, or absolute)
    prompts_dir: str = ".gantry/prompts"
    # tag -> stage list, seeded with the 5 built-in queues (DEFAULT_QUEUE_STAGES)
    # so a bare GantryConfig() carries them with no toml at all; a project's
    # [queues.<tag>] in gantry.toml overrides just that tag (see load_config).
    queues: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_QUEUE_STAGES))

    def stages_for(self, tag: str | None) -> list[str]:
        if tag and tag in self.queues:
            return self.queues[tag]
        return self.stages

    def model_for(self, stage: str) -> StageModel:
        if stage in self.models:
            return self.models[stage]
        # sensible default so a bare config still runs
        return StageModel(model="", max_turns=60, plan_mode=(stage == "plan"))

    def runner_for(self, stage: str) -> str:
        """Resolve which agent runner drives this stage: a per-stage override
        in [models.<stage>].runner, falling back to [agent].runner."""
        return self.model_for(stage).runner or self.agent.runner

    def profile_for(self, stage: str):
        """Resolve the immutable specialist profile for an invocation stage."""
        from .profiles import profile_for_stage
        return profile_for_stage(stage, self)

    def artifact_for(self, stage: str) -> str:
        return STAGE_ARTIFACTS.get(stage, f"{stage}.md")


def _coerce_models(raw: dict[str, Any]) -> dict[str, StageModel]:
    out: dict[str, StageModel] = {}
    for stage, spec in (raw or {}).items():
        if isinstance(spec, str):
            out[stage] = StageModel(model=spec, plan_mode=(stage == "plan"))
        elif isinstance(spec, dict):
            out[stage] = StageModel(
                model=spec.get("model", ""),
                max_turns=int(spec.get("max_turns", 60)),
                plan_mode=bool(spec.get("plan_mode", stage == "plan")),
                runner=spec.get("runner", ""),
                timeout=int(spec.get("timeout", 900)),
            )
    return out


def _coerce_proxy(raw: dict[str, Any]) -> dict[str, ProxyConfig]:
    """Mirrors _coerce_models's shape: [proxy.<runner>] tables keyed by
    runner name. cursor-cli has no verified proxy mechanism — a
    [proxy.cursor-cli] table is ignored with a logged warning rather than
    silently accepted and silently unused."""
    out: dict[str, ProxyConfig] = {}
    for runner, spec in (raw or {}).items():
        if runner == "cursor-cli":
            logger.warning("[proxy.cursor-cli] is configured but proxy overrides are not "
                           "supported for cursor-cli — ignoring this section.")
            continue
        if not isinstance(spec, dict):
            continue
        out[runner] = ProxyConfig(
            base_url=spec.get("base_url", ""),
            api_key_env=spec.get("api_key_env", ""),
            headers=dict(spec.get("headers", {})),
        )
    return out


def load_config(target_workspace: Path) -> GantryConfig:
    """Load gantry.toml from the target workspace. Missing file -> all defaults
    (so Gantry still runs on a fresh repo, just with empty models/checks).

    SECURITY INVARIANT: every call site in this codebase passes the TARGET
    repo's own root (self.target in Engine, `_target()`/GANTRY_TARGET in the
    CLI) — never a run's worktree path (see git.ensure_worktree/Engine.work_dir).
    This matters because [checks].commands and [agent]/[models.*].runner are
    code-executing fields: checks.py's run_repo_checks shells them out with
    gantry's own ambient privileges (GH_TOKEN, proxy secrets, etc). If an
    agent-produced branch (plan/build stage) could edit gantry.toml INSIDE its
    own worktree and have that edit picked up, a later `gantry checks --run ID`
    would execute whatever commands that branch just wrote. Engine loads its
    config exactly once, from the target repo, at construction time
    (`Engine.__init__` -> `self.cfg`) and reuses that same GantryConfig for
    every stage/check/review call on a run — a worktree-local gantry.toml
    mutation is therefore inert by construction; it's never re-read. Do not
    add a call site that resolves gantry.toml from `Engine.work_dir(run_id)`
    or any other worktree path — that would reopen exactly this hole.
    """
    path = target_workspace / CONFIG_FILENAME
    if not path.exists():
        return GantryConfig()
    raw = tomllib.loads(path.read_text())

    cfg = GantryConfig()
    cfg.project_id = raw.get("project_id", cfg.project_id)
    cfg.stages = raw.get("stages", cfg.stages)
    cfg.prompts_dir = raw.get("prompts_dir", cfg.prompts_dir)
    cfg.queues.update({tag: q["stages"] for tag, q in raw.get("queues", {}).items() if "stages" in q})

    if "agent" in raw:
        a = raw["agent"]
        cfg.agent = AgentConfig(
            runner=a.get("runner", "cursor-sdk"),
            skip_permissions=bool(a.get("skip_permissions", True)),
            output_format=a.get("output_format", "json"),
            max_concurrent=int(a.get("max_concurrent", 0)),
        )
    cfg.models = _coerce_models(raw.get("models", {}))
    cfg.review.runner = cfg.agent.runner

    if "review" in raw:
        r = raw["review"]
        cfg.review = ReviewConfig(
            enabled=bool(r.get("enabled", True)),
            runner=r.get("runner", cfg.agent.runner),
            model=r.get("model", ""),
            approve_keywords=r.get("approve_keywords", ["APPROVE"]),
            request_changes_keywords=r.get("request_changes_keywords", ["REQUEST_CHANGES"]),
            escalate_keywords=r.get("escalate_keywords", ["ESCALATE"]),
            max_turns=int(r.get("max_turns", 10)),
            checklist=r.get("checklist", []),
            standards_checklist=r.get("standards_checklist", []),
            keyword_mode=r.get("keyword_mode", "anywhere"),
            two_axis=bool(r.get("two_axis", True)),
        )
    if "scope" in raw:
        s = raw["scope"]
        enforce_plan_scope = bool(s.get("enforce_plan_scope", True))
        # "mode" is the current knob; enforce_plan_scope=False is a deprecated
        # alias for mode="off", kept so existing gantry.toml files that only
        # set the old bool still behave identically. An explicit "mode" always
        # wins over the deprecated bool.
        default_mode = "off" if not enforce_plan_scope else "block"
        cfg.scope = ScopeConfig(
            forbid_paths=s.get("forbid_paths", ScopeConfig().forbid_paths),
            enforce_plan_scope=enforce_plan_scope,
            mode=s.get("mode", default_mode),
            require_declared_additions=bool(s.get("require_declared_additions", True)),
            high_risk_paths=s.get("high_risk_paths", ScopeConfig().high_risk_paths),
        )
    if "checks" in raw:
        c = raw["checks"]
        # commands entries are each either a bare string or a
        # {command, timeout, parallel} table — both are valid TOML array
        # elements; _coerce_check_command (checks.py's run_repo_checks call
        # site) normalizes either shape, so no coercion needed here.
        cfg.checks = ChecksConfig(commands=c.get("commands", []), timeout=int(c.get("timeout", 900)),
                                  max_parallel=int(c.get("max_parallel", 4)),
                                  retry_checks=int(c.get("retry_checks", 3)),
                                  auto_resolve=bool(c.get("auto_resolve", False)),
                                  resolve_attempts=int(c.get("resolve_attempts", 2)),
                                  flaky_retry_attempts=int(c.get("flaky_retry_attempts", 0)))
    if "e2e" in raw:
        e = raw["e2e"]
        # apps values are each either a bare string or a
        # {command, spec_glob, retry} table — both valid TOML; run_e2e_tests'
        # _coerce_e2e_app normalizes either shape, so no coercion needed here.
        cfg.e2e = E2eConfig(
            enabled=bool(e.get("enabled", False)),
            apps=dict(e.get("apps", {})),
            spec_glob=e.get("spec_glob", E2eConfig().spec_glob),
            timeout=int(e.get("timeout", 1800)),
        )
    if "plan" in raw:
        p = raw["plan"]
        cfg.plan = PlanConfig(
            include_git_log=bool(p.get("include_git_log", False)),
            git_log_lines=int(p.get("git_log_lines", 20)),
            context_files=p.get("context_files", []),
            depth=p.get("depth", "detailed"),
        )
    if "build" in raw:
        b = raw["build"]
        cfg.build = BuildConfig(
            pre_hook=b.get("pre_hook", ""),
            pre_hook_required=bool(b.get("pre_hook_required", False)),
        )
    if "evidence" in raw:
        ev = raw["evidence"]
        cfg.evidence = EvidenceConfig(output_format=ev.get("output_format", "prose"))
    if "git" in raw:
        g = raw["git"]
        cfg.git = GitConfig(base_branch=g.get("base_branch", "main"),
                            auto_ship=bool(g.get("auto_ship", False)),
                            auto_merge=bool(g.get("auto_merge", False)),
                            auto_approve_docs=bool(g.get("auto_approve_docs", False)),
                            ship_retry_attempts=int(g.get("ship_retry_attempts", 2)))
    if "notify" in raw:
        n = raw["notify"]
        cfg.notify = NotifyConfig(backend=n.get("backend", "none"), webhook_url=n.get("webhook_url", ""))
    if "skills" in raw:
        sk = raw["skills"]
        installers = {k: dict(v) for k, v in DEFAULT_SKILL_INSTALLERS.items()}
        installers.update(sk.get("installers", {}))
        cfg.skills = SkillsConfig(enabled=sk.get("enabled", []), installers=installers,
                                  evidence_directive=sk.get("evidence_directive", ""))
    # MCP: merge curated defaults with any user-declared servers.
    servers = {name: MCPServer(command=s["command"], args=s.get("args", []),
                               stages=s.get("stages", []), register=dict(s.get("register", {})))
               for name, s in DEFAULT_MCP_SERVERS.items()}
    if "mcp" in raw:
        m = raw["mcp"]
        for name, s in (m.get("servers", {}) or {}).items():
            servers[name] = MCPServer(command=s.get("command", ""), args=s.get("args", []),
                                      stages=s.get("stages", []), register=dict(s.get("register", {})))
        cfg.mcp = MCPConfig(enabled=m.get("enabled", []), servers=servers)
    else:
        cfg.mcp = MCPConfig(enabled=[], servers=servers)
    if "herdr" in raw:
        h = raw["herdr"]
        cfg.herdr = HerdrConfig(enabled=bool(h.get("enabled", True)),
                                report_state=bool(h.get("report_state", True)))
    if "daemon" in raw:
        d = raw["daemon"]
        cfg.daemon = DaemonConfig(
            per_target_timeout_seconds=int(d.get("per_target_timeout_seconds", 45)))
    cfg.proxy = _coerce_proxy(raw.get("proxy", {}))
    cfg.profiles = {
        role: dict(profile)
        for role, profile in (raw.get("profiles", {}) or {}).items()
        if isinstance(profile, dict)
    }
    return cfg
