# Notes for agents working on this repo

Start with [README.md](README.md) for what this is. This file covers what is not obvious
from the code: what cannot be verified without hardware, the conventions that are
deliberate, and what is known to be unfinished.

## Hardware in the loop

There is one capture card: a MacroSilicon MS2130, USB `345f:2131`. Everything measured
about it is in [docs/hardware.md](docs/hardware.md) — read that before changing anything in
`vcap_py/vcap/source.py` or `vcap_py/vcap/device.py`.

The package lives in `vcap_py/vcap/` and the C++ implementation in `vcap_cpp/`; the
directories name the language, the importable and includable name is `vcap` in both.
Anything putting this repo on `sys.path` wants `vcap_py`, not the root.

```bash
./tests/run_tests.sh          # anywhere: no card, no dependencies
./vcap_cpp/tests/run_tests.sh # builds C++, round-trips through the Python reader
./tests/hardware.sh           # needs the card plugged in
```

The hardware-free suite covers the storage format end to end, which is where a bug costs a
dataset. It cannot cover format negotiation, buffer `mmap`, kernel timestamps, or any
firmware behaviour. **Say so plainly rather than implying a change is verified when only
its logic is.** A change to the capture loop that passes `run_tests.sh` is untested.

An HDMI source is *not* required for those — but see below, because that cuts both ways.
`tools/vcap-glass-to-glass` is the exception: it needs a display fed by the card, and
`python3-tk`, which Debian and Ubuntu package separately.

## Conventions that are deliberate

**The timestamp is the product.** `Frame.ts_mono_ns` comes from the kernel and is never
recomputed, adjusted, interpolated or corrected anywhere in this repo. If a change would
make a frame's timestamp something other than what the driver reported, it is wrong. The
whole reason the V4L2 layer is hand-written rather than built on OpenCV is that
`VideoCapture` discards it. See [docs/timebase.md](docs/timebase.md).

**Never report health you cannot verify.** This card emits a full-rate, valid MJPEG stream
with *nothing connected to its HDMI input*. Frames arrive, timestamps advance, nothing
flags, and a recording of a placeholder image verifies clean. So no counter here claims to
know whether a source is attached, `source.stats()` deliberately omits any figure for
frames the card dropped before the kernel saw them, and `vcap-probe` says outright that its
own success is not evidence of a signal. Do not add a "healthy" indicator that cannot
distinguish these cases.

**Requested and granted are separate facts.** `VIDIOC_S_FMT` is a negotiation; the driver
may silently return something else. Every manifest records both. Never log or store only
the request.

**Flag, do not discard.** The first frame of nearly every session from this card is an
undecodable fragment -- the tail of a frame that was in flight when `STREAMON` landed. It
is written to the recording with `FLAG_CORRUPT` rather than dropped, because a recording
with a silently removed frame looks complete and is not. Filtering is the consumer's
decision -- `Recording.flagged()`, `Frame.ok`. The same applies to gaps.

**Record and live are opposite policies.** `AsyncWriter` never drops and raises when it
cannot keep up; `LatestFrame` always drops and keeps only the newest. Merging them into one
structure with a mode flag was considered and rejected: it makes every call site responsible
for knowing which behaviour it gets.

**Structure sizes in `v4l2.py` are ABI.** An ioctl request number encodes its argument's
size, so a structure one byte off makes every call return `ENOTTY` — which reads as "the
device does not support this" and is a long way from its cause. The padding is explained
where it appears and `tests/test_ioctl_abi.py` pins the resulting numbers against the
kernel's. This is the one place a fact is deliberately stated twice.

**Verify a test fails on the bug it targets.** Every test in `run_tests.sh` has been
confirmed against a deliberate mutation of the code it covers. A test that has only ever
passed is not evidence. This caught a real mistake during development: a tolerance test used
"half a second after frame 75", which at 60 Hz lands within 10 ns of frame 105, so the test
failed while the code was right — the comment recording that is in `tests/test_storage.py`.

**The dependency boundary is a line, not a ban.** The core is standard library only and
`tests/check_stdlib_only.py` enforces it, because V4L2 needs nothing more and a submodule
should cost nothing to install. Decoding is a genuine exception and lives behind
`vcap_py/vcap/decode.py` and `vcap_py/vcap/export/`, listed in that check's `ALLOWED`. Adding to that list
is allowed; doing it without a reason in the commit message is not. Do not put a decoder,
an encoder or a tensor in the core to preserve a tidy import graph.

**One declaration of a fact.** The `vcap_py/` move broke `tests/hardware.sh` because the
package path was written into every heredoc in it rather than once at the top, and the
restructure updated the copies that were greppable from Python and missed the ones embedded
in shell. It reached `main` and survived three merges, because CI cannot run that suite.
Where a fact must appear twice, say why — `tests/test_ioctl_abi.py` is the one place that
earns it.

**Do not compute while measuring.** The first end-to-end run of `vcap-glass-to-glass`
reported a median of 296 ms with a 248 ms spread, and was measuring itself: DC-decoding a
1080p frame is ~107 ms of pure Python, which saturated the interpreter lock and starved the
drawing loop, so the pattern on screen went stale and the staleness got measured. Capture
now stores frames and decodes them afterwards. The giveaway was a standard deviation far
larger than a frame period; anything timing the display should watch for the same shape.

**A search needs validation proportional to its width.** Locating the bar pattern began by
trying every pair of bright runs — about a hundred candidate geometries against six bits of
checksum — and every single-bar corruption still "decoded", just to a different value.
Narrowing to the four geometries that can actually be right fixed it. The false-decode rate
is now measured rather than assumed, and `tests/test_barcode.py` holds it to a budget.

**Neither this repo nor a control repo owns "episode".** That belongs to whatever repo
submodules both. See [docs/composition.md](docs/composition.md). A change that makes this
repo import a control library, or vice versa, breaks the property that either can be used
alone.

## Where the traps are already written down

Do not re-derive these:

| Trap | Where |
|---|---|
| Frames arrive with no HDMI input connected | `docs/hardware.md`, and asserted in `tests/hardware.sh` |
| The first frame after `STREAMON` is a headless fragment, on 39 of 40 starts | `docs/hardware.md`; the cause is stream-start alignment, *not* a missing HDMI input |
| UVC descriptors advertise unsustainable modes | `docs/hardware.md`, and why `vcap_py/vcap/probe.py` exists |
| `/dev/videoN` numbering moves; two nodes per device | `vcap_py/vcap/device.py` docstring |
| `/dev/video*` access is a logind ACL, not the `video` group | `docs/hardware.md`, `scripts/install_deps.sh` |
| `v4l2_format` needs 4 bytes of padding after `type` | the `Format` docstring in `vcap_py/vcap/v4l2.py` |
| A container loses the exact per-frame timestamp | `docs/formats.md`, and the header of `vcap_py/vcap/export/to_mp4.py` |
| `at()` without a tolerance always returns a frame | `vcap_py/vcap/reader.py`, `docs/timebase.md` |
| Storage rate is content, not resolution: 2.5 to 19 MB/s on the same card | `docs/formats.md`, three measurements |
| Frame loss is per frame, not per second, so a slower rate does not make any frame safer | `docs/hardware.md`, measured at 30 and 60 Hz |
| A window need not be fullscreen for the latency pattern; it locates itself | `vcap_py/vcap/barcode.py`; Chrome's `--kiosk` silently does not take on GNOME/Wayland |

## What CI does and does not cover

Measured, not assumed -- the workflow was run and its logs read.

Covered on every push: the Python suite, the stdlib-only check, the ioctl ABI constants,
the C++ client built both with cmake and through the direct-g++ fallback, and the
cross-language round trip where C++ writes a recording and Python reads it back.

**Not covered: the capture loop.** `tests/test_vivid.py` exists to exercise the real
ioctl, mmap and DQBUF path against the kernel's virtual video driver, but GitHub's runners
ship no `vivid` module, so that job skips every time. A green tick on it means "did not
run". The capture loop is only ever exercised by hand, against the card, via
`tests/hardware.sh` and `vcap_cpp/tests/roundtrip.sh`.

## Known unfinished

- **Runs beyond 20 minutes** are untested, as is behaviour with a nearly full disk.
- **One card, one machine.** A second identical unit is untested and may collide on
  `by-id` — this chipset family often ships a shared firmware serial. `by-path` would
  distinguish them at the cost of pinning a physical port.
- **YUYV capture works but is barely exercised.** The capture loop has been run at
  YUYV 1920x1080 against a UVC webcam, not against this card, and the corruption check in
  `source.read()` is MJPEG-only -- so a raw capture gets no structural validation beyond a
  short buffer. There is no reason to prefer YUYV here anyway; see `docs/hardware.md`.
- **Audio is untouched.** The card exposes an ALSA device. The MS2109's audio path is known
  broken; whether the MS2130's is, is not tested here.
- **`capture_offset_ns` is never populated automatically,** and should not be.
  `tools/vcap-glass-to-glass` now measures it end to end where the capturing machine also
  drives the display — about 60 ms ± 6 here — but that is this laptop's display pipeline
  plus this card, and only the card-and-USB half transfers to another source. The method
  cannot separate them. `tools/vcap-latency` remains for the case where the source is a
  console, and supplies only the video half.
- **`vcap_py/vcap/export/to_lerobot.py` is named in the README's design and not written.**
- **Cross-machine capture is out of scope and cannot be rescued here.** The single-clock
  guarantee is the foundation of the format; two machines means two clocks and a different
  design.

## Workflow

`main` is protected: a pull request is required, six CI checks must pass, the branch must be
up to date before merging, history stays linear, and force pushes and deletions are refused.
Branch, PR, squash merge. Commit messages explain *why*, including what was tried and
rejected — match the existing ones.

Two things about that protection are worth knowing rather than discovering.

**Admin bypass is on.** `enforce_admins` is false, so a push straight to `main` by the repo
owner succeeds with a `Bypassed rule violations` warning rather than being rejected. It is
not a wall, it is a speed bump, and the warning scrolls past easily. Treat the PR route as
the rule anyway.

**Force pushes are refused even so.** That one is enforced for everyone, which is worth
knowing before assuming a bad commit can be quietly removed — undoing one means lifting the
rule, pushing, and putting it back.

**Zero approvals are required.** GitHub does not let anyone approve their own pull request,
so requiring even one would make every PR unmergeable on a single-maintainer repo. The PR
requirement is there for the CI gate and the diff, not for review theatre.

The required checks are named in the protection rule, so **renaming a CI job silently stops
gating on it** — the rule keeps waiting for a check that no longer reports. If a job in
`.github/workflows/ci.yml` is renamed, update the protection too. The vivid job is
deliberately *not* required: it always skips on GitHub's runners, so requiring it would
enshrine a check that proves nothing.
