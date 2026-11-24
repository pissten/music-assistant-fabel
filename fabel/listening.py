"""Reliable listening-session tracking and retry queue for Fabel allowance reporting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

LOGGER = logging.getLogger("music_assistant.fabel.listening")

# Music Assistant fires periodic on_played reports about every 30s.
DEFAULT_REPORT_INTERVAL_SECONDS = 30
# Allow up to 3x audiobook playback speed plus jitter before treating a jump as a forward seek.
MAX_SPEED_MULTIPLIER = 3.0
FORWARD_SEEK_JITTER_SECONDS = 15.0
MAX_FIRST_TICK_FROM_ZERO_SECONDS = 35
MAX_RESUME_DELTA_SECONDS = DEFAULT_REPORT_INTERVAL_SECONDS * 3 + 15
# Cap pending sessions so a prolonged outage cannot grow memory unboundedly.
MAX_PENDING_SESSIONS = 500


@dataclass(frozen=True)
class ListeningSession:
    """A completed playback interval ready for `POST /listening-sessions/batch`."""

    isbn: str
    start_position: int
    end_position: int
    started_at: datetime
    ended_at: datetime
    book_duration: int

    @property
    def seconds(self) -> int:
        """Duration of this interval in whole seconds."""
        return max(0, self.end_position - self.start_position)


@dataclass
class _ActivePlayback:
    """In-flight playback state for one audiobook ISBN."""

    isbn: str
    anchor_position: int
    anchor_at: datetime
    book_duration: int
    is_active: bool = True


SessionUploader = Callable[[Sequence[ListeningSession]], Awaitable[None]]
ClockFn = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ListeningTracker:
    """
    Track audiobook playback intervals and reliably flush them to Fabel.

    Solves three edge cases in naive `on_played`-only reporting:
    - Anchors each playback at `on_stream_started` (`seek_position`) so the first stretch
      before MA's first 30 s `on_played` report is counted.
    - Tracks forward progress and backward seeks independently so re-listened audio
      (e.g. 100→200 s, seek back to 50 s, listen to 150 s) is fully counted, while large
      forward seeks do not over-report skipped audio.
    - Queues completed intervals and only removes them after `POST /listening-sessions/batch`
      succeeds, retrying automatically on subsequent events or provider unload.
    """

    def __init__(
        self,
        uploader: SessionUploader,
        *,
        clock: ClockFn = _utc_now,
        max_pending: int = MAX_PENDING_SESSIONS,
    ) -> None:
        """Initialize the tracker with an async batch uploader."""
        self._uploader = uploader
        self._clock = clock
        self._max_pending = max_pending
        self._active: dict[str, _ActivePlayback] = {}
        self._pending: list[ListeningSession] = []
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        """Number of completed sessions still waiting to be uploaded."""
        return len(self._pending)

    async def on_stream_started(
        self,
        *,
        isbn: str,
        start_position: int,
        book_duration: int,
    ) -> None:
        """Record the exact start position when audio first reaches a player."""
        if not isbn:
            return
        now = self._clock()
        pos = max(0, int(start_position))
        duration = max(0, int(book_duration))
        if duration > 0:
            pos = min(pos, duration)

        async with self._lock:
            self._active[isbn] = _ActivePlayback(
                isbn=isbn,
                anchor_position=pos,
                anchor_at=now,
                book_duration=duration,
                is_active=True,
            )
            await self._flush_locked()

    async def on_played(
        self,
        *,
        isbn: str,
        position: int,
        book_duration: int,
        fully_played: bool,
        is_playing: bool,
    ) -> None:
        """Record progress from MA's `on_played` callback and flush pending sessions."""
        if not isbn or book_duration <= 0:
            return

        now = self._clock()
        duration = int(book_duration)
        pos = max(0, min(int(position), duration))
        if fully_played:
            pos = duration

        async with self._lock:
            self._record_progress_locked(
                isbn=isbn,
                pos=pos,
                duration=duration,
                fully_played=fully_played,
                is_playing=is_playing,
                now=now,
            )
            await self._flush_locked()

    async def flush(self) -> bool:
        """Attempt to upload all queued sessions. Returns True when queue is empty."""
        async with self._lock:
            return await self._flush_locked()

    def _record_progress_locked(  # noqa: C901, PLR0912, PLR0913
        self,
        *,
        isbn: str,
        pos: int,
        duration: int,
        fully_played: bool,
        is_playing: bool,
        now: datetime,
    ) -> None:
        state = self._active.get(isbn)

        if state is None:
            # No prior on_stream_started (e.g. reload mid-playback or direct test).
            # If the first report lands within the initial report window from 0, count 0→pos.
            if 0 < pos <= MAX_FIRST_TICK_FROM_ZERO_SECONDS:
                self._enqueue_locked(
                    ListeningSession(
                        isbn=isbn,
                        start_position=0,
                        end_position=pos,
                        started_at=now - timedelta(seconds=pos),
                        ended_at=now,
                        book_duration=duration,
                    )
                )
            if is_playing and not fully_played:
                self._active[isbn] = _ActivePlayback(
                    isbn=isbn,
                    anchor_position=pos,
                    anchor_at=now,
                    book_duration=duration,
                    is_active=True,
                )
            return

        if duration > 0:
            state.book_duration = duration
        effective_duration = state.book_duration or duration

        elapsed = max(0.0, (now - state.anchor_at).total_seconds())
        was_active = state.is_active

        if pos > state.anchor_position:
            delta = pos - state.anchor_position
            if was_active:
                max_plausible = max(
                    elapsed * MAX_SPEED_MULTIPLIER + FORWARD_SEEK_JITTER_SECONDS,
                    float(DEFAULT_REPORT_INTERVAL_SECONDS + FORWARD_SEEK_JITTER_SECONDS),
                )
                if delta <= max_plausible:
                    start_pos = state.anchor_position
                    started_at = state.anchor_at
                else:
                    # Forward seek jump: only credit plausible listening time, never the skipped gap.
                    credit = min(round(elapsed), pos)
                    start_pos = max(0, pos - credit)
                    started_at = now - timedelta(seconds=pos - start_pos)
            elif delta <= MAX_RESUME_DELTA_SECONDS:
                # Resumed from pause without a new stream; keep started_at realistic.
                start_pos = state.anchor_position
                started_at = now - timedelta(seconds=delta)
            else:
                # Seeked forward while paused before resuming.
                credit = min(DEFAULT_REPORT_INTERVAL_SECONDS, pos)
                start_pos = max(0, pos - credit)
                started_at = now - timedelta(seconds=pos - start_pos)

            if pos > start_pos:
                self._enqueue_locked(
                    ListeningSession(
                        isbn=isbn,
                        start_position=start_pos,
                        end_position=pos,
                        started_at=min(started_at, now),
                        ended_at=now,
                        book_duration=effective_duration,
                    )
                )
        elif pos < state.anchor_position:
            # Backward seek / re-listening: credit audio listened since last report up to `pos`.
            if was_active:
                credit = min(round(elapsed), pos)
            else:
                credit = min(DEFAULT_REPORT_INTERVAL_SECONDS, pos) if is_playing else 0
            start_pos = max(0, pos - credit)
            if pos > start_pos:
                self._enqueue_locked(
                    ListeningSession(
                        isbn=isbn,
                        start_position=start_pos,
                        end_position=pos,
                        started_at=now - timedelta(seconds=pos - start_pos),
                        ended_at=now,
                        book_duration=effective_duration,
                    )
                )

        # Advance anchor to current position; completed interval is already safe in `_pending`.
        state.anchor_position = pos
        state.anchor_at = now
        if fully_played:
            self._active.pop(isbn, None)
        else:
            state.is_active = is_playing

    def _enqueue_locked(self, session: ListeningSession) -> None:
        if session.end_position <= session.start_position or session.book_duration <= 0:
            return
        self._pending.append(session)
        if len(self._pending) > self._max_pending:
            dropped = len(self._pending) - self._max_pending
            del self._pending[:dropped]
            LOGGER.warning(
                "Fabel listening-session queue exceeded %s; dropped %s oldest item(s)",
                self._max_pending,
                dropped,
            )

    async def _flush_locked(self) -> bool:
        if not self._pending:
            return True
        batch = tuple(self._pending)
        try:
            await self._uploader(batch)
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "Fabel listening-session upload failed (%s pending, will retry): %s",
                len(self._pending),
                err,
            )
            return False
        del self._pending[: len(batch)]
        return not self._pending
