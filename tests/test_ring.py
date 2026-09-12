#!/usr/bin/env python3
"""Latest-frame-wins buffering.

The invariant worth testing is the one that distinguishes this from a queue: a slow
consumer must get the newest frame, never a backlog. A structure that quietly queued
would make a policy act on stale observations, which is the failure this class exists to
prevent and which would be invisible in a live demo.
"""
from __future__ import annotations

import sys
import threading
import time

import fake

from vcap.ring import LatestFrame

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def main() -> int:
    ring = LatestFrame()
    frame, generation = ring.get()
    check("starts empty", frame, None)
    check("generation starts at zero", generation, 0)

    produced = list(fake.frames(10))
    for f in produced:
        ring.put(f)

    frame, generation = ring.get()
    check("holds the newest frame", frame.seq, 9)
    check("generation counts every put", generation, 10)
    # Nine frames were overwritten before being read. A consumer seeing this rise knows
    # it is sampling more slowly than the source produces.
    check("overwrites are counted", ring.overwritten, 9)

    # A consumer already up to date must block rather than re-reading the same frame.
    started = time.monotonic()
    frame, generation = ring.wait_for_new(generation, timeout=0.05)
    check("waiting when up to date blocks until the timeout",
          time.monotonic() - started >= 0.04, True)

    # And must return immediately when it is behind, with the newest frame rather than
    # the next one in order.
    ring.put(produced[0])
    ring.put(produced[5])
    frame, new_generation = ring.wait_for_new(generation, timeout=1.0)
    check("a behind consumer gets the newest frame, not the next", frame.seq, 5)
    check("generation advanced by both puts", new_generation, generation + 2)

    # A waiter blocked on an empty ring must wake when a frame arrives.
    ring2 = LatestFrame()
    result = {}

    def consumer():
        f, g = ring2.wait_for_new(0, timeout=2.0)
        result["seq"] = f.seq if f else None

    thread = threading.Thread(target=consumer)
    thread.start()
    time.sleep(0.05)
    ring2.put(produced[3])
    thread.join(timeout=2.0)
    check("a blocked waiter is woken by a put", result.get("seq"), 3)

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    latest-frame-wins semantics")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
