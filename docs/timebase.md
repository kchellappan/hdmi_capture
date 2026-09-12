# Time

Everything this repo does is in service of one claim: that a frame and a control sample
can be placed on the same timeline. This file is the single place that claim is defined.
Nothing else in the repo should restate it; where code needs to reason about time it
points here.

## The clock

Every frame carries `ts_mono_ns`, which is the kernel's timestamp for that buffer, on
`CLOCK_MONOTONIC`, taken at the frame's start-of-frame as reconstructed by `uvcvideo`
from the USB SOF counter. It is not a userspace observation and it is never recomputed.

`rpi_gamepad_bridge`'s `CaptureReceiver` stamps each arriving control sample with
`time.monotonic_ns()`. That is the same clock, in the same units, on the same machine.

So alignment is subtraction. There is no clock synchronisation protocol here, no drift
correction, and no shared epoch to negotiate, and that is a deliberate design constraint
rather than a happy accident: **video and controls must be captured on the same machine.**
If capture ever moves to two boxes, this entire scheme is void and no amount of code in
this repo can rescue it.

Whether the clock really is monotonic is recorded per session rather than assumed.
`manifest.timebase.comparable_with_monotonic_ns` is read from the driver's buffer flags at
setup, and `Recording.timestamps_are_monotonic` exposes it. A recording where it is false
cannot be aligned, and `vcap-verify` calls that a problem.

Monotonic time does not survive a reboot and cannot be rendered as a date, so each
manifest also records one `CLOCK_MONOTONIC`/`CLOCK_REALTIME` pair sampled back to back at
session start, with the spread between the two reads. `Recording.to_realtime_ns()` uses
it. The pair is stated rather than assumed so that a reader can see how precise it is --
the same reason `rpi_gamepad_bridge` sends both clocks in every state.

## What the timestamp does not mean

It is when the **USB frame reached the kernel**. It is not when the photons left the
display.

Between the two sit the source's own output pipeline, the capture card's internal
buffering, and the USB transfer. Together that is tens of milliseconds, it is invisible
from software, and it is not constant across setups -- a different console, a different
output resolution, or a different cable path all change it.

This matters more than it sounds like it should. A button press recorded 50 ms before the
frame it caused trains a policy to act 50 ms early, every time, consistently enough that
it will look like a subtle control-tuning problem rather than a data bug.

So:

- The offset is **measured once per setup**, with `tools/vcap-latency` supplying the video
  half and your control recording supplying the other.
- It is **recorded** in `manifest.timebase.capture_offset_ns`, together with
  `capture_offset_method` describing how it was obtained.
- It is **never folded into `ts_mono_ns`**. A corrected timestamp is indistinguishable
  from an uncorrected one, so a recording that silently applied a correction cannot be
  re-corrected later when the measurement is improved, and cannot be trusted by anyone
  who did not perform it.

`capture_offset_ns` defaults to `null`, which means nobody has measured it. That is
honest, and it is better than a plausible default that quietly becomes fact.

## Aligning, in practice

The pairing primitive is `Recording.at(ts_mono_ns, tolerance_ns=...)`.

`tolerance_ns` has no default, on purpose. "Close enough" depends entirely on what the
pair is for: a policy trained at 10 Hz can tolerate 50 ms of slack, one at 60 Hz cannot,
and a replay script that only needs the right second can tolerate far more than either. A
default here would silently become that decision for every caller, so the parameter is
required to be thought about.

Passing `None` returns the nearest frame at any distance. That is occasionally what you
want and is usually how a dataset acquires pairs that are half a second apart without
anyone noticing, so prefer a real tolerance and handle the `None` return.

Note also that a gap is not the same as a long interval between frames. If the driver
dropped frames, `index_at()` returns the frame that was genuinely on screen at that
timestamp -- the one before the gap -- rather than interpolating to a frame that does not
exist. Frames adjacent to a gap carry `FLAG_GAP_BEFORE` so a dataset builder can exclude
pairs that straddle one.
