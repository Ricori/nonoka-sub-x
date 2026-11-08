from __future__ import annotations

from nonoka_x import cloud_route
from nonoka_x.cloud_route import RouteState

PRIMARY = "cloud.nonoka.online"
BACKUP = "cloud-jp.nonoka.online"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_primary_is_used_until_it_fails():
    assert not RouteState(clock=Clock()).prefer_backup(PRIMARY)


def test_the_backup_is_kept_until_the_stickiness_lapses():
    clock = Clock()
    state = RouteState(clock=clock)
    state.failed(PRIMARY, PRIMARY)

    assert state.prefer_backup(PRIMARY)
    clock.now += cloud_route.STICKINESS_SECONDS + 1
    assert not state.prefer_backup(PRIMARY)


def test_a_failing_backup_hands_the_lookups_back_to_the_primary():
    state = RouteState(clock=Clock())
    state.failed(PRIMARY, PRIMARY)
    state.failed(PRIMARY, BACKUP)

    assert not state.prefer_backup(PRIMARY)
