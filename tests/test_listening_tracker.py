"""Unit tests for ListeningTracker (start capture, re-listening, seek, retry queue)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from fabel.listening import ListeningSession, ListeningTracker

ISBN = "9788203454332"
BOOK_DURATION = 28_835


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _RecordingUploader:
    def __init__(self) -> None:
        self.batches: list[list[ListeningSession]] = []
        self.fail_next: int = 0

    @property
    def all_sessions(self) -> list[ListeningSession]:
        return [item for batch in self.batches for item in batch]

    @property
    def total_seconds(self) -> int:
        return sum(item.seconds for item in self.all_sessions)

    async def __call__(self, sessions: Sequence[ListeningSession]) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            msg = "temporary Fabel outage"
            raise RuntimeError(msg)
        self.batches.append(list(sessions))


@pytest.mark.asyncio
async def test_first_stretch_from_zero_is_counted() -> None:
    """Playback before MA's first 30s on_played report must be counted."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=0, book_duration=BOOK_DURATION)
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=30, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=60, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )
    clock.advance(15)
    await tracker.on_played(
        isbn=ISBN, position=75, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    assert uploader.total_seconds == 75
    assert [(s.start_position, s.end_position) for s in uploader.all_sessions] == [
        (0, 30),
        (30, 60),
        (60, 75),
    ]
    assert tracker.pending_count == 0


@pytest.mark.asyncio
async def test_short_session_under_30s_is_counted() -> None:
    """Stopping before the first 30s tick must still count the listened stretch."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=100, book_duration=BOOK_DURATION)
    clock.advance(20)
    await tracker.on_played(
        isbn=ISBN, position=120, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    assert uploader.total_seconds == 20
    assert [(s.start_position, s.end_position) for s in uploader.all_sessions] == [(100, 120)]


@pytest.mark.asyncio
async def test_relisten_after_backward_seek_without_new_stream_is_counted() -> None:
    """
    OzGav's exact scenario: listen 100→200s, seek back to 50s, listen to 150s.

    Even when MA does not re-fire on_stream_started on seek, total counted must be 200s.
    """
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=100, book_duration=BOOK_DURATION)
    for pos in (130, 160, 190):
        clock.advance(30)
        await tracker.on_played(
            isbn=ISBN,
            position=pos,
            book_duration=BOOK_DURATION,
            fully_played=False,
            is_playing=True,
        )
    clock.advance(10)
    await tracker.on_played(
        isbn=ISBN, position=200, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )

    # User seeks back to 50s and listens for 100s (to 150s) across 30s ticks + final stop.
    for pos in (80, 110, 140):
        clock.advance(30)
        await tracker.on_played(
            isbn=ISBN,
            position=pos,
            book_duration=BOOK_DURATION,
            fully_played=False,
            is_playing=True,
        )
    clock.advance(10)
    await tracker.on_played(
        isbn=ISBN, position=150, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    assert uploader.total_seconds == 200
    assert tracker.pending_count == 0


@pytest.mark.asyncio
async def test_relisten_after_backward_seek_with_new_stream_is_counted() -> None:
    """Re-listening when a seek also fires on_stream_started must count all 200s."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=100, book_duration=BOOK_DURATION)
    clock.advance(100)
    await tracker.on_played(
        isbn=ISBN, position=200, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    clock.advance(5)
    await tracker.on_stream_started(isbn=ISBN, start_position=50, book_duration=BOOK_DURATION)
    clock.advance(100)
    await tracker.on_played(
        isbn=ISBN, position=150, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    assert uploader.total_seconds == 200
    assert [(s.start_position, s.end_position) for s in uploader.all_sessions] == [
        (100, 200),
        (50, 150),
    ]


@pytest.mark.asyncio
async def test_forward_seek_does_not_overreport_skipped_audio() -> None:
    """Jumping ahead by an hour in 10s must not burn an hour of allowance."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=100, book_duration=BOOK_DURATION)
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=130, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )
    # Jump from 130s to 5000s over 10s of wall time.
    clock.advance(10)
    await tracker.on_played(
        isbn=ISBN, position=5000, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )

    assert uploader.total_seconds == 40
    assert uploader.all_sessions[-1].start_position == 4990
    assert uploader.all_sessions[-1].end_position == 5000


@pytest.mark.asyncio
async def test_failed_uploads_are_queued_and_retried_in_batch() -> None:
    """Failed POSTs must keep intervals queued and deliver all of them once API recovers."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    uploader.fail_next = 3
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=0, book_duration=BOOK_DURATION)
    # First 3 reports fail (including the final pause/stop report!).
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=30, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=60, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )
    clock.advance(25)
    await tracker.on_played(
        isbn=ISBN, position=85, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    assert uploader.total_seconds == 0
    assert tracker.pending_count == 3

    # Next flush (e.g. unload or next playback) succeeds and delivers all 3 intervals in one batch.
    assert await tracker.flush() is True
    assert tracker.pending_count == 0
    assert len(uploader.batches) == 1
    assert uploader.total_seconds == 85
    assert [(s.start_position, s.end_position) for s in uploader.all_sessions] == [
        (0, 30),
        (30, 60),
        (60, 85),
    ]


@pytest.mark.asyncio
async def test_pause_for_hours_and_resume_keeps_realistic_timestamps() -> None:
    """Resuming after a long pause must not stretch started_at back to pause time."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_stream_started(isbn=ISBN, start_position=500, book_duration=BOOK_DURATION)
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=530, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    # Paused for 2 hours, then resumes without a new stream and plays 30s.
    clock.advance(7200)
    await tracker.on_played(
        isbn=ISBN, position=560, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )

    assert uploader.total_seconds == 60
    resumed = uploader.all_sessions[-1]
    assert (resumed.start_position, resumed.end_position) == (530, 560)
    assert int((resumed.ended_at - resumed.started_at).total_seconds()) == 30


@pytest.mark.asyncio
async def test_first_report_without_on_stream_started_counts_from_zero() -> None:
    """Fallback when on_played fires at ~30s without prior on_stream_started."""
    clock = _FakeClock()
    uploader = _RecordingUploader()
    tracker = ListeningTracker(uploader, clock=clock)

    await tracker.on_played(
        isbn=ISBN, position=30, book_duration=BOOK_DURATION, fully_played=False, is_playing=True
    )
    clock.advance(30)
    await tracker.on_played(
        isbn=ISBN, position=60, book_duration=BOOK_DURATION, fully_played=False, is_playing=False
    )

    assert uploader.total_seconds == 60
    assert [(s.start_position, s.end_position) for s in uploader.all_sessions] == [
        (0, 30),
        (30, 60),
    ]
