import pytest

from gorets.digest.claude import BatchStatus, ClaudeError, extract_json, wait_for_batch


class StatusGateway:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.polls = 0

    def batch_status(self, batch_id: str) -> BatchStatus:
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        return BatchStatus(id=batch_id, status=status, counts={"processing": 1})


def test_wait_until_ended_with_fake_clock() -> None:
    gateway = StatusGateway(["in_progress", "in_progress", "ended"])
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    status = wait_for_batch(
        gateway,
        "b1",
        max_wait_seconds=3600,
        poll_seconds=120,
        sleep=sleep,
        clock=lambda: clock["t"],
    )
    assert status is not None and status.ended
    assert gateway.polls == 3
    assert slept == [120, 120]


def test_wait_times_out_without_hanging() -> None:
    gateway = StatusGateway(["in_progress"])
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    status = wait_for_batch(
        gateway, "b1", max_wait_seconds=300, poll_seconds=120, sleep=sleep, clock=lambda: clock["t"]
    )
    assert status is None
    assert clock["t"] == 300  # последний сон укорочен до остатка


def test_extract_json_tolerates_fences_and_prose() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Вот ответ:\n{"a": {"b": [1, 2]}}\nготово') == {"a": {"b": [1, 2]}}
    with pytest.raises(ClaudeError):
        extract_json("ничего")
    with pytest.raises(ClaudeError):
        extract_json("[1, 2]")
