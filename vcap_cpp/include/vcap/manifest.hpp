// SPDX-License-Identifier: MIT
//
// The manifest: everything about a recording that is not a frame.
//
// Only written here, never parsed. That asymmetry is the whole reason this client can
// stay dependency-free: emitting JSON is a hundred lines, parsing it is a library. The
// C++ side captures and the Python side reads, and the format is the interface between
// them -- see vcap_cpp/README.md.
//
// Written before the first frame, so a session killed by a crash or a power loss still
// describes itself, and rewritten at the end with the counters. A reader must tolerate
// the end-of-session fields being absent; that is what an unclean shutdown looks like.
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "vcap/device.hpp"
#include "vcap/source.hpp"
#include "vcap/writer.hpp"

namespace vcap {

// Bumped when a change would make an older reader misinterpret a newer recording. Adding
// a field does not count; changing the meaning of one does. Must track FORMAT_VERSION in
// vcap/manifest.py.
inline constexpr int kFormatVersion = 1;

struct ClockPair {
    std::int64_t monotonic_ns = 0;
    std::int64_t realtime_ns = 0;
    std::int64_t sample_spread_ns = 0;
};

// CLOCK_MONOTONIC and CLOCK_REALTIME sampled back to back.
//
// Frame timestamps are monotonic, which is what makes them comparable with control
// timestamps taken on the same machine, but monotonic time means nothing across a reboot
// and cannot be rendered as a date. Recording both once is what lets a monotonic stamp
// become wall clock afterwards -- and the spread between the two reads is recorded too,
// so a reader can see how precise the pairing is rather than trusting it.
ClockPair sample_clocks();

class Manifest {
public:
    Manifest(std::string session, const Device& device, const Negotiated& negotiated,
             std::string requested_pixelformat, std::uint32_t requested_width,
             std::uint32_t requested_height, double requested_fps);

    void set_note(const std::string& key, const std::string& value);

    // Write to <directory>/manifest.json atomically, so a reader never sees half of one.
    void write(const std::string& directory, const SegmentWriter& writer,
               bool finalized, const VideoSource* source = nullptr,
               std::size_t queue_high_water = 0, std::size_t queue_depth = 0) const;

private:
    std::string session_;
    const Device& device_;
    const Negotiated& negotiated_;
    std::string req_pixelformat_;
    std::uint32_t req_width_, req_height_;
    double req_fps_;
    ClockPair started_;
    std::vector<std::pair<std::string, std::string>> notes_;
};

}  // namespace vcap
