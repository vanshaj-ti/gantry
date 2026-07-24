"""Session lineage / additive schema contract tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gantry.sessions import (
    IMPLEMENTATION_LINEAGE_ID,
    SCHEMA_VERSION,
    SessionRecord,
    can_native_resume,
    lineage_for,
    migrate_record,
    policy_for,
    resolve_resume_session_id,
    save_session_record,
)
from gantry.state import RunStore

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_sessions.json"


class TestLineageTopology(unittest.TestCase):
    def test_isolated_doc_and_investigation(self):
        for stage in ("spec", "design", "investigation", "research"):
            self.assertEqual(lineage_for(stage), "isolated")
            self.assertTrue(policy_for(stage).allow_native_resume)

    def test_shared_implementation(self):
        for stage in ("plan", "build", "resolve"):
            pol = policy_for(stage)
            self.assertEqual(pol.lineage, "shared_implementation")
            self.assertEqual(pol.shared_lineage_id, IMPLEMENTATION_LINEAGE_ID)

    def test_fresh_evidence_and_review_axes(self):
        for stage in ("evidence", "review", "review_spec", "review_standards"):
            pol = policy_for(stage)
            self.assertEqual(pol.lineage, "fresh")
            self.assertFalse(pol.allow_native_resume)


class TestLegacyMigration(unittest.TestCase):
    def test_legacy_fixture_migrates_additively(self):
        data = json.loads(FIXTURES.read_text())
        rec = migrate_record(data["plan"], stage="plan")
        self.assertEqual(rec.session_id, "sess-plan-legacy-001")
        self.assertEqual(rec.runner, "claude-code")
        self.assertEqual(rec.schema_version, SCHEMA_VERSION)
        self.assertTrue(rec.gantry_session_id.startswith("gsess-"))
        self.assertEqual(rec.lineage, "shared_implementation")
        self.assertEqual(rec.lineage_id, IMPLEMENTATION_LINEAGE_ID)

    def test_roundtrip_preserves_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create("r1", "t")
            legacy = json.loads(FIXTURES.read_text())["build"]
            store.save_session("r1", "build", **legacy)
            rec = save_session_record(store, "r1", "build", terminal_status="ok")
            loaded = store.get_session("r1", "build")
            self.assertEqual(loaded["session_id"], legacy["session_id"])
            self.assertEqual(loaded["model"], legacy["model"])
            self.assertEqual(loaded["runner"], legacy["runner"])
            self.assertIn("gantry_session_id", loaded)
            self.assertEqual(rec.terminal_status, "ok")


class TestResumeGuards(unittest.TestCase):
    def test_backend_mismatch_rejects(self):
        decision = can_native_resume(
            stage="build",
            stored={"session_id": "s1", "runner": "claude-code", "model": "opus"},
            backend="cursor-sdk",
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.fallback_to_artifacts)

    def test_worktree_mismatch_rejects(self):
        decision = can_native_resume(
            stage="plan",
            stored={"session_id": "s1", "runner": "cursor-sdk", "worktree_id": "wt-a"},
            backend="cursor-sdk",
            worktree_id="wt-b",
        )
        self.assertFalse(decision.allowed)

    def test_fresh_stages_never_resume(self):
        decision = can_native_resume(
            stage="evidence",
            stored={"session_id": "s1", "runner": "cursor-sdk"},
            backend="cursor-sdk",
        )
        self.assertFalse(decision.allowed)

    def test_shared_lineage_resolves_peer_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create("r1", "t")
            store.save_session("r1", "plan", session_id="plan-sess", runner="cursor-sdk", model="m")
            decision = resolve_resume_session_id(
                store, "r1", "build", backend="cursor-sdk", model="m",
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.session_id, "plan-sess")


class TestSessionRecordDict(unittest.TestCase):
    def test_from_dict_keeps_unknown_extra(self):
        rec = SessionRecord.from_dict({"session_id": "x", "custom_flag": True})
        self.assertEqual(rec.session_id, "x")
        self.assertEqual(rec.extra.get("custom_flag"), True)


if __name__ == "__main__":
    unittest.main()
