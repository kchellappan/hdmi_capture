// SPDX-License-Identifier: MIT
//
// What a captured frame is. Mirrors vcap/frame.py -- deliberately, because the two
// halves of this repo have to agree on what they mean by a frame even though only one of
// them is doing the capturing at any moment.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace vcap {

// Flag bits, identical to vcap/frame.py. These end up in the index on disk, so they are
// part of the file format and not an implementation detail: changing a value here
// silently changes the meaning of every recording ever written.
inline constexpr std::uint32_t kFlagDriverError = 1u << 0;
inline constexpr std::uint32_t kFlagCorrupt     = 1u << 1;
inline constexpr std::uint32_t kFlagGapBefore   = 1u << 2;

struct Frame {
    // The payload exactly as the device produced it. Copied out of the mmap'd buffer
    // before that buffer is requeued, for the same reason the Python side copies: the
    // driver is free to overwrite it the moment it is handed back.
    std::vector<std::uint8_t> data;

    // The kernel's timestamp for this buffer, on CLOCK_MONOTONIC, in nanoseconds.
    // Never recomputed, never adjusted. See docs/timebase.md.
    std::int64_t ts_mono_ns = 0;

    std::uint32_t seq = 0;
    std::uint32_t flags = 0;
    std::uint32_t dropped_before = 0;

    bool ok() const { return (flags & (kFlagDriverError | kFlagCorrupt)) == 0; }
    std::size_t size() const { return data.size(); }
};

// Whether the payload is a whole JPEG: a start-of-image marker *and* an end-of-image
// marker. Both ends, because fragments occur at both ends for different reasons -- see
// the long note in vcap/frame.py. On this project's card the first frame of nearly every
// stream is missing its head, so a tail-only check would pass the common case.
bool looks_like_jpeg(const std::uint8_t* data, std::size_t len);

inline bool looks_like_jpeg(const std::vector<std::uint8_t>& d) {
    return looks_like_jpeg(d.data(), d.size());
}

// Width and height from a JPEG's start-of-frame header, without decoding it. Returns
// false if the data is not a JPEG or is truncated before the header -- which is exactly
// how the stream-start fragment presents.
bool jpeg_dimensions(const std::uint8_t* data, std::size_t len,
                     std::uint32_t* width, std::uint32_t* height);

}  // namespace vcap
