# vcap_cpp

Capture. Records an HDMI source to the same `.mjpg`/`.idx`/`manifest.json` triple the
Python client reads, with the kernel's per-frame `CLOCK_MONOTONIC` timestamp carried
through untouched.

```bash
./build.sh
./build/vcap-record-cpp data/run-01 -d MACROSILICON --fps 60 --seconds 30
```

```cpp
#include "vcap/device.hpp"
#include "vcap/source.hpp"
#include "vcap/writer.hpp"

auto device = vcap::find_capture_card("MACROSILICON");
vcap::VideoSource source(device, "MJPG", 1920, 1080, 60.0);
const auto& negotiated = source.open();

vcap::SegmentWriter writer("data/run-01");
vcap::AsyncWriter async(writer);
while (recording) {
    async.write(source.read());
}
```

## Capture only, on purpose

This client captures and writes. It does not read recordings back — Python does that, and
anything written here is readable there.

That split is the reason it needs no dependencies. Emitting JSON for a manifest is a
hundred lines; parsing it is a library. Since nothing here ever reads a manifest, the
question never arises, and the whole client links nothing but `pthread`.

If a C++ process needs to *read* a dataset, that is a real gap — say so rather than
bolting a JSON parser onto this.

## The format is the interface

The two clients agree on bytes, not on code. A recording written here has to satisfy a
reader that lives in another language and never sees this source, so that agreement is
tested rather than assumed:

| | |
|---|---|
| `tests/test_index_format.cpp` | The 32-byte index record, pinned against bytes Python actually produced. |
| `tests/roundtrip_synthetic.sh` | C++ writes a recording from synthetic frames, Python reads and verifies it. No hardware, runs in CI. |
| `tests/roundtrip.sh` | The same against the real card, including segment rollover. |

`tests/roundtrip_synthetic.sh` is the one that runs anywhere, and it is deliberately the
more thorough of the two: it checks the first-frame fragment is flagged, that an injected
gap survives into the index, that entry indices are numbered across the session rather
than per segment, and that every frame is exactly the bytes its index record claims.

Run all of it with `./tests/run_tests.sh`.

## Differences from the Python client

**The ABI comes from the kernel.** `vcap_py/vcap/v4l2.py` hand-rolls every V4L2 structure and
ioctl number in `ctypes`, and pins them with a test, because Python has no way to include
a C header. This client just includes `<linux/videodev2.h>`. There is nothing to get wrong
and nothing to pin.

One wrinkle that direction does introduce: the kernel renamed
`V4L2_BUF_FLAG_TSTAMP_SRC_SOF` to `..._SOE` and kept its value, so a source file naming
the old constant stops compiling on a newer header while the Python client — which
hardcodes `0x00010000` — is unaffected. `src/source.cpp` handles both spellings and
reports the same string either way, because a manifest is read by whichever client did not
write it.

**The index record is written field by field.** The obvious translation of Python's
`struct.Struct("<QIQIII")` is a packed struct, which is avoided here: a packed struct
depends on a compiler extension for its layout and on the host being little-endian for its
contents, and neither dependency announces itself when it breaks.

Everything else mirrors `vcap_py/vcap/` closely and on purpose, so that when one of them
misbehaves the two are comparable.

## Conventions worth keeping

The ones that are load-bearing rather than stylistic — see the repo's `CLAUDE.md`:

- **The timestamp is never recomputed.** It comes from `v4l2_buffer.timestamp` and reaches
  the index unchanged.
- **Requested and granted are separate facts.** `S_FMT` is a negotiation; the manifest
  records both, and `vcap-record-cpp` prints a note when they differ.
- **Flag, do not discard.** The first frame of nearly every stream from this card is an
  undecodable fragment. It is written with `kFlagCorrupt` rather than dropped, because a
  recording with a silently removed frame looks complete and is not.
- **Never report health you cannot verify.** `VideoSource` exposes no figure for frames the
  card dropped before the kernel saw them, because nothing here can know that.

## Building

`./build.sh` uses cmake when it is present and falls back to a direct `g++` call when it is
not. The fallback is load-bearing: it is what allows a build on a freshly imaged machine
that has a compiler but no cmake. `USE_CMAKE=0 ./build.sh` exercises it deliberately.

Requires a C++17 compiler and Linux kernel headers. Nothing else.
