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

The exports in `vcap/export/` produce MP4 or MKV for watching. Do not train from them: the
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

## Segments

Rollover defaults to 2 GB. It exists because a single file for a long session cannot be
moved off the machine until the session ends, is refused outright by filesystems with a
4 GB limit, and loses the whole recording to one corrupt region.

At 1080p60 MJPEG, ~40 KB a frame, a segment is about fourteen minutes. `--segment-mb 0`
disables rollover.

## Sizing

Roughly, from measurement: **2.5 MB/s, 8.8 GB/hour** at 1080p60 with this card's
compression on static content. Busy video compresses worse; MJPEG is intra-only, so the
figure scales with image complexity rather than with motion.

If that is too much to keep, `vcap/export/to_mp4.py` re-encodes to H.264 at a large
saving. Archive with it; do not train from it.
