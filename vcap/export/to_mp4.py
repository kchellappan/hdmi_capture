"""Remuxing a recording into MP4 or MKV, for humans rather than for training.

This is a remux, not a transcode: the JPEG frames are copied into the container
untouched, so it costs no quality and almost no CPU. What it does cost is timestamp
precision -- a container quantises presentation times to a fixed timebase, and MJPEG in
MP4 is awkward enough that some players will only accept a constant frame rate. That is
why the .mjpg/.idx pair stays the source of truth and this is an export.

Use it to watch a session back, to attach to a bug report, or to eyeball whether a
demonstration is worth keeping. Do not use its output as training input: the frame
timestamps in it are approximations of the ones in the index.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from ..reader import Recording


class FfmpegMissing(RuntimeError):
    pass


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FfmpegMissing(
            "ffmpeg is not installed. It is needed only for export, not for capture: "
            "apt install ffmpeg")
    return path


def remux(recording_dir: str, output: str, *, fps: float | None = None,
          segment: int = 0, overwrite: bool = False) -> str:
    """Wrap one segment's MJPEG stream in a container without re-encoding.

    `fps` defaults to the recording's measured effective rate rather than the rate it
    requested, because that is what the frames actually are. A constant-rate container
    built from variable-rate frames drifts; for anything where that matters, read the
    index instead of watching the file.
    """
    exe = ffmpeg_path()
    with Recording(recording_dir) as rec:
        segments = rec.manifest.get("segments", [])
        if not segments:
            raise ValueError(f"{recording_dir} lists no segments")
        if segment >= len(segments):
            raise IndexError(f"segment {segment}; recording has {len(segments)}")
        stream = os.path.join(rec.directory, segments[segment]["stream"])
        rate = fps or rec.manifest.get("effective_fps") or \
            (rec.manifest.get("granted") or {}).get("fps") or 30.0

    cmd = [exe, "-hide_banner", "-loglevel", "warning",
           "-y" if overwrite else "-n",
           "-f", "mjpeg", "-framerate", f"{rate:g}", "-i", stream,
           "-c:v", "copy", output]
    subprocess.run(cmd, check=True)
    return output


def transcode(recording_dir: str, output: str, *, crf: int = 18,
              fps: float | None = None, segment: int = 0,
              overwrite: bool = False) -> str:
    """Re-encode to H.264, for when the MJPEG file is too large to move around.

    MJPEG is an intra-only codec, so a session compresses by a large factor here -- often
    ten to one or better on game footage, which has long static stretches. That makes
    this the right way to archive or share a session and the wrong way to store one you
    intend to train on: the frames a decoder produces afterwards are not the frames the
    card sent.
    """
    exe = ffmpeg_path()
    with Recording(recording_dir) as rec:
        segments = rec.manifest.get("segments", [])
        if not segments:
            raise ValueError(f"{recording_dir} lists no segments")
        stream = os.path.join(rec.directory, segments[segment]["stream"])
        rate = fps or rec.manifest.get("effective_fps") or 30.0

    cmd = [exe, "-hide_banner", "-loglevel", "warning",
           "-y" if overwrite else "-n",
           "-f", "mjpeg", "-framerate", f"{rate:g}", "-i", stream,
           "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
           "-pix_fmt", "yuv420p", output]
    subprocess.run(cmd, check=True)
    return output
