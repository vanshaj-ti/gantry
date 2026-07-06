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

# The ordered pipeline. Boards are gone; gates are enforced via `gantry approve`.
# doc-stages (spec/design) produce a markdown artifact and pause for human review.
# agent-stages (plan/build/evidence) invoke the agent runner.
DEFAULT_STAGES = ["spec", "design", "plan", "build", "evidence", "review"]

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


@dataclass
class ChecksConfig:
    """Delegate house rules to the repo's own toolchain. Gantry runs these and
    gates on exit code. Works on any repo/language."""
    commands: list[str] = field(default_factory=list)  # e.g. ["npm run lint", "npm run build"]
    timeout: int = 900


@dataclass
class GitConfig:
    base_branch: str = "main"             # diff base for scope/review (was origin/staging)


@dataclass
class NotifyConfig:
    backend: str = "none"                 # "none" | "telegram" | "webhook"
    # telegram: reads GANTRY_TELEGRAM_BOT_TOKEN / GANTRY_TELEGRAM_CHAT_ID from env
    # webhook: posts JSON to this url
    webhook_url: str = ""


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
    git: GitConfig = field(default_factory=GitConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    # prompts dir: where stage prompt templates live (relative to config, or absolute)
    prompts_dir: str = ".gantry/prompts"

    def model_for(self, stage: str) -> StageModel:
        if stage in self.models:
            return self.models[stage]
        # sensible default so a bare config still runs
        return StageModel(model="", max_turns=60, plan_mode=(stage == "plan"))

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
        cfg.scope = ScopeConfig(
            forbid_paths=s.get("forbid_paths", ScopeConfig().forbid_paths),
            enforce_plan_scope=bool(s.get("enforce_plan_scope", True)),
        )
    if "checks" in raw:
        c = raw["checks"]
        cfg.checks = ChecksConfig(commands=c.get("commands", []), timeout=int(c.get("timeout", 900)))
    if "git" in raw:
        cfg.git = GitConfig(base_branch=raw["git"].get("base_branch", "main"))
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
    return cfg
