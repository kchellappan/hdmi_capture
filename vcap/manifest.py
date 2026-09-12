"""The manifest: everything about a recording that is not a frame.

Written at the start of a session so that a recording interrupted by a crash or a power
loss is still self-describing, and updated at the end with the counters. A reader must
tolerate the end-of-session fields being absent -- that is what an unclean shutdown looks
like, and the frames before it are still good.
"""
from __future__ import annotations

import json
import os
import platform
import time

# Bumped when a change would make an older reader misinterpret a newer recording.
# Adding a field does not count; changing the meaning of one does.
FORMAT_VERSION = 1


def clock_pair() -> dict:
    """CLOCK_MONOTONIC and CLOCK_REALTIME sampled together.

    Frame timestamps are monotonic, which is what makes them comparable with control
    timestamps taken on the same machine, but monotonic time means nothing across a
    reboot and cannot be rendered as a date. Recording both clocks once, back to back,
    is what lets a monotonic timestamp be converted to wall clock afterwards -- and
    keeps the conversion honest, because the offset is stated rather than assumed.

    rpi_gamepad_bridge sends both clocks in every state for the same reason.
    """
    mono = time.monotonic_ns()
    real = time.time_ns()
    mono_after = time.monotonic_ns()
    return {
        "monotonic_ns": mono,
        "realtime_ns": real,
        # How long the two reads took. If this is large the pairing is correspondingly
        # imprecise, and a reader can see that rather than trusting it blindly.
        "sample_spread_ns": mono_after - mono,
    }


def build(*, session: str, device, negotiated, requested: dict,
          stream_file: str, index_file: str, notes: dict | None = None) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "session": session,
        "files": {"stream": stream_file, "index": index_file},
        "device": device.identity(),
        # What was asked for and what was granted, separately. S_FMT is a negotiation
        # and the driver may return something else; recording only the request would
        # describe a recording that does not exist.
        "requested": requested,
        "granted": {
            "pixelformat": negotiated.pixelformat,
            "width": negotiated.width,
            "height": negotiated.height,
            "fps": negotiated.fps_granted,
        },
        "timebase": {
            "clock": negotiated.timestamp_clock,
            "source": negotiated.timestamp_source,
            "comparable_with_monotonic_ns": negotiated.timestamps_are_monotonic,
            # The offset between a frame's timestamp and the instant the photons left
            # the source display. Unknown unless someone measured it; see
            # tools/vcap-latency and docs/timebase.md. Never guessed, because a guessed
            # offset folded into timestamps cannot be distinguished later from a
            # measured one.
            "capture_offset_ns": None,
            "capture_offset_method": None,
        },
        "started": clock_pair(),
        "host": {
            "hostname": platform.node(),
            "kernel": platform.release(),
        },
        "notes": notes or {},
    }


def finalize(manifest: dict, *, stats: dict, frames_written: int,
             bytes_written: int) -> dict:
    manifest["ended"] = clock_pair()
    manifest["frames_written"] = frames_written
    manifest["bytes_written"] = bytes_written
    manifest["counters"] = dict(stats)
    started, ended = manifest["started"]["monotonic_ns"], manifest["ended"]["monotonic_ns"]
    duration_ns = ended - started
    manifest["duration_ns"] = duration_ns
    # Measured, not requested: frames actually written over wall time actually elapsed.
    manifest["effective_fps"] = (
        frames_written / (duration_ns / 1e9) if duration_ns > 0 else None)
    return manifest


def write(path: str, manifest: dict) -> None:
    """Write atomically, so a reader never sees a half-written manifest."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)
