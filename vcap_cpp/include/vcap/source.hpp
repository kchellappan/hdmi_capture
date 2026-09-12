// SPDX-License-Identifier: MIT
//
// The capture loop: mmap'd V4L2 buffers in, timestamped frames out.
//
// Does one job and refuses the rest: it hands over each frame with the kernel's
// timestamp attached and an honest account of what was lost. It does not decode, encode,
// convert, resample or drop. Dropping in particular is policy, and the two consumers this
// repo exists for want opposite policies -- a recorder must never drop, a live inference
// loop must always prefer the newest frame.
#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "vcap/device.hpp"
#include "vcap/frame.hpp"

namespace vcap {

// The device is streaming but produced no frame within the timeout. For an HDMI capture
// card this is the normal state when nothing is connected -- though note that the card
// this repo targets emits frames regardless, so its absence proves nothing about whether
// a source is attached. See docs/hardware.md.
struct NoSignal : std::runtime_error {
    explicit NoSignal(const std::string& what) : std::runtime_error(what) {}
};

// The device stopped being usable, typically unplugged mid-stream.
struct StreamError : std::runtime_error {
    explicit StreamError(const std::string& what) : std::runtime_error(what) {}
};

// What the driver actually granted, which is not always what was asked for. S_FMT is a
// negotiation and the driver may silently return something else, so this is what a
// manifest records -- never the request.
struct Negotiated {
    std::string pixelformat;
    std::uint32_t width = 0, height = 0, sizeimage = 0;
    double fps_requested = 0.0, fps_granted = 0.0;
    std::string timestamp_clock;   // "monotonic" if comparable with CLOCK_MONOTONIC
    std::string timestamp_source;  // "start-of-frame" or "end-of-frame"

    bool timestamps_are_monotonic() const { return timestamp_clock == "monotonic"; }
};

class VideoSource {
public:
    // `buffers` is how many kernel buffers are queued. Four covers a scheduling hiccup at
    // 60 Hz without adding latency a live consumer would feel: each buffer is one frame
    // period of slack. Measured on the target card, raising it changed nothing.
    VideoSource(Device device, std::string pixelformat = "MJPG",
                std::uint32_t width = 1920, std::uint32_t height = 1080,
                double fps = 60.0, unsigned buffers = 4);
    ~VideoSource();

    VideoSource(const VideoSource&) = delete;
    VideoSource& operator=(const VideoSource&) = delete;

    const Negotiated& open();
    void close();

    // One frame. Throws NoSignal on timeout, StreamError if the device went away. The
    // buffer is copied and requeued before returning, so the frame outlives the loop.
    Frame read(double timeout_seconds = 2.0);

    const Negotiated& negotiated() const { return negotiated_; }
    const Device& device() const { return device_; }
    unsigned buffer_count() const { return n_buffers_; }

    // Counters, restricted to things actually observed. Deliberately absent: any figure
    // for frames the card itself dropped before the kernel saw them. Nothing here can
    // know that, and a guess in a health readout is worse than a blank.
    std::uint64_t frames_yielded = 0;
    std::uint64_t driver_dropped = 0;
    std::uint64_t driver_errors = 0;
    std::uint64_t corrupt = 0;

private:
    void set_format();
    void set_rate();
    void request_buffers();
    void map_buffers();
    void queue(unsigned index);
    void stream_on();
    void read_timestamp_flags();

    Device device_;
    std::string pixelformat_;
    std::uint32_t width_, height_;
    double fps_;
    unsigned n_buffers_;

    int fd_ = -1;
    bool streaming_ = false;
    bool have_last_seq_ = false;
    std::uint32_t last_seq_ = 0;
    Negotiated negotiated_{};

    struct Mapping { void* start = nullptr; std::size_t length = 0; };
    std::vector<Mapping> maps_;
};

}  // namespace vcap
