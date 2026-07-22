import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gantry import daemon
from gantry.state import now_iso


class TestDaemonHeartbeatStatus(unittest.TestCase):
    """daemon_heartbeat_status compares the last-completed-tick timestamp
    against interval_seconds * _HEARTBEAT_STALE_MULTIPLIER. Tests write the
    last-tick file directly with fabricated timestamps rather than sleeping,
    so the stale/fresh boundary is exercised instantly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._config_dir_patch = mock.patch.object(daemon, "_CONFIG_DIR", Path(self._tmp.name))
        self._last_tick_patch = mock.patch.object(
            daemon, "_LAST_TICK_FILE", Path(self._tmp.name) / "daemon-last-tick.json")
        self._config_dir_patch.start()
        self._last_tick_patch.start()

    def tearDown(self):
        self._config_dir_patch.stop()
        self._last_tick_patch.stop()
        self._tmp.cleanup()

    def _write_last_tick(self, age_seconds: float) -> None:
        import time
        from datetime import datetime, timezone
        ts = time.time() - age_seconds
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        daemon._LAST_TICK_FILE.write_text(json.dumps({"completed_at": iso}))

    def test_never_ticked_is_not_stale(self):
        result = daemon.daemon_heartbeat_status(interval_seconds=60)
        self.assertFalse(result["stale"])
        self.assertIsNone(result["last_tick_at"])
        self.assertIsNone(result["age_seconds"])

    def test_fresh_tick_is_not_stale(self):
        self._write_last_tick(age_seconds=10)
        result = daemon.daemon_heartbeat_status(interval_seconds=60)
        self.assertFalse(result["stale"])
        self.assertIsNotNone(result["last_tick_at"])

    def test_tick_just_under_grace_is_not_stale(self):
        grace = 60 * daemon._HEARTBEAT_STALE_MULTIPLIER
        self._write_last_tick(age_seconds=grace - 5)
        result = daemon.daemon_heartbeat_status(interval_seconds=60)
        self.assertFalse(result["stale"])

    def test_tick_past_grace_is_stale(self):
        grace = 60 * daemon._HEARTBEAT_STALE_MULTIPLIER
        self._write_last_tick(age_seconds=grace + 30)
        result = daemon.daemon_heartbeat_status(interval_seconds=60)
        self.assertTrue(result["stale"])
        self.assertGreater(result["age_seconds"], grace)

    def test_corrupt_file_treated_as_never_ticked(self):
        daemon._LAST_TICK_FILE.write_text("not json")
        result = daemon.daemon_heartbeat_status(interval_seconds=60)
        self.assertFalse(result["stale"])
        self.assertIsNone(result["last_tick_at"])

    def test_record_tick_completed_makes_status_fresh(self):
        daemon._record_tick_completed()
        result = daemon.daemon_heartbeat_status(interval_seconds=60)
        self.assertFalse(result["stale"])
        self.assertIsNotNone(result["last_tick_at"])


class TestAdvanceTargetWithTimeout(unittest.TestCase):
    """The per-target timeout wrapper must catch a hanging target and still
    let the tick loop move on — verified here directly against
    _advance_target_with_timeout with a fake slow function standing in for
    advance_all, so the test doesn't actually wait out a real timeout."""

    def test_fast_target_returns_normally(self):
        fake_result = {"target": "/x", "ok": True, "advanced": 2}
        with mock.patch.object(daemon, "_advance_target", return_value=fake_result):
            result = daemon._advance_target_with_timeout(Path("/x"), timeout_seconds=5)
        self.assertEqual(result, fake_result)

    def test_hanging_target_times_out_with_error_shape(self):
        import time as time_mod

        def _hang(target):
            time_mod.sleep(2)  # longer than the tiny timeout below
            return {"target": str(target), "ok": True, "advanced": 0}

        with mock.patch.object(daemon, "_advance_target", side_effect=_hang):
            result = daemon._advance_target_with_timeout(Path("/slow"), timeout_seconds=0.05)
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["error"])
        self.assertEqual(result["target"], "/slow")

    def test_target_raising_is_reported_not_raised(self):
        with mock.patch.object(daemon, "_advance_target", side_effect=RuntimeError("boom")):
            result = daemon._advance_target_with_timeout(Path("/bad"), timeout_seconds=5)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])


class TestRunTickPerTargetTimeout(unittest.TestCase):
    """A hanging target inside run_tick's own loop must not prevent later
    targets in the same tick from being processed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._config_dir_patch = mock.patch.object(daemon, "_CONFIG_DIR", base)
        self._targets_patch = mock.patch.object(daemon, "_TARGETS_FILE", base / "targets.json")
        self._lock_patch = mock.patch.object(daemon, "_TICK_LOCK", base / "tick.lock")
        self._last_tick_patch = mock.patch.object(daemon, "_LAST_TICK_FILE", base / "last-tick.json")
        self._config_dir_patch.start()
        self._targets_patch.start()
        self._lock_patch.start()
        self._last_tick_patch.start()
        self.target_a = base / "repo-a"
        self.target_b = base / "repo-b"
        daemon.add_target(self.target_a)
        daemon.add_target(self.target_b)

    def tearDown(self):
        self._config_dir_patch.stop()
        self._targets_patch.stop()
        self._lock_patch.stop()
        self._last_tick_patch.stop()
        self._tmp.cleanup()

    def test_hanging_target_does_not_block_later_target(self):
        import time as time_mod

        from gantry.config import GantryConfig
        cfg = GantryConfig()
        cfg.daemon.per_target_timeout_seconds = 0.05

        def fake_load_config(target):
            return cfg

        def fake_advance_target(target):
            if target == self.target_a.resolve():
                time_mod.sleep(2)
            return {"target": str(target), "ok": True, "advanced": 0}

        with mock.patch("gantry.config.load_config", fake_load_config), \
             mock.patch.object(daemon, "_advance_target", side_effect=fake_advance_target):
            results = daemon.run_tick(interval_seconds=60)

        self.assertEqual(len(results), 2)
        result_a = next(r for r in results if str(self.target_a.resolve()) == r["target"])
        result_b = next(r for r in results if str(self.target_b.resolve()) == r["target"])
        self.assertFalse(result_a["ok"])
        self.assertIn("timeout", result_a["error"])
        self.assertTrue(result_b["ok"])


class TestTickLock(unittest.TestCase):
    """_tick_lock_acquire/_tick_lock_release now use a real OS-level advisory
    lock (fcntl.flock) instead of a PID-liveness/staleness heuristic. Verify
    the actual kernel-enforced mutual exclusion, not a simulation of it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._config_dir_patch = mock.patch.object(daemon, "_CONFIG_DIR", base)
        self._lock_patch = mock.patch.object(daemon, "_TICK_LOCK", base / "tick.lock")
        self._config_dir_patch.start()
        self._lock_patch.start()

    def tearDown(self):
        self._config_dir_patch.stop()
        self._lock_patch.stop()
        self._tmp.cleanup()

    def test_first_acquire_succeeds(self):
        lock_fh = daemon._tick_lock_acquire()
        self.assertIsNotNone(lock_fh)
        daemon._tick_lock_release(lock_fh)

    def test_second_acquire_fails_while_first_still_held(self):
        """Two real, concurrent attempts to acquire the tick lock: the first
        succeeds and holds an open fd on the lock file; the second — a
        genuinely separate file descriptor on the same file, opened while the
        first is still open — must be rejected by the kernel, not by any
        Python-level bookkeeping."""
        first = daemon._tick_lock_acquire()
        self.assertIsNotNone(first)
        try:
            second = daemon._tick_lock_acquire()
            self.assertIsNone(second, "a second concurrent acquire must fail "
                                       "while the first lock is still held")
        finally:
            daemon._tick_lock_release(first)

    def test_acquire_succeeds_again_after_release(self):
        first = daemon._tick_lock_acquire()
        daemon._tick_lock_release(first)
        second = daemon._tick_lock_acquire()
        self.assertIsNotNone(second, "lock must be re-acquirable once released")
        daemon._tick_lock_release(second)

    def test_lock_released_even_when_holder_process_is_killed(self):
        """The whole point of a real flock over a PID-staleness heuristic:
        the kernel releases the lock on ANY death of the holding process,
        including SIGKILL — no staleness window, no liveness probe needed."""
        import os
        import signal
        import time as time_mod

        lock_path = daemon._TICK_LOCK
        pid = os.fork()
        if pid == 0:  # child: acquire the lock and then hang until killed
            fh = open(lock_path, "w")
            fcntl_mod = __import__("fcntl")
            fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
            time_mod.sleep(30)
            os._exit(0)

        try:
            # Give the child a moment to acquire the lock.
            for _ in range(50):
                time_mod.sleep(0.02)
                probe = daemon._tick_lock_acquire()
                if probe is None:
                    break
                daemon._tick_lock_release(probe)
            else:
                self.fail("child never appeared to acquire the lock")

            # Confirm it's genuinely held right now.
            self.assertIsNone(daemon._tick_lock_acquire())

            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

            # Kernel must have released the lock the instant the process died.
            reacquired = daemon._tick_lock_acquire()
            self.assertIsNotNone(reacquired, "lock must be free immediately "
                                              "after the holder is SIGKILLed")
            daemon._tick_lock_release(reacquired)
        finally:
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass

    def test_run_tick_skips_when_lock_already_held(self):
        base = daemon._CONFIG_DIR
        with mock.patch.object(daemon, "_TARGETS_FILE", base / "targets.json"), \
             mock.patch.object(daemon, "_LAST_TICK_FILE", base / "last-tick.json"):
            held = daemon._tick_lock_acquire()
            self.assertIsNotNone(held)
            try:
                results = daemon.run_tick(interval_seconds=60)
            finally:
                daemon._tick_lock_release(held)
        self.assertEqual(results, [])


class TestNotifyDaemonStale(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._config_dir_patch = mock.patch.object(daemon, "_CONFIG_DIR", base)
        self._targets_patch = mock.patch.object(daemon, "_TARGETS_FILE", base / "targets.json")
        self._config_dir_patch.start()
        self._targets_patch.start()
        self.target = base / "repo"
        daemon.add_target(self.target)

    def tearDown(self):
        self._config_dir_patch.stop()
        self._targets_patch.stop()
        self._tmp.cleanup()

    def test_notifies_via_first_target_with_notify_configured(self):
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        cfg.notify.backend = "webhook"
        cfg.notify.webhook_url = "https://example.invalid/hook"

        sent = []

        class FakeNotifier:
            def send(self, text, meta=None):
                sent.append((text, meta))
                return {"sent": True}

        with mock.patch("gantry.config.load_config", return_value=cfg), \
             mock.patch("gantry.notify.get_notifier", return_value=FakeNotifier()):
            daemon._notify_daemon_stale({"stale": True, "last_tick_at": now_iso(), "age_seconds": 999})

        self.assertEqual(len(sent), 1)
        self.assertIn("999", sent[0][0])

    def test_skips_targets_with_notify_disabled(self):
        from gantry.config import GantryConfig
        cfg = GantryConfig()  # backend defaults to "none"

        with mock.patch("gantry.config.load_config", return_value=cfg), \
             mock.patch("gantry.notify.get_notifier") as get_notifier:
            daemon._notify_daemon_stale({"stale": True, "last_tick_at": None, "age_seconds": 500})
        get_notifier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
