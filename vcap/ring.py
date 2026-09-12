"""Latest-frame-wins buffering, for consumers that must not fall behind.

The mirror image of writer.py. A policy running inference wants the most recent frame and
has no use for the one before it: acting on a frame from 200 ms ago is worse than waiting
for a fresh one, because the world has moved. So this keeps exactly one frame -- the
newest -- and overwrites it.

Keeping both behaviours in one buffer was considered and rejected. A single structure that
sometimes blocks and sometimes discards has to be told which it is doing at every call
site, which is the same as having two structures with worse names.
"""
from __future__ import annotations

import threading

from .frame import Frame


class LatestFrame:
    """One slot, overwritten by the producer, read by any number of consumers."""

    def __init__(self):
        self._frame: Frame | None = None
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._generation = 0
        self.overwritten = 0

    def put(self, frame: Frame) -> None:
        with self._arrived:
            if self._frame is not None:
                # Counted, not silent: a consumer that finds this rising knows it is
                # sampling more slowly than the card produces, which is a fact about
                # its own loop rather than about the capture.
                self.overwritten += 1
            self._frame = frame
            self._generation += 1
            self._arrived.notify_all()

    def get(self) -> tuple[Frame | None, int]:
        """The current frame and its generation, without waiting."""
        with self._lock:
            return self._frame, self._generation

    def wait_for_new(self, since: int, timeout: float | None = None
                     ) -> tuple[Frame | None, int]:
        """Block until a frame newer than generation `since` arrives.

        Taking a generation rather than "the next frame" is what makes this correct for
        a consumer whose own processing is slower than capture: it gets the newest frame
        at the moment it asks, and never a stale one that happened to be queued.
        """
        with self._arrived:
            if self._generation > since:
                return self._frame, self._generation
            self._arrived.wait_for(lambda: self._generation > since, timeout=timeout)
            return self._frame, self._generation
