# Gantry Strategic Competitive Analysis

**Four-repo analysis on landscape positioning, integration opportunities, and viability verdict.**

---

## Executive Summary

Gantry is a deterministic staged pipeline CLI for autonomous AI coding agent orchestration (spec→design→plan→build→evidence→review) with human gates, repo-linter checks, and independent LLM review. State persists in `.agent-runs/` per repo, with dashboard and auto-advance cron.

**The landscape verdict: Gantry should pivot toward skills integration rather than compete as a standalone CLI.** The #4 repo (agent-skills) represents the dominant paradigm, with 70K stars and massive author distribution. Repos #1-3 occupy complementary niches (orchestration UI, terminal multiplexing, federated multi-agent coordination), but the skills-as-context approach scales better with the agent ecosystem than a separate build pipeline tool.

---

## Repo Analysis

### 1. Orca (stablyai/orca) — Desktop+Mobile Multi-Agent Orchestrator

**What it actually is:**
- **Tier:** Orchestration layer / IDE environment for agents
- **Scope:** Desktop/mobile app (TypeScript) + CLI for running multiple coding agents side-by-side with each in its own git worktree
- **Key features:** Parallel worktree management, mobile companion app, terminal splits with WebGL, design mode (click UI elements to capture for agents), GitHub/Linear native integration, SSH worktrees, diff annotation, Computer Use, account switcher, notifications
- **Architecture:** Agent-agnostic (works with Claude Code, Cursor, Codex, Grok, GitHub Copilot, etc.); state lives in app context; CLI surface exposes `orca worktree create`, `snapshot`, `click`, `fill` for scriptability
- **Stats:** 12.6K stars, TypeScript, active (last update today)

**Relationship to Gantry:**
- **Position:** ABOVE (orchestration layer / workbench for running agents)
- **Overlap:** Both manage agent state and multi-worktree workflows, but Orca is the **IDE/UI** and Gantry is the **deterministic pipeline driver**
- **Competitive threat:** Low. Orca orchestrates *running* agents; Gantry orchestrates *stage transitions* within a build pipeline

**Integration paths:**
1. Gantry as a "stage driver" *within* Orca: Orca worktrees invoke `gantry advance --agent <name>` to drive stage progression while remaining under Orca's terminal/UI supervision
2. Gantry could expose `.orca/hooks` integration to subscribe to agent state changes and auto-trigger stage gates
3. Orca's mobile companion (push notifications when agent finishes) maps cleanly to Gantry's human gate approval workflow
4. Layer Gantry gates *around* Orca's existing snapshot/click/fill primitives

**Verdict:** **RUN-PARALLEL** — Orca is the *execution environment*, Gantry is the *pipeline governance*. They complement rather than compete. Integration could be powerful (Orca spawns agents in stages, Gantry enforces gates), but neither requires the other.

---

### 2. Herdr (ogulcancelik/herdr) — Terminal Multiplexer for Agents

**What it actually is:**
- **Tier:** Terminal multiplexing / session persistence layer
- **Scope:** Single ~10MB Rust binary acting like tmux but designed for agents — persistent sessions, detach/reattach over SSH, real terminal panes, agent state detection (🔴 blocked, 🟡 working, 🔵 done, 🟢 idle)
- **Key features:** Mouse-native UI, workspace/tab/pane management, real terminal rendering (not GUI emulation), agent-aware status sidebar (process matching + heuristics for ~16 agents), local Unix socket API for agent orchestration, plugin system
- **Architecture:** Server-client (socket-based), agent detection out-of-the-box for Claude Code, Codex, Cursor, Devin, Grok, GitHub Copilot, Hermes Agent, etc.; integrations add native session restore
- **Stats:** ~16.5K stars, Rust, trending (#1 on June 30, 2026)

**Relationship to Gantry:**
- **Position:** BESIDE (complementary terminal infrastructure)
- **Overlap:** Both address agent state visibility and multi-agent orchestration, but herdr is **session persistence/viewing**, Gantry is **pipeline gating**
- **Competitive threat:** None. Herdr doesn't enforce stage gates or deterministic pipelines

**Integration paths:**
1. Gantry emits stage changes to herdr's socket API (herdr sidebar shows which stage each agent is in, not just working/blocked/done)
2. Herdr UI allows humans to approve gates, invoking `gantry advance --agent <name>` when approved
3. Gantry dashboard runs *inside* herdr workspace as a pane, using herdr as terminal multiplexer
4. **Gantry exposes a Unix socket (matching herdr's model)** for agents to report completion, request approval, query stage state

**Verdict:** **BORROW-PATTERN** — Herdr's socket API and agent-aware state detection are proven patterns. Gantry should expose a Unix socket for orchestration, matching herdr's model. The two form a natural stack: herdr multiplexes terminals, Gantry drives stage progression.

---

### 3. Gas Town (gastownhall/gastown) — Multi-Agent Coordination, Git-Backed Persistent State

**What it actually is:**
- **Tier:** Multi-agent workflow coordinator / CI-like refinery system
- **Scope:** Go-based orchestrator for 20-30 agents working on same codebase; persistent work state via git worktrees and Beads (git-backed issue/ledger); mailboxes/handoffs; hierarchical roles (Mayor, Polecats, Witness, Deacon, Boot, Refinery, Scheduler)
- **Key features:** Multi-agent coordination at scale, Beads ledger (structured work tracking), git worktree-backed hooks, merge queue (Bors-style bisecting), escalation/health monitoring (Witness per-rig, Deacon cross-rig), convoy batching, Wasteland federation (DoltHub-backed cross-Town coordination), seance (session discovery/continuation), activity feed TUI, scheduler for rate limiting
- **Architecture:** Daemon-driven (Go), integrates with Claude Code/Codex/GitHub Copilot, uses Dolt for distributed storage, Beads for issue tracking, tmux for sessions
- **Stats:** 16.5K stars, Go, active (last update today)

**Relationship to Gantry:**
- **Position:** ABOVE/BESIDE (different scope — Gas Town is *multi-repo federated multi-agent*, Gantry is *single-repo staged pipeline*)
- **Overlap:** Both enforce deterministic state transitions and gate progression; both persist state in repos; both target orchestrating multiple agents
- **Competitive threat:** Medium-high, but different vertical. Gas Town is for 20-30 agents across projects; Gantry is for staged deterministic progression within *one* project. Gas Town doesn't enforce spec→plan→build stages.

**Integration paths:**
1. **Gantry as stage-driver for Gas Town Polecats:** Polecat invokes `gantry advance --all` to drive assigned project through stages, with Gas Town handling multi-agent/multi-repo coordination *around* Gantry stages
2. **Borrow Gas Town's Beads model:** Replace raw `.agent-runs/` metadata with Beads-style git-backed structured data for stage evidence, enabling queryable/federable Gantry runs (like Gas Town's Wasteland)
3. **Merge queue integration:** Gas Town Refinery skips MR verification if a Gantry run in `.agent-runs/` attests the MR passed all gates
4. **Escalation integration:** Gantry gate failures (linter, LLM review blocks) escalate to Gas Town's escalation system
5. **Socket API parity:** Both expose Unix sockets for orchestration

**Verdict:** **BORROW-PATTERN** — Gas Town's hierarchical monitoring (Witness/Deacon), Beads ledger, and escalation are gold. Gantry should not be Gas Town (federated 20-30 agents); be the *stage driver* that Gas Town Polecats invoke. Two solve adjacent problems: Gas Town says "who does what across projects," Gantry says "how does a project progress through stages deterministically."

---

### 4. Agent Skills (addyosmani/agent-skills) — Production Lifecycle Skills (Skills-as-Context)

**What it actually is:**
- **Tier:** Agent context / workflow encoding layer
- **Scope:** 24 Markdown-based skills encoding production workflows (DEFINE→PLAN→BUILD→VERIFY→REVIEW→SHIP) with steps, verification gates, anti-rationalization tables, red flags
- **Key features:** Slash commands (`/spec`, `/plan`, `/build`, `/test`, `/review`, `/ship`, `/webperf`, `/code-simplify`); `/build auto` for autonomous spec→plan→implement in one pass; skills auto-activate by task type; 4 specialist personas; reference checklists
- **Installation:** Claude Code marketplace (one-click zero-install), Cursor rules, native plugins for Antigravity/Gemini/Kiro, plain Markdown for any agent
- **Architecture:** Context-agnostic — Markdown loads into system prompt, rules files, or session; no runtime; no state outside agent session
- **Stats:** 70.3K stars (highest on list), Shell, active (last update today)

**Relationship to Gantry:**
- **Position:** DIRECTLY OVERLAPPING — Closest prior art to Gantry
- **Overlap:**
  - Both implement spec→plan→build→test/verify→review→ship lifecycle
  - Both enforce step ordering (spec before code, tests before ship)
  - Both include verification gates (tests passing, build output, security checks)
  - Both target same user (engineer wanting agents to ship production code safely)
- **Architectural difference:**
  - **agent-skills:** Skills in agent context (Markdown in prompt/rules); ephemeral per session; zero install friction; 70K stars, millions installed; **but no deterministic gating outside model**, **no runner agnosticism** (skills are Claude-native; other agents load as text; no CLI driving them), **state lives in agent memory** (lost on restart)
  - **Gantry:** Deterministic CLI pipeline driver; state in `.agent-runs/` per repo; works with any CLI agent (Claude Code, Cursor, Devin, Grok as black boxes); human gates *outside* model; repo-linter and independent LLM review are *tooling enforced*, not workflow suggestions

**Competitive threat:** VERY HIGH. 70K stars (40x larger than any other repo here), shipped by top engineering leader, zero-install friction, massive distribution.

**Integration paths:**
1. **Gantry as CLI wrapper around agent-skills:** `gantry spec` → agent-skills `/spec`, then Gantry gates (linter, independent review, validation)
2. **Hybrid model:** Use agent-skills for workflow (spec/plan/build), use Gantry for gating (deterministic approval, linter, cross-model review)
3. **Gantry as skills-plugin:** Ship as Claude Code plugin that adds `/gantry spec`, `/gantry plan`, `/gantry build` *on top of* agent-skills, with deterministic gating
4. **Adopt agent-skills patterns:** Gantry specs as Markdown (matching agent-skills anatomy), anti-rationalization tables, red flags

**Verdict (Critical decision):** **PIVOT-NOT-COMPETE**

**Blunt honest assessment:**

Gantry does **not earn its existence as a standalone CLI** versus agent-skills-as-skills. Here's why:

1. **Distribution:** 70K stars means agent-skills is already in millions of Claude Code sessions. Gantry would need equivalent adoption. The economics don't favor new CLI tools; skills approach has won the distribution war.

2. **Friction:** agent-skills has zero install friction (marketplace one-click or copy Markdown). Gantry requires `pip install gantry` + config + `.agent-runs/` setup. For 100 projects, agent-skills scales (load once globally); Gantry requires per-repo setup.

3. **Gantry's edge (deterministic gating OUTSIDE model):** Valid, but solving for unproven problem. agent-skills users are happy with in-context gates (tests before merge, review before ship). Gantry's hard gates (linter-enforced, independent LLM review as separate invocation) add friction for unproven benefit. "Independent LLM review" is interesting but not proven better than in-context review at scale.

4. **Runner agnosticism (works with Cursor, Devin, etc.):** Valid differentiator. But agent-skills already has Cursor guide and plain-Markdown mode (works with anything). Gantry's edge: *orchestrates* agent (drives `gantry spec`, then `gantry plan`); agent-skills just *advises*. If runner agnosticism is the goal, Gantry could be a *skills-plugin* that Cursor/Devin/Gemini load, not separate CLI.

5. **Real opportunity:** Gantry's gates (linter, independent review) are tooling that can be *expressed as skills* or added to Gantry-as-skills-plugin. No architectural reason Gantry must be CLI; it could be **Claude Code plugin augmenting agent-skills** with deterministic gates, with CLI hooks for non-Claude tools.

**Recommended pivot:**

1. **Short term:** Ship Gantry as Claude Code plugin augmenting agent-skills:
   - `/gantry spec` → agent-skills `/spec`, then Gantry gates (linter, independent review, validation)
   - `/gantry plan` → agent-skills `/plan`, then Gantry gates
   - `/gantry build` → agent-skills `/build`, then Gantry gates (linter, repo-linter, coverage gates)
   - State in `.agent-runs/` or Beads-style ledger
   - Reuse agent-skills structure (anatomy, anti-rationalization, red flags)

2. **Medium term:** Ship Gantry as standalone skill for Cursor, Devin, Gemini, Codex

3. **Long term:** CLI becomes *orchestration wrapper* for multi-agent pipelines (like Gas Town), not single-agent stage driver. CLI is "stage bus" coordinating multiple agents through Gantry gates across monorepo.

**Can Gantry survive as standalone CLI?** Technically yes, but loses distribution war to agent-skills + native support. Window to shift is *now* (Gantry is early); waiting 6 months means agent-skills will have even more market share.

---

## Summary Matrix

| Repo | Category | Position | Compete? | Integration Opportunity | Verdict |
|------|----------|----------|----------|-------------------------|---------|
| Orca | Orchestration UI/IDE | ABOVE | No | Gantry stages from Orca worktrees | RUN-PARALLEL |
| Herdr | Terminal Multiplexing | BESIDE | No | Socket API for state sync; herdr as backend | BORROW-PATTERN |
| Gas Town | Multi-Agent Coordinator | ABOVE/BESIDE | Medium | Gantry as stage driver for Polecats; borrow patterns | BORROW-PATTERN |
| Agent Skills | Workflow Skills | DIRECTLY OVERLAPPING | Yes (distribution) | **Pivot: Gantry as plugin augmenting agent-skills** | **PIVOT-NOT-COMPETE** |

---

## Key Recommendations

1. **Abandon Gantry-as-standalone-CLI positioning.** Skills paradigm (agent-skills 70K stars) has won. Competing head-to-head for CLI adoption is losing bet.

2. **Ship Gantry as Claude Code plugin.** Use agent-skills as foundation (adopt anatomy, commands, personas), add Gantry gates on top (deterministic linter, independent review, validation).

3. **Borrow from herdr (socket API):** Gantry exposes Unix socket for external tools/agents to drive stage progression and query state. Don't reinvent what herdr proved.

4. **Borrow from Gas Town (Witness/Deacon monitoring):** For multi-agent scenarios, adopt hierarchical health patterns. Don't replace Gas Town; be the stage driver it orchestrates.

5. **Make state Beads-compatible:** Use Gas Town's Beads model for stage evidence so Gantry runs are queryable and federable (Wasteland).

6. **Keep CLI, narrow scope:** CLI becomes *multi-repo orchestrator* (like Gas Town, stages-focused), not single-agent driver. Single agents get Gantry as plugin/skill.

---

## Conclusion

Gantry's core insights (deterministic stages, repo-linter checks, independent LLM review) are sound. But delivery mechanism (standalone CLI) is wrong for 2026. Market has spoken: agent-skills' skills-as-context model dominates. Gantry should integrate with that model (ship as plugin), not compete.

**Window to pivot is now.** In 6 months, agent-skills will be so entrenched that any new stage tool will be niche curiosity, not standard practice.

**Verdict: Continue building Gantry, but as a Claude Code plugin that augments agent-skills, not as a standalone CLI.** The insights are valuable; the delivery model needs a 180° rotation.
