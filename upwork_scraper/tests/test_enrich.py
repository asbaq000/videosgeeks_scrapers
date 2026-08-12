import random
from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.enrich import ClientEnricher, CircuitBreaker, HumanDelay
from upwork_scraper.enrich.client_fetcher import parse_client_info
from upwork_scraper.models.job_models import Job


def _job(cipher="~c1") -> Job:
    return Job.model_validate(
        {
            "title": "Video Editor",
            "description": "d",
            "ontologySkills": None,
            "jobTile": {
                "job": {
                    "ciphertext": cipher,
                    "jobType": "HOURLY",
                    "publishTime": 1700000000000,
                    "hourlyBudgetMin": None,
                    "hourlyBudgetMax": None,
                    "contractorTier": None,
                    "hourlyEngagementDuration": None,
                    "fixedPriceAmount": None,
                    "fixedPriceEngagementDuration": None,
                }
            },
        }
    )


class FakeSleep:
    """Records sleeps instead of performing them."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds):
        self.calls.append(seconds)

    @property
    def total(self):
        return sum(self.calls)


class TestHumanDelay:

    def test_first_request_is_immediate(self):
        sleep = FakeSleep()
        HumanDelay(sleeper=sleep).wait()

        assert sleep.calls == []

    def test_delays_fall_in_the_configured_range(self):
        sleep = FakeSleep()
        delay = HumanDelay(2, 5, long_pause_every=0, sleeper=sleep)

        for _ in range(30):
            delay.wait()

        assert len(sleep.calls) == 29
        assert all(2 <= s <= 5 for s in sleep.calls)

    def test_delays_are_not_constant(self):
        """A fixed interval is the easiest bot signature to spot."""
        sleep = FakeSleep()
        delay = HumanDelay(2, 9, long_pause_every=0, sleeper=sleep)

        for _ in range(25):
            delay.wait()

        assert len(set(sleep.calls)) > 15

    def test_takes_a_long_pause_periodically(self):
        sleep = FakeSleep()
        delay = HumanDelay(
            1, 2, long_pause_every=5, long_pause_seconds=(30, 40), sleeper=sleep
        )

        for _ in range(10):
            delay.wait()

        long_pauses = [s for s in sleep.calls if s >= 30]
        assert len(long_pauses) == 2

    def test_rejects_a_bad_range(self):
        with pytest.raises(ValueError):
            HumanDelay(10, 2)

    def test_is_reproducible_with_a_seeded_rng(self):
        def run():
            sleep = FakeSleep()
            delay = HumanDelay(1, 9, sleeper=sleep, rng=random.Random(42))
            for _ in range(5):
                delay.wait()
            return sleep.calls

        assert run() == run()


class TestCircuitBreaker:

    def test_starts_closed(self):
        assert CircuitBreaker().is_open is False

    def test_opens_after_consecutive_failures(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=3, sleeper=sleep)

        for _ in range(3):
            breaker.record_failure()

        assert breaker.is_open is True

    def test_success_resets_the_streak(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=3, sleeper=sleep)

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()

        assert breaker.is_open is False
        assert breaker.consecutive_failures == 1

    def test_backoff_grows_with_each_failure(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=5, base_backoff=10, sleeper=sleep)

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        assert sleep.calls == [10, 20, 40]

    def test_backoff_is_capped(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(
            trip_after=99, base_backoff=10, max_backoff=45, sleeper=sleep
        )

        for _ in range(8):
            breaker.record_failure()

        assert max(sleep.calls) == 45

    def test_no_backoff_once_tripped(self):
        """Once open the caller stops, so there is nothing to wait for."""
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=2, base_backoff=10, sleeper=sleep)

        breaker.record_failure()
        breaker.record_failure()

        assert len(sleep.calls) == 1
