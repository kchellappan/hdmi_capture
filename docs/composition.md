# Using this with a control capture repo

This repo is built to be a submodule. It is one half of a data collection setup whose
other half, for this project, is
[`rpi_gamepad_bridge`](https://github.com/kchellappan/rpi_gamepad_bridge).

```
your_project/
  third_party/
    hdmi_capture/        this repo
    rpi_gamepad_bridge/  controls
  your_project/
    episode.py           yours: what an episode is
    dataset.py           yours: how pairs become training data
```

Neither submodule imports the other, and that is a rule rather than an accident.

## The two rules

**1. The submodules never import each other.**

This repo knows about frames and time. `rpi_gamepad_bridge` knows about controls and time.
Neither knows about the other, so either can be used alone, updated alone, and tested
alone -- and neither needs a dependency on a repo that a given consumer may not have.

**2. Neither submodule owns the word "episode".**

A demonstration has a beginning, an end, a task, and an outcome. All four are facts about
your data collection, not about video or about controllers. If "episode" lived in this
repo it would have to be kept in step with a control library this repo cannot see.

So the parent repo owns it, and writes a manifest that references both halves:

```json
{
  "episode": "pick-place-041",
  "task": "place the red block on the mark",
  "outcome": "success",
  "video": "video/run-041",
  "controls": "controls/run-041.bin",
  "aligned_with_tolerance_ns": 8000000
}
```

That is the file your dataset builder reads. Both submodules just produced inputs to it.

## Alignment

Both halves timestamp on `CLOCK_MONOTONIC` on the same machine, so pairing is subtraction.
See [timebase.md](timebase.md) for what that guarantees and what it does not.

```python
from vcap import Recording
from gpb_client import CaptureReceiver

# Collect, then pair. Not the other way around: doing per-sample work inside the
# capture loop is how frames get dropped.
with Recording("video/run-041") as video:
    for state, arrived_ns in samples:
        frame = video.at(arrived_ns, tolerance_ns=8_000_000)
        if frame is None:
            continue        # no frame close enough; the pair is not usable
        if not frame.ok:
            continue        # flagged at capture; see formats.md
        yield frame, state
```

Three things that go wrong here, in order of how often:

- **Pairing inside the capture loop.** Both capture paths need to be drained promptly.
  Record both streams to disk, then align offline. `vcap` records video on its own writer
  thread for exactly this reason.
- **Omitting the tolerance.** `at()` with no tolerance always returns a frame, however far
  away. That is how a dataset acquires pairs hundreds of milliseconds apart with nothing
  to show it happened.
- **Ignoring the capture offset.** Frame timestamps lag the display by tens of
  milliseconds. Measure it once with `tools/vcap-latency`, record it, and decide
  deliberately whether to shift controls or frames. This repo will not do it for you.

## Recording both at once

The two are independent processes, which is what you want -- neither can stall the other.

```python
from vcap import find_capture_card, Session

device = find_capture_card("MACROSILICON")
with Session(f"video/{episode_id}", device, fps=60,
             notes={"episode": episode_id, "task": task}) as video:
    start_control_capture(episode_id)
    for _frame in video.frames():
        if stop_requested():
            break
    stop_control_capture()
```

`notes` is free-form and lands in the video manifest, which is worth using: it makes a
video directory traceable back to an episode even if the parent manifest is lost.

## Pinning

Submodules pin a commit, which is the behaviour you want here -- a storage format change
should not arrive in your dataset unannounced.

```bash
git submodule add https://github.com/kchellappan/hdmi_capture third_party/hdmi_capture
git -C third_party/hdmi_capture checkout <tag>
git add third_party/hdmi_capture && git commit
```

There is nothing to build and nothing to install. `sys.path.insert` on
`third_party/hdmi_capture` is enough, because the core imports only the standard library.
See `vcap/decode.py` for where that stops being true and why.
