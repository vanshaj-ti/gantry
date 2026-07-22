import unittest

from gantry.retry import RetryPolicy


class TestRetryPolicy(unittest.TestCase):
    def test_not_exhausted_below_cap(self):
        policy = RetryPolicy(max_attempts=3)
        self.assertFalse(policy.exhausted(0))
        self.assertFalse(policy.exhausted(2))

    def test_exhausted_at_exact_limit(self):
        policy = RetryPolicy(max_attempts=3)
        self.assertTrue(policy.exhausted(3))

    def test_exhausted_past_limit(self):
        policy = RetryPolicy(max_attempts=3)
        self.assertTrue(policy.exhausted(5))

    def test_attempts_remaining(self):
        policy = RetryPolicy(max_attempts=3)
        self.assertEqual(policy.attempts_remaining(0), 3)
        self.assertEqual(policy.attempts_remaining(2), 1)
        self.assertEqual(policy.attempts_remaining(3), 0)

    def test_attempts_remaining_never_negative(self):
        policy = RetryPolicy(max_attempts=2)
        self.assertEqual(policy.attempts_remaining(10), 0)

    def test_zero_max_attempts_always_exhausted(self):
        policy = RetryPolicy(max_attempts=0)
        self.assertTrue(policy.exhausted(0))


if __name__ == "__main__":
    unittest.main()
