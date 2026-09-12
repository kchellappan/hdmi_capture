# On-disk format

A session is a directory:

```
run-04/
  manifest.json     what this recording is
  session.mjpg      the frames, exactly as the card produced them
  session.idx       one 32-byte record per frame
  session.001.mjpg  further segments, if it rolled over
  session.001.idx
```

## Why not MP4

The obvious alternative is a container, and it was rejected for two specific reasons.

**Timestamps.** A container quantises presentation times to a fixed timebase. The exact
per-frame capture instant is the one thing this repo exists to preserve, and putting it
through a container's rational timebase either rounds it or requires a sidecar to carry
the real values -- at which point the container is doing nothing the index does not.

**Access.** A dataloader wants frame *K* of an episode and the frame nearest a control
timestamp. With fixed-size index records, the first is one seek and the second a binary
search over a memory-mapped file. Getting either out of an MP4 means a demuxer.

The exports in `vcap_py/vcap/export/` produce MP4 or MKV for watching. Do not train from them: the
timestamps in them are approximations of the ones in the index.

## The stream file

Concatenated frames, byte for byte as the device delivered them. For MJPEG that is a
sequence of complete JPEGs; for YUYV, raw pixels at a fixed stride.

Nothing is added -- no header, no framing, no length prefixes. That is deliberate: if the
index is ever lost, an MJPEG stream can be recovered by scanning for `FFD8`/`FFD9` pairs,
and the file is already in the most portable form it could be in. It also means the
recorder does no work per frame beyond an append.

`ffplay -f mjpeg session.mjpg` will play one directly, at whatever rate it guesses.

## The index file

One record per frame, no header, little-endian, explicitly unaligned so the record is 32
bytes everywhere:

| offset | type | field | meaning |
|---|---|---|---|
| 0 | u64 | `byte_offset` | where the frame starts **in its own segment's** stream file |
| 8 | u32 | `length` | frame size in bytes |
| 12 | u64 | `ts_mono_ns` | kernel `CLOCK_MONOTONIC` timestamp; see [timebase.md](timebase.md) |
| 20 | u32 | `seq` | the driver's frame counter |
| 24 | u32 | `flags` | see below |
| 28 | u32 | `dropped_before` | frames the driver lost immediately before this one |

Frame *N* of a segment is therefore at byte *32N* of its index, which is what makes
`IndexReader` an `mmap` and a `struct.unpack_from` rather than a parser.

`byte_offset` is relative to the segment, not to the session. A `Recording` presents
segments as one continuous sequence and does that mapping itself.

### Flags

| bit | name | meaning |
|---|---|---|
| 0 | `FLAG_DRIVER_ERROR` | the driver marked the buffer as errored |
| 1 | `FLAG_CORRUPT` | the payload is not a well-formed complete JPEG |
| 2 | `FLAG_GAP_BEFORE` | the sequence counter jumped; `dropped_before` says by how much |

**Flagged frames are written, not discarded.** A recording with a silently removed frame
looks complete and is not, and the frame's timestamp is real even when its pixels are not.
Filtering is a consumer's decision: `Recording.flagged()` lists them and `Frame.ok` tests
one.

Expect exactly one flagged frame at the start of every session recorded from this card --
see [hardware.md](hardware.md) on the first frame after `STREAMON`.

## The manifest

JSON, written before the first frame and rewritten at the end. Written first so that a
session killed by a crash or a power loss still describes itself; a reader must therefore
tolerate the end-of-session fields being absent, which is what an unclean shutdown looks
like on disk. `vcap-verify` reports it and keeps going.

Fields worth knowing about:

- `requested` and `granted` are separate. `VIDIOC_S_FMT` is a negotiation and the driver
  may return something other than what was asked for, silently. Recording only the request
  would describe a recording that does not exist.
- `timebase` says which clock the frames are on and whether it can be compared with
  `time.monotonic_ns()`. Read from the driver's buffer flags, not assumed.
- `timebase.capture_offset_ns` is the display-to-USB latency, `null` until somebody
  measures it. See [timebase.md](timebase.md).
- `started` and `ended` each hold a monotonic/realtime pair and the spread between the two
  reads, which is how a monotonic frame timestamp becomes a wall-clock time.
- `effective_fps` is frames written over wall time elapsed. Measured, not requested.
- `writer.queue_high_water` is how close the disk came to not keeping up. If it approaches
  `queue_depth`, the next session may fail rather than degrade.
- `format_version` is 1, and is checked on read. It is bumped when a change would make an
  older reader misinterpret a newer recording -- adding a field does not qualify, changing
  the meaning of one does.

## Durability

Two different failures, handled separately.

**This process dying** -- SIGKILL, an OOM kill, an unhandled exception -- is covered
unconditionally: both files are flushed to the OS after every frame. That is not an fsync,
costs two syscalls, and measures as no change in frame rate or queue depth at 60 Hz.

It is there because the index is 32 bytes per frame against a 64 KB buffer, which is 2048
frames. Without the flush, a hard kill lost up to 34 seconds of index at 60 Hz while the
frames it described were already on disk -- the pixels would survive and their timestamps
would not, which for this repo is the wrong half to keep.

**The machine losing power** is what `--fsync-every` controls, and it is not free: an fsync
is a round trip to the device. The default of 0 leaves it to the OS, risking only what is
still in the page cache. Both files are append-only, so whole records before the cut
survive and the recording stays readable; `vcap-verify` reports the truncation.

A clean stop needs neither. SIGINT and SIGTERM are handled, the manifest is finalized, and
`vcap-record` exits with a complete recording.

## Segments

**The episode is the real unit, not the segment.** Whatever repo submodules this one
decides where a demonstration begins and ends, by starting and stopping a `Session`.
Size-based rollover is a guardrail underneath that, for the case where one recording gets
genuinely large.

So the default is 16 GB — about **seventeen minutes** of 1080p60 — which is longer than
any demonstration episode is likely to be. In practice one episode is one file and the
split never appears. `--segment-mb 0` disables it entirely.

It exists at all because exFAT on an external drive refuses anything over 4 GB, a failed
copy of a 60 GB file means restarting the whole transfer, and one corrupt region would
otherwise cost the whole recording.

A reader never sees segments: `Recording` presents them as one continuous sequence, so a
conversion script asking for frame *N* or the frame at timestamp *T* is unaffected by where
the boundaries fall.

That abstraction is worth being slightly wary of. The only bug this feature has caused —
index entries numbered per file rather than per session, so an index from `flagged()`
fetched a frame two minutes away — was invisible in every single-segment recording. At the
previous 2 GB default a segment was two and a half minutes, so almost every real session
was multi-segment and the bug was always live; at 16 GB it is the rare case, which is
safer but also means the multi-segment path gets exercised less. `vcap-verify` and the
round-trip tests both check it deliberately for that reason.

## Sizing

Measured at 1080p60 on live video from a laptop's HDMI output:

| | frame | rate | per hour |
|---|---|---|---|
| Live video content | 210-345 KB | **14.5-19 MB/s** | **52-69 GB** |
| The no-signal placeholder | ~40 KB | 2.5 MB/s | 8.8 GB |

**Plan against the first row.** The placeholder figure is what this card produces with
nothing connected to its HDMI input, it is about six times smaller, and an earlier version
of this document quoted it as though it were a capture rate. It is not: it is the cost of
recording a static blue screen.

MJPEG is intra-only, so the rate scales with how much detail is in each frame rather than
with motion. Expect the high end of that range for anything busy, and note that a policy
collection run of a hundred two-minute episodes is on the order of 170 GB.

A 2 GB segment is therefore about **two and a half minutes**, not the fourteen this
document previously claimed.

If that is too much to keep, `vcap_py/vcap/export/to_mp4.py` re-encodes to H.264 at a large
saving. Archive with it; do not train from it.
