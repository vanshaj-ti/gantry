"""Gantry configuration: load, validate, and provide defaults for gantry.toml.

gantry.toml lives in the *target repo* and declares how Gantry should operate on
that project: which agent runner to use, per-stage models, which stages run,
scope guards, and the repo's own check commands.

Nothing in Gantry's engine hardcodes a project, model, or tool — it all comes
from here (or the documented defaults below).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for <3.11
    import tomli as tomllib  # type: ignore

CONFIG_FILENAME = "gantry.toml"

# The ordered pipeline used when no gantry.toml [stages] is set (or no config
# file exists at all — see load_config's bare-repo fallback). spec/design are
# NOT included: they have no CLI execution verb yet (see Engine.create_run's
# guard) and would leave a fresh run stuck at awaiting_spec forever. Add them
# back to this default once `gantry stage spec/design` exists.
DEFAULT_STAGES = ["plan", "build", "evidence", "review"]

DOC_STAGES = {"spec", "design"}          # human-authored/agent-drafted, human-gated
AGENT_STAGES = {"plan", "build", "evidence"}  # invoke the agent runner
REVIEW_STAGE = "review"                    # independent LLM review

STAGE_ARTIFACTS = {
    "spec": "product-spec.md",
    "design": "architecture-design.md",
    "plan": "implementation-plan.md",
    "build": "build-summary.md",
    "evidence": "evidence-report.md",
    "review": "review-result.json",
}


@dataclass
class AgentConfig:
    """Which agent CLI drives the plan/build/evidence stages."""
    runner: str = "claude-code"        # "claude-code" | "cursor-cli"
    skip_permissions: bool = True       # pass the runner's auto-approve flag
    output_format: str = "json"


# Per-runner install commands for agent skill libraries (e.g. superpowers).
# Clean, inspectable commands only — never a piped remote shell script.
# doctor verifies presence; `init --with-skills` runs the command for the active runner.
DEFAULT_SKILL_INSTALLERS = {
    "superpowers": {
        "claude-code": "claude plugin install superpowers@claude-plugins-official",
        "cursor-cli": "npx skills add obra/superpowers -a cursor",
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
    runner: str = "claude-code"          # reviewer can use either runner
    model: str = ""                       # e.g. a strong reviewing model
    approve_keywords: list[str] = field(default_factory=lambda: ["APPROVE"])
    request_changes_keywords: list[str] = field(default_factory=lambda: ["REQUEST_CHANGES"])
    escalate_keywords: list[str] = field(default_factory=lambda: ["ESCALATE"])


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


@dataclass
class ChecksConfig:
    """Delegate house rules to the repo's own toolchain. Gantry runs these and
    gates on exit code. Works on any repo/language."""
    commands: list[str] = field(default_factory=list)  # e.g. ["npm run lint", "npm run build"]
    timeout: int = 900
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


@dataclass
class E2eConfig:
    """Deterministic, non-LLM e2e test step run between checks and evidence.

    Runs each touched app's e2e command directly (no agent involved) and writes
    a JSON report the evidence-stage prompt reads instead of re-running the
    suite itself — decouples slow, restart-safe test execution from the
    expensive, hard-to-resume LLM evidence turn. Empty `apps` = step is a no-op
    (evidence stage falls back to running e2e itself, old behavior)."""
    enabled: bool = False
    # app dir name (under apps/<name>) -> shell command to run its e2e suite
    apps: dict[str, str] = field(default_factory=dict)
    # glob (relative to the app dir) used to detect whether this run touched
    # that app's e2e-relevant surface at all — skip apps with no matching spec
    spec_glob: str = "tests/e2e/*.spec.ts"
    timeout: int = 1800


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


@dataclass
class NotifyConfig:
    backend: str = "none"                 # "none" | "telegram" | "webhook"
    # telegram: reads GANTRY_TELEGRAM_BOT_TOKEN / GANTRY_TELEGRAM_CHAT_ID from env
    # webhook: posts JSON to this url
    webhook_url: str = ""


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
DEFAULT_MCP_SERVERS = {
    "codebase-memory": {
        "command": "codebase-memory-mcp",
        "args": ["serve"],
        "stages": ["plan", "build", "evidence", "review"],
        "register": {
            "claude-code": "claude mcp add codebase-memory --scope user codebase-memory-mcp serve",
            "cursor-cli": "",  # cursor reads project .cursor/mcp.json; init writes it
        },
    },
    "chrome-devtools": {
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@latest"],
        "stages": ["evidence"],
        "register": {
            "claude-code": "claude mcp add chrome-devtools --scope user npx chrome-devtools-mcp@latest",
            "cursor-cli": "",
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
class GantryConfig:
    project_id: str = "project"
    stages: list[str] = field(default_factory=lambda: list(DEFAULT_STAGES))
    agent: AgentConfig = field(default_factory=AgentConfig)
    models: dict[str, StageModel] = field(default_factory=dict)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    checks: ChecksConfig = field(default_factory=ChecksConfig)
    e2e: E2eConfig = field(default_factory=E2eConfig)
    git: GitConfig = field(default_factory=GitConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    herdr: HerdrConfig = field(default_factory=HerdrConfig)
    # prompts dir: where stage prompt templates live (relative to config, or absolute)
    prompts_dir: str = ".gantry/prompts"

    def model_for(self, stage: str) -> StageModel:
        if stage in self.models:
            return self.models[stage]
        # sensible default so a bare config still runs
        return StageModel(model="", max_turns=60, plan_mode=(stage == "plan"))

    def runner_for(self, stage: str) -> str:
        """Resolve which agent runner drives this stage: a per-stage override
        in [models.<stage>].runner, falling back to [agent].runner."""
        return self.model_for(stage).runner or self.agent.runner

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


def load_config(target_workspace: Path) -> GantryConfig:
    """Load gantry.toml from the target workspace. Missing file -> all defaults
    (so Gantry still runs on a fresh repo, just with empty models/checks)."""
    path = target_workspace / CONFIG_FILENAME
    if not path.exists():
        return GantryConfig()
    raw = tomllib.loads(path.read_text())

    cfg = GantryConfig()
    cfg.project_id = raw.get("project_id", cfg.project_id)
    cfg.stages = raw.get("stages", cfg.stages)
    cfg.prompts_dir = raw.get("prompts_dir", cfg.prompts_dir)

    if "agent" in raw:
        a = raw["agent"]
        cfg.agent = AgentConfig(
            runner=a.get("runner", "claude-code"),
            skip_permissions=bool(a.get("skip_permissions", True)),
            output_format=a.get("output_format", "json"),
        )
    cfg.models = _coerce_models(raw.get("models", {}))

    if "review" in raw:
        r = raw["review"]
        cfg.review = ReviewConfig(
            enabled=bool(r.get("enabled", True)),
            runner=r.get("runner", cfg.agent.runner),
            model=r.get("model", ""),
            approve_keywords=r.get("approve_keywords", ["APPROVE"]),
            request_changes_keywords=r.get("request_changes_keywords", ["REQUEST_CHANGES"]),
            escalate_keywords=r.get("escalate_keywords", ["ESCALATE"]),
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
        )
    if "checks" in raw:
        c = raw["checks"]
        cfg.checks = ChecksConfig(commands=c.get("commands", []), timeout=int(c.get("timeout", 900)),
                                  retry_checks=int(c.get("retry_checks", 3)),
                                  auto_resolve=bool(c.get("auto_resolve", False)),
                                  resolve_attempts=int(c.get("resolve_attempts", 2)))
    if "e2e" in raw:
        e = raw["e2e"]
        cfg.e2e = E2eConfig(
            enabled=bool(e.get("enabled", False)),
            apps=dict(e.get("apps", {})),
            spec_glob=e.get("spec_glob", E2eConfig().spec_glob),
            timeout=int(e.get("timeout", 1800)),
        )
    if "git" in raw:
        g = raw["git"]
        cfg.git = GitConfig(base_branch=g.get("base_branch", "main"),
                            auto_ship=bool(g.get("auto_ship", False)),
                            auto_merge=bool(g.get("auto_merge", False)))
    if "notify" in raw:
        n = raw["notify"]
        cfg.notify = NotifyConfig(backend=n.get("backend", "none"), webhook_url=n.get("webhook_url", ""))
    if "skills" in raw:
        sk = raw["skills"]
        installers = {k: dict(v) for k, v in DEFAULT_SKILL_INSTALLERS.items()}
        installers.update(sk.get("installers", {}))
        cfg.skills = SkillsConfig(enabled=sk.get("enabled", []), installers=installers)
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
    return cfg
