# hdmi_capture

> **This repository is primarily AI-generated, with human review.** Treat it accordingly:
> read it before you depend on it.
>
> What that means in practice here. The hardware claims are **measured, not asserted** —
> the figures in this README and in `docs/hardware.md` came from running against the card
> named below, and the commit messages record what was measured and what was found wrong.
> Three documented claims were corrected during development after measurement contradicted
> them, including a storage figure that was six times too low. The hardware-free test suite
> was checked by mutation: each test was confirmed to fail when the behaviour it covers is
> broken. Where a claim is second-hand rather than measured — the MS2109 notes in
> `docs/hardware.md` are the main case — it says so on the spot.
>
> What that does **not** cover: one capture card, on one machine, over short sessions. No
> long-run or multi-day soak, no second card, no non-x86 host, and no review by anyone who
> has shipped a V4L2 binding before. The `Status` section at the bottom lists what is
> known-untested; it is deliberately specific, and it is not exhaustive.

Video capture for control-and-video data collection on Linux. Records an HDMI source
through a USB capture card, keeping the kernel's per-frame timestamp intact so the frames
can be paired with controls captured on the same machine — for replaying repetitive
inputs, or for training an ACT or VLA policy.

Built to be a submodule alongside a control capture repo. The core imports only the
standard library, so adding it to a project adds nothing to install.

```bash
git clone https://github.com/kchellappan/hdmi_capture
cd hdmi_capture
./tools/vcap-list                        # what is plugged in
./tools/vcap-probe -d MACROSILICON       # what it can actually do
./tools/vcap-view  -d MACROSILICON       # look at it in a browser
./tools/vcap-record data/run-01 -d MACROSILICON --seconds 30
./tools/vcap-verify data/run-01          # before you train on it
```

```python
from vcap import find_capture_card, Recording, Session

device = find_capture_card("MACROSILICON")
with Session("data/run-01", device, fps=60) as s:
    s.record(seconds=30)

with Recording("data/run-01") as rec:
    frame = rec.at(control_sample_ts_ns, tolerance_ns=8_000_000)
```

## The idea

A frame is only training data if you know what the operator was doing when it was on
screen. So the whole repo is arranged around one number: `Frame.ts_mono_ns`, the kernel's
`CLOCK_MONOTONIC` timestamp for that capture buffer, carried untouched from the V4L2 buffer
through to the on-disk index.

That is the same clock `rpi_gamepad_bridge`'s client stamps control samples with, on the
same machine, so alignment is subtraction — no sync protocol, no shared epoch, no drift
correction. The cost of that simplicity is a hard constraint: **video and controls must be
captured on the same machine.** [docs/timebase.md](docs/timebase.md) is the one place that
contract is written down, including what the timestamp does *not* mean.

Three decisions follow from it:

**Nothing is decoded on the capture path.** The card delivers MJPEG, already compressed, so
recording a frame is an append of about 40 KB. Decoding and re-encoding during capture
would cost real CPU and would throw away the exact timestamps by quantising them to a
container's timebase. Decode belongs in the dataset builder, which runs offline and can
afford it.

**Frames are stored with an index, not in a container.** A `.mjpg` of raw concatenated
frames plus a `.idx` of fixed 32-byte records, so frame *N* is at byte *32N* and finding
the frame at a given timestamp is a binary search over an `mmap`. That is the access
pattern a dataloader has. See [docs/formats.md](docs/formats.md).

**Recording and live viewing get opposite buffers.** A recorder must never drop a frame,
because a hole corrupts an episode. A policy running inference must always drop, because a
stale frame is worse than a late one. Those are different structures — `writer.AsyncWriter`
and `ring.LatestFrame` — rather than one with a mode flag.

## Hardware

Developed against a **MacroSilicon MS2130** (`345f:2131`), sold as a "Rybozen 4K HDMI to
USB 3.0 capture card" and under many other names —
[the unit used here](https://www.amazon.com/dp/B097DKNS1M). It is UVC class, so `uvcvideo`
drives it with nothing to install. Measured on it, on live 1080p60 video over a 4-minute
run: **14403 frames to disk at 59.98 fps, 4.27 GB** — at **14.5–19 MB/s, or 52–69 GB/hour**,
which is the number to plan storage against.

Frame rate is settable with `--fps` and honoured exactly — 60, 50, 30, 20 and 10 at 1080p.
Storage scales linearly with it, since MJPEG compresses each frame independently: **60 Hz is
53 GB/hour, 30 Hz is 27, 10 Hz is 9.** A rate the card does not offer is rounded *up* to one
it does, so asking for 25 gets you 30; `vcap-record` says so when that happens. Full table
in [docs/hardware.md](docs/hardware.md).

Sustained capture loses about **one frame in a thousand** (0.103% over a 20-minute,
72009-frame burn-in; one roughly every 16 s), always a single frame and always right after a
damaged one — the signature of a USB transfer error. A 15-second run loses none, so it only
shows up over real durations. Both neighbours are flagged and `Recording.flagged()` lists
them.

The loss is **per frame, not per second** — measured at both rates, which rules out a
fixed-rate hiccup. So halving the capture rate halves the losses per minute but leaves any
given frame just as likely to be lost. Capture slower if you care how often an episode has
a hole; it will not change the ~0.1% of pairs that are unusable.

Buy on the USB ID, not the listing. Generic capture cards are relabelled constantly, and a
single marketplace listing can change vendor or silicon without changing its product page —
so that link is a record of what was bought, not a guarantee of what arrives. What this
repo's measurements apply to is a device that reports:

```
$ lsusb | grep -i macrosilicon
Bus 004 Device 002: ID 345f:2131 MACROSILICON USB3.0 Video
```

If yours reports `534d:2109` instead, it is the older USB 2.0 MS2109 in similar packaging:
expect 1080p30, and re-measure everything here.

Read [docs/hardware.md](docs/hardware.md) before a collection run. The traps that cost the
most:

| Trap | Consequence |
|---|---|
| **The card emits frames with nothing connected to its HDMI input** | Every software signal looks healthy while you record 60 fps of a placeholder image. No counter in this repo can detect it — look at the picture first. |
| The first frame of nearly every stream is an undecodable fragment | It is the *back half* of a frame in flight when `STREAMON` landed — no header, so it cannot be decoded. Happens with a live source too. Flagged, not hidden. Filter on `Recording.flagged()`. |
| Descriptors advertise modes the card cannot sustain | It claims every geometry in both MJPEG and YUYV at identical rates. Measure with `vcap-probe`; never trust the list. |
| `/dev/videoN` numbering moves, and two nodes appear per device | One is metadata-only. Always go through `/dev/v4l/by-id/`; `find_capture_card()` does. |
| A source changing resolution changes the image mid-stream | The V4L2 format does not change with it. `vcap-verify` checks JPEG headers to catch it. |
| `/dev/video*` access comes from a logind ACL, not the `video` group | So it works from a desktop session and fails from a service or over SSH. Install the udev rule. |

Use the card's **HDMI loop-out** to drive the operator's monitor. Hardware pass-through
adds no latency, which no software preview can match, and it removes any need for this repo
to provide one.

## What is in here

```
vcap_py/vcap/    Python: capture, storage, read-back        import vcap
vcap_cpp/        C++: capture only                          #include "vcap/source.hpp"
tools/           command line tools (Python)
docs/            the reasoning; read hardware.md before a run
tests/           hardware-free suite, plus hardware.sh
scripts/         optional setup: udev rule, device access
```

The directories say which language; the name you import or include is `vcap` in both.

| | |
|---|---|
| `vcap_py/vcap/v4l2.py` | The V4L2 ioctl interface in `ctypes`. Structure sizes are ABI; the padding is load-bearing. |
| `vcap_py/vcap/device.py` | Enumeration and stable identity through `by-id`. |
| `vcap_py/vcap/probe.py` | Capability measurement, because the descriptors overstate. |
| `vcap_py/vcap/source.py` | The capture loop. `mmap`'d buffers in, timestamped frames out. Drops nothing, converts nothing. |
| `vcap_py/vcap/writer.py` | Segmented `.mjpg`/`.idx` writing on its own thread. Raises rather than dropping. |
| `vcap_py/vcap/index.py` | Fixed-size records; random access and timestamp search. |
| `vcap_py/vcap/reader.py` | A recording opened for random access, segments presented as one sequence. |
| `vcap_py/vcap/ring.py` | Latest-frame-wins, for live consumers. |
| `vcap_py/vcap/session.py` | Source, writer and manifest wired together. |
| `vcap_py/vcap/decode.py` | The dependency boundary, and why it is there. |
| `vcap_py/vcap/export/` | MP4/MKV remux and frame extraction. For humans, not for training. |

Tools: `vcap-list`, `vcap-probe`, `vcap-record`, `vcap-verify`, `vcap-view`,
`vcap-latency`. All run from a checkout with no install step.

`clients/cpp/` is a C++ capture client that writes the same format — for a recorder with no
Python in the loop. It captures only; anything it writes is read back with the Python
reader above, which is what lets it stay dependency-free. See
[clients/cpp/README.md](clients/cpp/README.md).

## Dependencies

The capture, storage and read-back path imports only the standard library, and
`tests/check_stdlib_only.py` fails the build if that changes. This was not asceticism —
V4L2 is `ioctl` and `mmap`, and Python already has both, so there was nothing to install in
the first place. The useful consequence is that a repo submoduling this one can record data
on a fresh machine with no venv, no pip and no build step.

Decoding is where that stops being true, and `vcap_py/vcap/decode.py` says so rather than
pretending otherwise. Turning a JPEG into an array is work that numpy, Pillow and OpenCV
already do better than pure Python could, and a machine that trains a policy has them
already. So the boundary is explicit and narrow:

- `vcap` core — standard library only, enforced.
- `vcap.decode` — optional imports, resolved at call time with a clear error.
- `vcap.export.*` — optional imports and external binaries (`ffmpeg`).

The rule is not "no dependencies". It is that recording an episode costs nothing to set up,
and needing tensors is an explicit step that says so.

## Using it as a submodule

```bash
git submodule add https://github.com/kchellappan/hdmi_capture third_party/hdmi_capture
```

Nothing to build. Put the directory on `sys.path` and import `vcap`.

Two rules, covered in [docs/composition.md](docs/composition.md): this repo and the control
capture repo **never import each other**, and **neither owns the word "episode"** — that
belongs to the parent repo, which is the only place that knows where a demonstration begins
and what it was for.

## Tests

```bash
./tests/run_tests.sh     # no hardware, no dependencies
./tests/hardware.sh      # with the card plugged in
```

The hardware-free suite covers the storage format end to end, because that is where a bug
costs a dataset: synthetic JPEGs stand in for the card, and everything from the writer
through to `Recording` runs against them. Every test in it has been confirmed to fail when
the behaviour it targets is broken.

What it cannot cover, and does not claim to: format negotiation, buffer `mmap`, kernel
timestamps, and every hardware behaviour in `docs/hardware.md`. Those need the card, and
live in `tests/hardware.sh`.

## Status

Working and measured on one card, on one machine, over sessions of seconds to a minute.

Not exercised on hardware:

- **Runs beyond 20 minutes.** A 20-minute burn-in at 1080p60 (72009 frames, 21.2 GB, nine
  segment rollovers) showed no drift in drop rate, frame interval or queue depth. Nothing
  has run for hours, and nothing has tested behaviour as a disk approaches full.
- **A second identical card on the same host.** The `by-id` serial may not be unique on
  this chipset family; `by-path` would distinguish them at the cost of pinning a port.
- **YUYV as a recording format.** It streams — verified on the card at 1280x720 and on a
  UVC webcam at 1080p — but no session has been recorded in it, and the corruption check
  in `source.read()` is MJPEG-only, so a raw capture gets no structural validation.
- **Audio**, which this repo does not touch at all.
- **The MS2109 variant**, and non-x86-64 hosts.

`vcap_py/vcap/export/to_lerobot.py` is named in the design and not written. The display-to-USB
latency offset (`timebase.capture_offset_ns`) is never populated automatically and needs a
control recording to measure — see [docs/timebase.md](docs/timebase.md).

## License

MIT.
