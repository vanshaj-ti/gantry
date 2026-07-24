"""End-to-end Linear intake exercise: a signed synthetic Linear webhook drives
classification, issue tagging, and gantry run creation, then the created run
is driven through every stage of its queue to a shipped/terminal state —
proving the whole loop (Linear -> classify -> gantry pipeline -> ship) works
before any real Linear/cloud infra is involved.

Mirrors tests/test_pipeline_e2e.py's pattern: real scratch git repo, real
Store/Engine/checks execution, but the LLM call itself faked via
patch(".get_runner", ...) — no real `claude` subprocess, no real network I/O
(Linear's GraphQL calls are patched at gantry.linear._graphql).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.advance import advance_run
from gantry.config import GantryConfig
from gantry.engine import Engine
from gantry.linear import (
    classify_ticket,
    handle_comment_created,
    handle_issue_created,
    verify_webhook_signature,
    verify_webhook_timestamp,
)
from gantry.runners import RunnerResult
from gantry.status import Status

TEST_SECRET = "test-webhook-signing-secret"
TEST_TEAM_ID = "team-123"
TEST_API_KEY = "test-api-key"


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    (path / "lint.sh").write_text("#!/bin/sh\nexit 0\n")
    (path / "lint.sh").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "staging"], cwd=str(path), check=True)


def _agent_result(text: str, session: str = "s") -> RunnerResult:
    return RunnerResult(ok=True, session_id=session, exit_code=0, raw={"result": text},
                        stdout=text, stderr="")


def _make_signed_issue_payload(issue_id: str, title: str, description: str) -> dict:
    """A Linear Issue-create webhook payload, matching the documented shape:
    https://linear.app/developers/webhooks#data-change-events-payload"""
    return {
        "action": "create",
        "type": "Issue",
        "actor": {"id": "u1", "type": "user", "name": "Reporter"},
        "createdAt": "2026-07-23T00:00:00.000Z",
        "data": {"id": issue_id, "title": title, "description": description},
        "url": f"https://linear.app/team/issue/{issue_id}",
        "webhookTimestamp": int(time.time() * 1000),
    }


class TestWebhookSecurity(unittest.TestCase):
    """Pure logic, no mocking — proves the signature/timestamp checks actually
    round-trip against gantry.linear's own signing algorithm."""

    def test_valid_signature_verifies(self):
        import hashlib
        import hmac
        payload = json.dumps(_make_signed_issue_payload("i1", "t", "d")).encode()
        sig = hmac.new(TEST_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook_signature(payload, sig, TEST_SECRET))

    def test_wrong_secret_fails_verification(self):
        payload = json.dumps(_make_signed_issue_payload("i1", "t", "d")).encode()
        self.assertFalse(verify_webhook_signature(payload, "deadbeef", "wrong-secret"))

    def test_missing_signature_fails(self):
        payload = b"{}"
        self.assertFalse(verify_webhook_signature(payload, None, TEST_SECRET))

    def test_stale_timestamp_rejected(self):
        old_ms = int(time.time() * 1000) - 120_000  # 2 minutes ago
        self.assertFalse(verify_webhook_timestamp(old_ms))

    def test_fresh_timestamp_accepted(self):
        self.assertTrue(verify_webhook_timestamp(int(time.time() * 1000)))


class _ClassifierRunner:
    """Fake runner for the classifier's single agent turn — always answers
    with whatever tag this test wants classified."""

    name = "claude-code"

    def __init__(self, tag: str):
        self.tag = tag

    def run(self, **kwargs):
        return _agent_result(self.tag)


class TestBugQueueEndToEnd(unittest.TestCase):
    """Full loop for the bug queue: webhook -> classify -> tag issue -> gantry
    run (investigation-led pipeline) -> every stage -> shipped."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.cfg.checks.commands = ["sh lint.sh"]
        self.cfg.git.base_branch = "staging"
        self.cfg.git.auto_approve_docs = True
        self.cfg.git.auto_ship = False  # ship exercised directly
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_webhook_to_shipped(self):
        payload = _make_signed_issue_payload(
            "issue-1", "Login button does nothing on mobile",
            "Tapping the login button on iOS Safari produces no response.",
        )
        raw_body = json.dumps(payload).encode()

        # --- verify (real logic, no mocking) ---
        import hashlib
        import hmac
        sig = hmac.new(TEST_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_webhook_signature(raw_body, sig, TEST_SECRET))
        self.assertTrue(verify_webhook_timestamp(payload["webhookTimestamp"]))

        # --- classify + tag + create run (classifier + Linear GraphQL mocked) ---
        graphql_calls = []

        def fake_graphql(query, variables, api_key):
            graphql_calls.append((query, variables))
            if "issueLabelCreate" in query:
                return {"issueLabelCreate": {"success": True, "issueLabel": {"id": "label-bug"}}}
            if "team(id" in query:
                return {"team": {"labels": {"nodes": []}}}  # no existing labels -> create one
            if "issue(id" in query and "labels" in query:
                return {"issue": {"labels": {"nodes": []}}}  # no prior labels on the issue
            return {"issueUpdate": {"success": True}} if "issueUpdate" in query else \
                   {"commentCreate": {"success": True}}

        with patch("gantry.runners.get_runner", return_value=_ClassifierRunner("bug")), \
             patch("gantry.linear._graphql", side_effect=fake_graphql), \
             patch("gantry.linear.load_config", return_value=self.cfg), \
             patch("gantry.linear.Engine", return_value=self.eng):
            result = handle_issue_created(payload, TEST_TEAM_ID, TEST_API_KEY, self.target)

        self.assertEqual(result["tag"], "bug")
        run_id = result["run_id"]
        self.assertTrue(any("issueUpdate" in q for q, _ in graphql_calls))
        self.assertTrue(any("commentCreate" in q for q, _ in graphql_calls))

        # --- run's own stages reflect the bug queue's built-in pipeline ---
        store = self.eng.store
        self.assertEqual(self.eng.stages_for_run(run_id),
                         ["investigation", "plan", "build", "evidence", "review"])
        self.assertEqual(store.state(run_id)["status"], "awaiting_investigation")

        # --- drive every stage with a stage-aware fake agent runner ---
        def write_investigation(**kwargs):
            store.artifact_path(run_id, "investigation-report.md").write_text(
                "# Investigation\n\nRoot cause: missing touchend handler on iOS Safari.\n")
            return _agent_result("investigation written")

        def write_plan(**kwargs):
            store.artifact_path(run_id, "implementation-plan.md").write_text(
                "# Plan\n\n## Allowed files\n`app.py`\n\n## Ordered implementation steps\n"
                "1. add touchend handler (verify: `sh lint.sh`)\n")
            store.artifact_path(run_id, "allowed-files.json").write_text(
                json.dumps({"allowed_globs": ["app.py"]}))
            return _agent_result("plan written")

        def write_build(**kwargs):
            wt = self.eng.work_dir(run_id)
            (wt / "app.py").write_text("def on_touchend():\n    return True\n")
            subprocess.run(["git", "add", "app.py"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-m", "fix touchend handler", "-q"], cwd=str(wt), check=True)
            store.artifact_path(run_id, "build-summary.md").write_text("# Build\n\nfixed touchend.\n")
            return _agent_result("build done")

        def write_evidence(**kwargs):
            store.artifact_path(run_id, "evidence-report.md").write_text(
                "# Evidence\n\nRan sh lint.sh, exit 0. Recommendation: PASS\n")
            return _agent_result("evidence written")

        def write_review(**kwargs):
            findings = json.dumps({"findings": []})
            return _agent_result(
                f"APPROVE\n\nRoot cause fixed, matches investigation.\n\n```json\n{findings}\n```\n"
                "## Verification Story\nRan sh lint.sh myself, confirmed exit 0.\n")

        scripted = {
            "investigation": write_investigation, "plan": write_plan, "build": write_build,
            "evidence": write_evidence,
        }

        class _StageAwareRunner:
            name = "claude-code"

            def run(self, **kwargs):
                session_name = kwargs.get("session_name", "")
                for stage, fn in scripted.items():
                    if session_name == f"{run_id}-{stage}":
                        return fn(**kwargs)
                if "review" in session_name:
                    return write_review(**kwargs)
                raise AssertionError(f"unscripted session_name: {session_name}")

        runner = _StageAwareRunner()

        with patch("gantry.engine.get_runner", return_value=runner), \
             patch("gantry.review.get_runner", return_value=runner):
            self.eng.run_agent_stage(run_id, "investigation")
            self.assertEqual(store.state(run_id)["status"], "investigation_complete")

            r = advance_run(self.eng, run_id)  # auto-approve investigation -> awaiting_plan
            self.assertIn("auto_approved_investigation", r["action"])

            r = advance_run(self.eng, run_id)  # plan
            self.assertEqual(store.state(run_id)["status"], Status.PLAN_COMPLETE)

            r = advance_run(self.eng, run_id)  # build
            self.assertEqual(store.state(run_id)["status"], Status.BUILD_COMPLETE)

            r = advance_run(self.eng, run_id)  # checks -> evidence
            self.assertEqual(r["action"], "checks_passed->evidence")
            self.assertEqual(store.state(run_id)["status"], Status.EVIDENCE_COMPLETE)

            r = advance_run(self.eng, run_id)  # review
            self.assertEqual(r["verdict"], "APPROVE")
            self.assertEqual(store.state(run_id)["status"], Status.REVIEW_APPROVED)

        # --- ship ---
        from gantry.ship import ship_run
        with patch("gantry.ship.draft_ship_meta",
                   return_value={"title": "fix login button", "body": "## Summary\n\nfix", "branch_slug": "fix-login"}), \
             patch("gantry.ship.commit_all", return_value={"ok": True, "committed": True, "output": ""}), \
             patch("gantry.ship.push", return_value={"ok": True, "output": "", "remote_branch": "fix/x"}), \
             patch("gantry.ship.create_pr", return_value={"ok": True, "url": "https://example.com/pr/1", "output": ""}):
            ship_result = ship_run(self.eng, run_id)

        self.assertTrue(ship_result["ok"])
        self.assertEqual(store.state(run_id)["status"], "shipped")


class TestHotfixQueueSkipsReview(unittest.TestCase):
    """Hotfix's stage list (build/evidence, no review) must reach a
    ship-eligible terminal state cleanly, without advance_run trying to
    invoke a review stage that isn't in this run's own pipeline."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.cfg.checks.commands = ["sh lint.sh"]
        self.cfg.git.base_branch = "staging"
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_hotfix_run_reaches_terminal_without_review_stage(self):
        run_id = self.eng.create_run("urgent fix", "fix the thing now", tag="hotfix")
        self.assertEqual(self.eng.stages_for_run(run_id), ["build", "evidence"])
        # No "plan" stage either — hotfix starts straight at build.
        self.assertEqual(self.eng.store.state(run_id)["status"], "awaiting_build")
        store = self.eng.store

        def write_build(**kwargs):
            wt = self.eng.work_dir(run_id)
            (wt / "hotfix.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "hotfix.py"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-m", "hotfix", "-q"], cwd=str(wt), check=True)
            store.artifact_path(run_id, "build-summary.md").write_text("# Build\n\nhotfix applied.\n")
            return _agent_result("build done")

        def write_evidence(**kwargs):
            store.artifact_path(run_id, "evidence-report.md").write_text(
                "# Evidence\n\nRan sh lint.sh, exit 0.\n")
            return _agent_result("evidence written")

        class _Runner:
            name = "claude-code"

            def run(self, **kwargs):
                sn = kwargs.get("session_name", "")
                return write_evidence(**kwargs) if sn.endswith("-evidence") else write_build(**kwargs)

        with patch("gantry.engine.get_runner", return_value=_Runner()), \
             patch("gantry.review.get_runner", return_value=_Runner()):
            advance_run(self.eng, run_id)  # build
            self.assertEqual(store.state(run_id)["status"], Status.BUILD_COMPLETE)

            advance_run(self.eng, run_id)  # checks pass -> evidence (last stage, no review configured)
            self.assertEqual(store.state(run_id)["status"], Status.EVIDENCE_COMPLETE)

        # evidence_complete with no further stage in this run's own list —
        # confirm it lands on a ship-eligible terminal state, not stuck
        # waiting on a review stage this queue never has.
        final_status = store.state(run_id)["status"]
        self.assertIn(final_status, (Status.EVIDENCE_COMPLETE, Status.REVIEW_APPROVED))


class TestCommentReplyPath(unittest.TestCase):
    """Linear is the only human-input channel for this target — a human's
    reply on the issue (a Comment webhook event) must resolve to the run
    that issue tracks and dispatch through the same deterministic
    status-driven gating logic Telegram replies already use. No agent call
    in the routing itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.cfg.git.base_branch = "staging"
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_comment_on_untracked_issue_is_a_noop(self):
        payload = {"type": "Comment", "action": "create",
                  "data": {"issueId": "issue-never-tracked", "body": "approve"}}
        result = handle_comment_created(payload, TEST_API_KEY, self.target)
        self.assertFalse(result["handled"])

    def test_gantry_own_comment_is_never_treated_as_a_reply(self):
        """Real incident this guards against: gantry posts a status comment
        -> Linear delivers it back as a Comment webhook event -> without
        this check, handle_comment_created would treat it as a human reply
        and resume the stage -> which posts another comment -> infinite
        loop. Confirmed live against a real Linear team."""
        from gantry.linear import post_comment
        run_id = self.eng.create_run("t", "r", tag="bug")
        self.eng.store.record_linear_issue("issue-loop", run_id)

        posted_bodies = []

        def fake_graphql(query, variables, api_key):
            if "commentCreate" in query:
                posted_bodies.append(variables["body"])
                return {"commentCreate": {"success": True}}
            return {}

        with patch("gantry.linear._graphql", side_effect=fake_graphql):
            post_comment("issue-loop", "Classified as `bug`. Gantry run created.", TEST_API_KEY)

        self.assertEqual(len(posted_bodies), 1)
        gantry_comment_body = posted_bodies[0]

        # Simulate Linear delivering that exact comment back as a webhook event.
        payload = {"type": "Comment", "action": "create",
                  "data": {"issueId": "issue-loop", "body": gantry_comment_body}}
        result = handle_comment_created(payload, TEST_API_KEY, self.target)
        self.assertFalse(result["handled"])
        self.assertIn("gantry itself", result["reason"])

    def test_comment_approve_advances_investigation_stage(self):
        run_id = self.eng.create_run("t", "r", tag="bug")
        store = self.eng.store
        store.record_linear_issue("issue-42", run_id)

        def write_investigation(**kwargs):
            store.artifact_path(run_id, "investigation-report.md").write_text("# Investigation\n\nfound it.\n")
            return _agent_result("investigation written")

        class _Runner:
            name = "claude-code"

            def run(self, **kwargs):
                return write_investigation(**kwargs)

        with patch("gantry.engine.get_runner", return_value=_Runner()):
            self.eng.run_agent_stage(run_id, "investigation")
        self.assertEqual(store.state(run_id)["status"], "investigation_complete")

        graphql_calls = []

        def fake_graphql(query, variables, api_key):
            graphql_calls.append((query, variables))
            return {"commentCreate": {"success": True}}

        payload = {"type": "Comment", "action": "create",
                  "data": {"issueId": "issue-42", "body": "approve"}}
        with patch("gantry.linear._graphql", side_effect=fake_graphql):
            result = handle_comment_created(payload, TEST_API_KEY, self.target)

        self.assertTrue(result["handled"])
        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(store.state(run_id)["status"], "awaiting_plan")
        # The approval confirmation got posted back as a Linear comment.
        self.assertTrue(any("commentCreate" in q for q, _ in graphql_calls))

    def test_comment_with_feedback_resumes_stage_with_answer(self):
        run_id = self.eng.create_run("t", "r", tag="bug")
        store = self.eng.store
        store.record_linear_issue("issue-99", run_id)

        calls = {"n": 0}

        def write_investigation(**kwargs):
            calls["n"] += 1
            store.artifact_path(run_id, "investigation-report.md").write_text(
                f"# Investigation\n\nattempt {calls['n']}\n")
            return _agent_result(f"investigation written, attempt {calls['n']}")

        class _Runner:
            name = "claude-code"

            def run(self, **kwargs):
                return write_investigation(**kwargs)

        with patch("gantry.engine.get_runner", return_value=_Runner()):
            self.eng.run_agent_stage(run_id, "investigation")
        self.assertEqual(store.state(run_id)["status"], "investigation_complete")
        self.assertEqual(calls["n"], 1)

        payload = {"type": "Comment", "action": "create",
                  "data": {"issueId": "issue-99",
                           "body": "not quite — also check the retry path"}}
        with patch("gantry.linear._graphql", return_value={"commentCreate": {"success": True}}), \
             patch("gantry.engine.get_runner", return_value=_Runner()):
            result = handle_comment_created(payload, TEST_API_KEY, self.target)

        self.assertTrue(result["handled"])
        # Feedback resumed the investigation stage (second agent turn), not
        # an approve-and-advance.
        self.assertEqual(calls["n"], 2)
        answer_path = store.artifact_path(run_id, "answers/investigation.md")
        self.assertTrue(answer_path.exists())
        self.assertIn("retry path", answer_path.read_text())


class TestDocStageAttachment(unittest.TestCase):
    """A completed doc stage's artifact gets attached to the Linear issue as
    a file exactly once — not re-uploaded on every poller tick that finds
    the run still sitting at the same *_complete gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_investigation_complete_uploads_report_once(self):
        from gantry.linear import sync_issue_status

        run_id = self.eng.create_run("t", "r", tag="bug")
        store = self.eng.store
        store.record_linear_issue("issue-1", run_id)
        store.artifact_path(run_id, "investigation-report.md").write_text("# Investigation\n\nfound it.\n")
        store.update_state(run_id, status="investigation_complete", current_stage="investigation")

        graphql_calls = []
        put_calls = []
        posted_comment_bodies = []

        def fake_graphql(query, variables, api_key):
            graphql_calls.append(query)
            if "fileUpload" in query:
                return {"fileUpload": {"success": True, "uploadFile": {
                    "uploadUrl": "https://upload.example/put", "assetUrl": "https://asset.example/file.md",
                    "headers": [{"key": "X-Test", "value": "1"}]}}}
            if "commentCreate" in query:
                posted_comment_bodies.append(variables["body"])
                return {"commentCreate": {"success": True}}
            if "team(id" in query and "states" in query:
                return {"team": {"states": {"nodes": [
                    {"id": "s-blocked", "name": "Blocked", "type": "started"}]}}}
            if "team(id" in query and "labels" in query:
                return {"team": {"labels": {"nodes": []}}}
            if "issue(id" in query and "labels" in query:
                return {"issue": {"labels": {"nodes": []}}}
            if "issueLabelCreate" in query:
                return {"issueLabelCreate": {"success": True, "issueLabel": {"id": "lbl-1"}}}
            return {"issueUpdate": {"success": True}}

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=60):
            put_calls.append(req.full_url)
            return _FakeResp()

        with patch("gantry.linear._graphql", side_effect=fake_graphql), \
             patch("gantry.linear.urllib.request.urlopen", side_effect=fake_urlopen):
            sync_issue_status(run_id, store, TEST_TEAM_ID, TEST_API_KEY)
            # Second tick, same status — must NOT upload again.
            sync_issue_status(run_id, store, TEST_TEAM_ID, TEST_API_KEY)

        self.assertEqual(put_calls, ["https://upload.example/put"])
        self.assertEqual(len([q for q in graphql_calls if "fileUpload" in q]), 1)
        self.assertIn("investigation", store.state(run_id)["linear_docs_posted"])
        # Announce (missing create-comment path) + doc attachment comment.
        self.assertEqual(len(posted_comment_bodies), 2)
        self.assertTrue(any("Tracking run" in b for b in posted_comment_bodies))
        self.assertTrue(any("https://asset.example/file.md" in b for b in posted_comment_bodies))
        self.assertTrue(any("Investigation stage complete" in b for b in posted_comment_bodies))

    def test_investigation_complete_reposts_after_content_changes(self):
        """A doc stage can complete more than once for the same run — human
        sends feedback, resume, investigation_complete fires again with a
        rewritten report. That second, genuinely different report must be
        re-posted, not silently skipped as an already-seen stage name."""
        from gantry.linear import sync_issue_status

        run_id = self.eng.create_run("t", "r", tag="bug")
        store = self.eng.store
        store.record_linear_issue("issue-2", run_id)
        store.artifact_path(run_id, "investigation-report.md").write_text("# Investigation\n\nfirst pass.\n")
        store.update_state(run_id, status="investigation_complete", current_stage="investigation")

        posted_comment_bodies = []

        def fake_graphql(query, variables, api_key):
            if "fileUpload" in query:
                return {"fileUpload": {"success": True, "uploadFile": {
                    "uploadUrl": "https://upload.example/put", "assetUrl": "https://asset.example/file.md",
                    "headers": []}}}
            if "commentCreate" in query:
                posted_comment_bodies.append(variables["body"])
                return {"commentCreate": {"success": True}}
            if "team(id" in query and "states" in query:
                return {"team": {"states": {"nodes": [
                    {"id": "s-blocked", "name": "Blocked", "type": "started"}]}}}
            if "team(id" in query and "labels" in query:
                return {"team": {"labels": {"nodes": []}}}
            if "issue(id" in query and "labels" in query:
                return {"issue": {"labels": {"nodes": []}}}
            if "issueLabelCreate" in query:
                return {"issueLabelCreate": {"success": True, "issueLabel": {"id": "lbl-1"}}}
            return {"issueUpdate": {"success": True}}

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("gantry.linear._graphql", side_effect=fake_graphql), \
             patch("gantry.linear.urllib.request.urlopen", return_value=_FakeResp()):
            sync_issue_status(run_id, store, TEST_TEAM_ID, TEST_API_KEY)
            # Simulate a resume that rewrote the report with different content.
            store.artifact_path(run_id, "investigation-report.md").write_text(
                "# Investigation\n\nrewritten after feedback.\n")
            sync_issue_status(run_id, store, TEST_TEAM_ID, TEST_API_KEY)

        # Announce once + doc attachment twice (content changed).
        self.assertEqual(len(posted_comment_bodies), 3)
        self.assertEqual(sum(1 for b in posted_comment_bodies if "Tracking run" in b), 1)
        self.assertEqual(sum(1 for b in posted_comment_bodies if "Investigation stage complete" in b), 2)


class TestStageProgressComments(unittest.TestCase):
    """Per-stage start/complete Linear comments — transition-deduped."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def _sync(self, run_id, store, bodies):
        from gantry.linear import sync_issue_status

        def fake_graphql(query, variables, api_key):
            if "commentCreate" in query:
                bodies.append(variables["body"])
                return {"commentCreate": {"success": True}}
            if "team(id" in query and "states" in query:
                return {"team": {"states": {"nodes": [
                    {"id": "s-ip", "name": "In Progress", "type": "started"}]}}}
            if "team(id" in query and "labels" in query:
                return {"team": {"labels": {"nodes": []}}}
            if "issue(id" in query and "labels" in query:
                return {"issue": {"labels": {"nodes": []}}}
            if "issueLabelCreate" in query:
                return {"issueLabelCreate": {"success": True, "issueLabel": {"id": "lbl-1"}}}
            return {"issueUpdate": {"success": True}}

        with patch("gantry.linear._graphql", side_effect=fake_graphql):
            sync_issue_status(run_id, store, TEST_TEAM_ID, TEST_API_KEY)

    def test_posts_start_and_complete_once_per_transition(self):
        run_id = self.eng.create_run("cleanup", "remove apps", tag="chore")
        store = self.eng.store
        store.record_linear_issue("issue-progress", run_id)
        bodies: list[str] = []

        store.update_state(run_id, status="plan_running", current_stage="plan")
        self._sync(run_id, store, bodies)
        self._sync(run_id, store, bodies)  # same status — no spam
        store.update_state(run_id, status="plan_complete", current_stage="plan")
        self._sync(run_id, store, bodies)
        store.update_state(run_id, status="build_running", current_stage="build")
        self._sync(run_id, store, bodies)

        starts = [b for b in bodies if "Starting **plan**" in b]
        completes = [b for b in bodies if "**Plan** stage complete" in b]
        builds = [b for b in bodies if "Starting **build**" in b]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(completes), 1)
        self.assertEqual(len(builds), 1)
        self.assertTrue(any("Tracking run" in b for b in bodies))

    def test_reentering_build_running_posts_again(self):
        run_id = self.eng.create_run("cleanup", "remove apps", tag="chore")
        store = self.eng.store
        store.record_linear_issue("issue-reenter", run_id)
        bodies: list[str] = []

        store.update_state(run_id, status="build_running", current_stage="build")
        self._sync(run_id, store, bodies)
        store.update_state(run_id, status="review_changes_requested", current_stage="review")
        self._sync(run_id, store, bodies)
        store.update_state(run_id, status="build_running", current_stage="build")
        self._sync(run_id, store, bodies)

        builds = [b for b in bodies if "Starting **build**" in b]
        self.assertEqual(len(builds), 2)


class TestClassifyTicketRunner(unittest.TestCase):
    """Classifier must honor the project's configured runner (not hardcode claude)."""

    def test_uses_explicit_runner_name(self):
        captured = {}

        class _Runner:
            name = "codex-cli"

            def run(self, **kwargs):
                captured.update(kwargs)
                return _agent_result("bug")

        with patch("gantry.runners.get_runner", return_value=_Runner()) as mock_get:
            tag = classify_ticket("broken login", "tap does nothing", runner="codex-cli")
        self.assertEqual(tag, "bug")
        mock_get.assert_called_once_with("codex-cli")
        self.assertEqual(captured.get("max_turns"), 1)

    def test_resolves_runner_from_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gantry.toml").write_text('[agent]\nrunner = "codex-cli"\n')
            captured = {}

            class _Runner:
                name = "codex-cli"

                def run(self, **kwargs):
                    captured["cwd"] = kwargs.get("cwd")
                    return _agent_result("chore")

            with patch("gantry.runners.get_runner", return_value=_Runner()) as mock_get:
                tag = classify_ticket("bump deps", "routine", project_root=root)
            self.assertEqual(tag, "chore")
            mock_get.assert_called_once_with("codex-cli")
            self.assertEqual(captured["cwd"], root)


class TestStatusToCategoryAutonomy(unittest.TestCase):
    def test_retry_pending_stays_in_progress(self):
        from gantry.linear import status_to_category

        for status in (
            "checks_failed", "e2e_failed", "build_failed",
            "review_changes_requested", "ship_failed",
        ):
            with self.subTest(status=status):
                self.assertEqual(status_to_category(status), "in_progress")

    def test_agent_complete_stays_in_progress(self):
        from gantry.linear import status_to_category

        for status in ("plan_complete", "build_complete", "evidence_complete"):
            with self.subTest(status=status):
                self.assertEqual(status_to_category(status), "in_progress")

    def test_human_gates_are_blocked(self):
        from gantry.linear import status_to_category

        for status in (
            "checks_escalated", "review_escalated", "resolve_escalated",
            "checks_high_risk_escalated", "ship_checks_failed",
            "blocked", "held", "spec_complete", "build_question",
        ):
            with self.subTest(status=status):
                self.assertEqual(status_to_category(status), "blocked")

    def test_exhausted_stage_retries_block(self):
        from gantry.linear import status_to_category

        self.assertEqual(
            status_to_category(
                "build_failed",
                {"build_retry_count": 2, "stage_retry_max": 2},
            ),
            "blocked",
        )
        self.assertEqual(
            status_to_category(
                "build_failed",
                {"build_retry_count": 1, "stage_retry_max": 2},
            ),
            "in_progress",
        )


if __name__ == "__main__":
    unittest.main()
