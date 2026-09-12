"""Extracting individual frames from a recording.

Writing every frame of a session as a separate JPEG is occasionally what a tool wants and
usually a mistake: a ten-minute session at 60 Hz is 36000 files, which is slow to create,
slow to copy, and unpleasant on any filesystem. The index already gives random access to
any frame without splitting the file, so prefer Recording[n] where you can.

The legitimate uses are picking out a handful of frames to look at, and feeding a tool
that insists on a directory of images.
"""
from __future__ import annotations

import os

from ..reader import Recording


def extract(recording_dir: str, output_dir: str, *, indices=None,
            every: int | None = None, include_flagged: bool = False) -> list[str]:
    """Write selected frames as .jpg files, named by their index in the recording.

    Names are zero-padded to the recording's width so that lexical order matches capture
    order -- which is what every tool that reads a directory of images assumes, and what
    breaks silently at the thousandth frame if the padding is fixed at three digits.

    Flagged frames are skipped by default. They are in the recording deliberately, but a
    directory of images has nowhere to record that a frame is suspect, so including them
    silently would launder a known-bad frame into looking good.
    """
    os.makedirs(output_dir, exist_ok=True)
    written: list[str] = []
    with Recording(recording_dir) as rec:
        if indices is None:
            step = every or 1
            indices = range(0, len(rec), step)
        width = max(4, len(str(max(1, len(rec) - 1))))
        for i in indices:
            entry = rec.entry(i)
            if entry.flags and not include_flagged:
                continue
            path = os.path.join(output_dir, f"frame_{i:0{width}d}.jpg")
            with open(path, "wb") as fh:
                fh.write(rec[i].data)
            written.append(path)
    return written


def sidecar_timestamps(recording_dir: str, output_path: str) -> str:
    """Write index, timestamp and flags as CSV, to go alongside extracted frames.

    Without this, a directory of extracted JPEGs has lost the only thing that made the
    recording useful. Frame filenames carry order but not time, and order alone cannot be
    aligned against controls.
    """
    with Recording(recording_dir) as rec:
        with open(output_path, "w") as fh:
            fh.write("index,ts_mono_ns,ts_realtime_ns,seq,flags,dropped_before\n")
            for i in range(len(rec)):
                e = rec.entry(i)
                real = rec.to_realtime_ns(e.ts_mono_ns)
                fh.write(f"{i},{e.ts_mono_ns},{real if real is not None else ''},"
                         f"{e.seq},{e.flags},{e.dropped_before}\n")
    return output_path
