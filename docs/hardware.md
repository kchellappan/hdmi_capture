# The card

Measured on the card this repo was built against: **MacroSilicon MS2130**, USB ID
`345f:2131`, sold as a "Rybozen 4K HDMI to USB 3.0 capture card" and under many other
names. The unit used here was https://www.amazon.com/dp/B097DKNS1M -- though the USB ID
below is the identifier that matters, since these cards are relabelled constantly and a
listing can change silicon without changing its page. Everything below was observed on that device on Linux 7.0 with `uvcvideo`; where a
claim comes from the general UVC chipset family rather than from measurement here, it says
so.

```
$ lsusb | grep -i macrosilicon
Bus 004 Device 002: ID 345f:2131 MACROSILICON USB3.0 Video
```

## What it actually delivers

Genuine 1080p60 in MJPEG, sustained, with no dropped frames on an idle machine. Measured
both with nothing connected and with live video from a laptop's HDMI output:

```
no input:     MJPG 1920x1080 @ 60 -> 60.04 fps    41 KB/frame   dropped=0
live video:   MJPG 1920x1080 @ 60 -> 59.76 fps   236 KB/frame   dropped=0
              901 frames recorded to disk over 15 s, writer queue never exceeded 0
```

The frame size difference matters for planning: live content is about six times the data,
**14.5-19 MB/s or 52-69 GB/hour**. See [formats.md](formats.md#sizing).

## Frame rate is settable, and honoured exactly

Unlike the resolution list, the rate control on this card is real. Measured at 1080p MJPEG,
4 seconds per rate:

| requested | measured | interval | KB/frame | rate | per hour |
|---|---|---|---|---|---|
| 60 | 60.01 | 16.66 ms | 236 | 14.6 MB/s | 53 GB |
| 50 | 50.01 | 20.00 ms | 241 | 12.4 MB/s | 45 GB |
| 30 | 29.75 | 33.33 ms | 246 | 7.5 MB/s | 27 GB |
| 20 | 20.00 | 49.99 ms | 270 | 5.6 MB/s | 20 GB |
| 10 | 10.00 | 100.00 ms | 241 | 2.5 MB/s | 9 GB |

Frame size barely moves, because MJPEG is intra-only and each frame is compressed
independently at the same quality. So **storage scales linearly with rate**, and halving the
rate halves the bytes. If 53 GB/hour is the obstacle, dropping to 30 Hz is the cheapest fix
available and costs nothing but temporal resolution.

**A rate the card does not advertise is rounded up, not down.** It offers 60, 50, 30, 20 and
10 at 1080p; asking for anything else gets the next one up:

```
requested  granted  measured
   45       50.0     49.67
   25       30.0     30.01
   15       20.0     20.00
  120       60.0     60.02
```

Rounding up is the unhelpful direction -- ask for 25 to save space and you get 30, a fifth
more data per hour than planned. The driver does report the coercion back, `Negotiated`
captures it, the manifest records `requested` and `granted` separately, and `vcap-record`
prints a note when they differ. Nothing here silently substitutes a rate, but it is worth
knowing the rounding goes that way before sizing a disk.

## Sustained capture drops about one frame in 750

Over a 4-minute run -- 14403 frames, 4.27 GB, 59.98 fps effective -- the driver lost **19
frames, 0.132%**, roughly one every 13 seconds. A 15-second run loses none, which is why
this went unnoticed at first: it is not visible at short durations.

The losses have a consistent shape:

```
 idx    t(s)   corrupt  gap  dropped
 2269    37.8    True   False    0      <- a damaged frame arrives
 2270    37.8    True   True     1      <- then exactly one frame is lost
 2271    37.8    False  True     1
```

Always exactly one frame, always immediately after a frame the JPEG check rejects. That
pattern -- damaged payload, then a discarded frame -- is what a USB transfer error looks
like from this side of the driver. It was **not** caused by segment rollover, the writer
queue, or the disk: rollover was clean (see below), and the queue peaked at 26 of 240 with
no overrun.

Plan for it rather than trying to eliminate it. At 60 Hz a lost frame is 17 ms, both
neighbours are flagged, and `Recording.flagged()` lists them -- so a dataset builder can
drop the affected pairs. If your task cannot tolerate that, capture at 30 Hz, where the
same loss rate costs half as much wall time and the card has more margin per frame.

## Segment rollover, verified on hardware

A 4-minute capture crossed the 2 GB boundary and produced two segments:

```
seg 0: session.mjpg      6883 frames  2.15 GB
seg 1: session.001.mjpg  7520 frames  2.12 GB
```

The boundary is seamless. The frame sequence is continuous across it (delta 1), the
interval across it is 16.68 ms -- exactly one frame period, so closing and fsyncing a 2 GB
file did not stall capture -- and byte offsets restart at 0 in the new segment as intended.
Reading the recording back presents all 14403 frames as one sequence, and a deep verify of
every frame took 0.7 s.

It is tempting to read that gap the other way and treat a 40 KB frame as a no-signal
detector. Do not: genuinely dark or static content compresses small too, so it is a hint
worth eyeballing and not a fact worth acting on. Nothing in this repo reports it as health.

Buffer depth made no difference to drops (tested at 4, 8 and 16 buffers). Doing per-frame
work inside the capture loop did: a measurement loop that parsed JPEG headers and updated a
set on every frame lost 2 frames in 600 and saw a 50 ms stall, while the same capture
recording to disk through `AsyncWriter` lost none in 901. That is the whole argument for
the writer thread.

The link negotiates SuperSpeed (`/sys/bus/usb/devices/*/speed` reads `5000`), so the
"USB 3.0" on the box is real. This matters because the **MS2109** -- the older, USB 2.0
chip sold in near-identical packaging, often also labelled 4K -- does not: it tops out far
below 1080p60 and is widely reported to misreport its own capabilities. If `lsusb` shows
`534d:2109` rather than `345f:2131`, expect 1080p30 and re-measure everything here.

## The descriptors overstate what is possible

The card advertises 15 geometries in **both** MJPEG and YUYV, with identical frame rates
for each. That cannot all be true. 1920x1080 at 60 Hz is about 12 MB/s in MJPEG and 249
MB/s in YUYV, and the descriptors do not distinguish them.

UVC descriptors are a list of modes the firmware will accept, not a promise that it can
sustain them. This is why `vcap-probe` negotiates and measures each mode instead of
reading `--list-formats-ext` and believing it, and why `Negotiated` records what the
driver granted rather than what was requested.

Prefer MJPEG regardless of bandwidth. It is what the card compresses to internally, so
asking for YUYV at high resolution asks it to do more work, not less.

## It emits frames with nothing plugged into the HDMI input

**This is the trap most likely to cost someone a data collection run.**

With no source connected, the card does not time out or error. It produces a valid MJPEG
stream at the full 60 fps, of a near-solid placeholder image (~37-41 KB per frame, eight
distinct byte values in the entropy-coded data). Every software-visible signal looks
healthy: frames arrive, timestamps advance, nothing is flagged, `vcap-verify` reports the
recording as clean.

So **"frames are arriving" is not evidence that a source is attached**, and no counter in
this repo can be made to prove otherwise -- which is why none of them claim to.
`vcap-probe` and `tests/hardware.sh` both say so explicitly rather than implying a healthy
stream means a healthy setup.

The only reliable check is to look at the image, with `vcap-view` or the card's HDMI
loop-out. Do that before a session, not after.

## The first frame after STREAMON is a fragment

Observed on 39 of 40 stream starts with live 1080p60 video on the input. The first buffer
holds a **partial frame missing its beginning**:

```
frame 0:  21992 bytes  head=2800a2800a28  tail=803fffd9
frame 1:  41109 bytes  head=ffd8ffdb0043  tail=803fffd9
frame 2:  41109 bytes  head=ffd8ffdb0043  tail=803fffd9

frame 0 shares its last 21991 bytes with frame 1 -- 100% of frame 0
```

So frame 0 is byte-for-byte the tail of the frame that was already in flight. There is no
start-of-image marker and no `SOF0`, so its geometry is unreadable, but it ends with a
perfectly valid `FFD9`.

**The cause is stream-start alignment, not signal loss.** The card transmits continuously
whenever it is clocked, and `VIDIOC_STREAMON` begins assembling from whatever USB payload
arrives next -- which is normally the middle of a frame. `uvcvideo` would ordinarily
discard such a fragment using the UVC frame-ID and end-of-frame bits; this card evidently
does not set them in a way that permits it.

Two consequences worth being clear about:

- **It is not specific to having no HDMI input.** Confirmed on 5 of 5 stream starts with
  live 1080p60 video on the input, each one headless with a valid end-of-image marker,
  exactly as with no input. The fragments are correspondingly larger there -- a partial
  frame of real content, 36-277 KB -- which is another reason not to infer anything from
  payload size alone.
- **It is not strictly guaranteed, and the fragment has two shapes.** Over 40 starts with
  a live source:

  | first frame | share |
  |---|---|
  | headless, valid end-of-image | 95% |
  | missing both ends -- a few-KB sliver | 2.5% |
  | clean, where the payload boundary lined up | 2.5% |

  The invariant worth relying on is that the fragment is **missing its head**, which holds
  for both broken shapes. Requiring a valid end-of-image marker as well is too strict:
  `tests/hardware.sh` did that at first and failed roughly one run in eight. A fragment
  that *has* a head and lacks its tail would be a different fault -- damage mid-frame
  rather than stream-start alignment -- and the test still fails on that.

Because the fragment is missing its header it cannot be decoded at all, which is a
stronger statement than "it looks odd". This repo still writes it, flagged with
`FLAG_CORRUPT`, rather than discarding it: the recorder throwing frames away silently is
the failure mode that matters more, and the frame's timestamp is real even though its
pixels are unusable. `vcap-view` skips flagged frames, and a dataset builder should filter
on `Recording.flagged()`.

`looks_like_jpeg()` therefore checks for a start-of-image marker as well as an
end-of-image one. Checking only the tail -- the intuitive choice, since a frame cut short
by signal loss loses its tail -- passes this case, which is the common one.

## Two device nodes, and the numbering moves

A UVC device registers a video node and a UVC payload-header metadata node. On the
development machine that is:

```
/dev/v4l/by-id/usb-MACROSILICON_USB3.0_Video_19283458-video-index0 -> /dev/video4
/dev/v4l/by-id/usb-MACROSILICON_USB3.0_Video_19283458-video-index1 -> /dev/video5
```

`video4` captures; `video5` reports `META_CAPTURE` and no pixel formats. Opening the wrong
one gets you a device that enumerates no modes.

Those numbers are not stable. They depend on what else enumerated first -- this laptop's
built-in camera already occupies `video0` through `video3` -- so they move when the card
is replugged or another camera is attached. **Never record or configure a bare
`/dev/videoN`.** `find_capture_card()` resolves through `by-id` and every manifest records
both the stable path and the node it resolved to at capture time.

The `by-id` name embeds a serial (`19283458`). Treat it as a device model identifier
rather than a unique one: this chipset family commonly ships many units with the same
firmware serial string, so two identical cards on one machine may produce colliding
`by-id` paths. That is untested here -- there is one card -- and if you add a second, check
before assuming `by-id` distinguishes them. `by-path` does, at the cost of being tied to
the physical port.

## Resolution follows the HDMI source, mid-stream

The card downscales 4K input but otherwise reports what its source is sending. When the
source changes output mode, the emitted image geometry can change **without** the V4L2
format changing underneath the capture loop, so a session can contain frames of two
different sizes while claiming one.

`vcap-verify` checks JPEG headers against the negotiated geometry for exactly this reason
and reports a mode change as a problem. It is also why the timestamp regression check
exists: `uvcvideo` interpolates its clock from the device's, and a renegotiation can reset
that interpolation, which makes alignment across the discontinuity wrong.

## Audio

The card exposes an ALSA device (`/proc/asound/Video`) carrying HDMI audio. This repo does
not touch it. The MS2109's audio path is well known to misreport its sample rate and
duplicate mono across channels; whether the MS2130 shares that is **not tested here**. If
you need audio, verify it independently and assume nothing from this repo's video
behaviour.

## Permissions

`/dev/video*` is `root:video`, but `systemd-logind` grants the seat's active user an ACL,
which is why capture works from a desktop session without adding anyone to the `video`
group:

```
crw-rw----+ 1 root video 81, 4 ... /dev/video4
          ^ the ACL
```

That ACL is tied to an active local session. It is **not** present for a systemd service,
an SSH login, or a cron job, all of which will fail to open the device. For headless or
service use, install the udev rule in `scripts/udev/` -- see `scripts/install_deps.sh`.

## Use the HDMI loop-out

The card has an HDMI pass-through. Drive the operator's monitor from it.

A human in the loop needs to see what they are doing with no perceptible delay, and
hardware pass-through adds none. Routing the operator's display through software instead
means a decode, a composite and a present -- tens of milliseconds at best, which is enough
to degrade the demonstrations being recorded. Doing it in hardware also removes any
requirement for this repo to provide a low-latency preview, which is why `vcap-view` is
free to be a convenience served over HTTP.
