# Agent Harness

Claude Code stage harness for product/spec → architecture/design → implementation-plan → build → evidence workflows.

## Dedicated Telegram HITL bot

Use a separate Telegram bot for harness human-in-the-loop questions. Do **not** use the normal Hermes Telegram bot for this, because any reply to the Hermes bot starts a separate Hermes gateway chat session.

Required environment variables:

```bash
export HARNESS_TELEGRAM_BOT_TOKEN='<botfather-token-for-harness-bot>'
export HARNESS_TELEGRAM_CHAT_ID='<your-telegram-chat-id>'
```

Behavior:

- `scripts/send_question.py` sends questions directly through Telegram Bot API when both env vars are set.
- `scripts/telegram_reply_watcher.py` polls that same harness bot with `getUpdates`, records the answer under `.agent-runs/<run_id>/answers/<stage>.md`, and can resume the stored Claude Code session with `--auto-resume`.
- If the env vars are missing, scripts fall back to the old Hermes gateway/state.db path.
- On this machine, a no-agent Hermes cron named `agent-harness-telegram-watcher` runs the watcher every minute via `~/.hermes/scripts/agent-harness-telegram-watcher.sh`.

Commands:

```bash
python3 agent-harness/scripts/send_question.py --run-id <run_id> --question-file questions/<stage>-inline-question.json
python3 agent-harness/scripts/telegram_reply_watcher.py --auto-resume
```

Recommended reply format: use Telegram's reply action on the question message and type only the answer. The watcher maps the reply-to message id back to the run/stage.

Fallbacks still supported by the watcher for debugging: `/answer <run_id> <stage> <answer>`, `/a <run_id> <stage> <answer>`, and `ANSWER <run_id> <stage>: <answer>`.

Telegram does not provide true slash-command argument dropdowns. For one-click selection later, use an inline keyboard button per pending question with callback data containing the run/stage, then ask only for the free-text answer.

## Complete workflow

Default Hermes is the intake/orchestrator. There is no separate intake agent and no Ragnar stage.

Artifacts:

```text
.agent-runs/<run_id>/
  intake.md
  product-spec.md
  architecture-design.md
  implementation-plan.md
  build-summary.md
  evidence-report.md
  review-result.json
```

Boards:

```text
edupaid-odin  -> odin-pm         -> product-spec.md
edupaid-thor  -> thor-architect  -> architecture-design.md
```

Only one task is created at a time for these boards:

1. Default Hermes creates the run and an Odin task only.
2. Odin picks up the task, writes `product-spec.md`, then blocks the task for human review instead of marking it done.
3. You review the artifact. If changes are needed, add comments and unblock the Odin task. If approved, run:
   ```bash
   python3 agent-harness/scripts/advance_flow.py --run-id <run_id> --stage product-spec --approve
   ```
4. Approval marks the Odin task done and creates the Thor task.
5. Thor writes `architecture-design.md`, then blocks the task for human review.
6. You review. If changes are needed, add comments and unblock Thor. If approved, run:
   ```bash
   python3 agent-harness/scripts/advance_flow.py --run-id <run_id> --stage architecture-design --approve
   ```
7. The run becomes `ready_for_claude_plan`; trigger Claude Code plan mode next.
   ```bash
   python3 agent-harness/scripts/trigger_claude_plan.py --run-id <run_id>
   ```

The `start_flow.py` and approval helpers enforce one active task per Odin/Thor board. If a board already has a non-terminal task, they stop instead of creating another.

Start a run:

```bash
python3 agent-harness/scripts/start_flow.py --title "<title>" --task-class feature --risk medium --request "<full request>"
```

Request revisions:

```bash
python3 agent-harness/scripts/advance_flow.py --run-id <run_id> --stage product-spec --revise "<comments>"
python3 agent-harness/scripts/advance_flow.py --run-id <run_id> --stage architecture-design --revise "<comments>"
```

## Reviewer gate

After Claude Code evidence is complete, run the independent GPT-5.5 reviewer:

```bash
python3 agent-harness/scripts/reviewer_gate.py --run-id <run_id>
```

The reviewer uses default Hermes with `openai-group/gpt-5.5`. Reviewer session identity is stored in:

```text
.agent-runs/<run_id>/reviewer-session.json
```

Every later reviewer pass for the same run resumes that same Hermes reviewer session. This keeps review context tied to the task while still using a different model family from Claude Code.

Reviewer outputs:

```text
review-result.json
review-gate.json
review-comments.md   # only when REQUEST_CHANGES
```

Verdicts:

```text
APPROVE          -> status review_approved
REQUEST_CHANGES  -> status review_changes_requested; resume Claude build with review-comments.md
ESCALATE         -> status review_escalated; human decision required
```
