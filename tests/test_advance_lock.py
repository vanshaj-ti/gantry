import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from gantry.advance import _acquire_lock, _lock_path, _pid_alive, _release_lock
from gantry.config import GantryConfig
from gantry.engine import Engine


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestPidAlive(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(_pid_alive(os.getpid()))

    def test_a_pid_that_cannot_exist_is_not_alive(self):
        # PID 1 always exists on POSIX (init/launchd); use a made-up huge PID
        # that's astronomically unlikely to be a real running process.
        self.assertFalse(_pid_alive(999999999))


class TestAcquireLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "test")

    def tearDown(self):
        self._tmp.cleanup()

    def test_acquires_when_no_lock_exists(self):
        self.assertTrue(_acquire_lock(self.eng, self.run_id))
        self.assertTrue(_lock_path(self.eng, self.run_id).exists())

    def test_second_acquire_fails_while_holder_pid_is_alive(self):
        # A real second process, alive but distinct from the test's own PID —
        # writing os.getpid() here would be indistinguishable from "it's my
        # own lock" (see test_own_pid_in_lock_does_not_block_own_reacquire),
        # which is a different, legitimately-allowed case.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            lock = _lock_path(self.eng, self.run_id)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(proc.pid))
            self.assertFalse(_acquire_lock(self.eng, self.run_id))
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_reclaims_immediately_when_holder_pid_is_dead_even_if_lock_is_fresh(self):
        """Regression test for a real incident: a lock from an interrupted
        manual `gantry advance` call sat for 13 minutes — under the 30-minute
        stale_after threshold — silently blocking `gantry loop`'s passive
        advance --all from ever touching that run again, even though the
        process that wrote the lock was long dead. The reclaim must key off
        actual process liveness, not just lock-file age."""
        lock = _lock_path(self.eng, self.run_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999999")  # a PID that cannot exist
        # Lock is fresh (just written), well under any stale_after — must
        # still reclaim because the holder PID is dead.
        self.assertTrue(_acquire_lock(self.eng, self.run_id, stale_after=1800))

    def test_falls_back_to_time_based_staleness_when_pid_content_is_unreadable(self):
        lock = _lock_path(self.eng, self.run_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-a-pid")
        old_time = time.time() - 2000
        os.utime(lock, (old_time, old_time))
        self.assertTrue(_acquire_lock(self.eng, self.run_id, stale_after=1800))

    def test_does_not_reclaim_unreadable_lock_content_if_still_fresh(self):
        lock = _lock_path(self.eng, self.run_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-a-pid")
        self.assertFalse(_acquire_lock(self.eng, self.run_id, stale_after=1800))

    def test_release_removes_the_lock(self):
        _acquire_lock(self.eng, self.run_id)
        _release_lock(self.eng, self.run_id)
        self.assertFalse(_lock_path(self.eng, self.run_id).exists())

    def test_own_pid_in_lock_does_not_block_own_reacquire(self):
        """A process re-acquiring its own held lock (e.g. sequential calls
        within the same process) should not be blocked by its own PID."""
        lock = _lock_path(self.eng, self.run_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
        self.assertTrue(_acquire_lock(self.eng, self.run_id))


if __name__ == "__main__":
    unittest.main()
