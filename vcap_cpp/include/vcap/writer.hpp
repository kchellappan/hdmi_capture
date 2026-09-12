// SPDX-License-Identifier: MIT
//
// Writing a recording to disk in the format vcap/index.py and vcap/reader.py read.
//
// The recorder's contract is the opposite of a live viewer's: a missing frame corrupts an
// episode, so this never discards one on purpose. When the disk cannot keep up the queue
// fills and the writer says so loudly rather than quietly skipping -- a recording with an
// unannounced hole looks fine and trains badly.
#pragma once

#include <condition_variable>
#include <cstdio>
#include <deque>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "vcap/frame.hpp"

namespace vcap {

struct WriterOverrun : std::runtime_error {
    explicit WriterOverrun(const std::string& what) : std::runtime_error(what) {}
};

struct SegmentInfo {
    unsigned number = 0;
    std::string stream, index;
    std::uint64_t frames = 0, bytes = 0;
    std::int64_t first_ts_mono_ns = 0, last_ts_mono_ns = 0;
    bool has_frames = false;
};

class SegmentWriter {
public:
    // Rollover exists because one file for a long session cannot be moved off the machine
    // until the session ends, is refused outright by filesystems with a 4 GB limit, and
    // loses the whole recording to one corrupt region. At the measured 14.5-19 MB/s a
    // 2 GB segment is about two and a half minutes. Zero disables rollover.
    static constexpr std::uint64_t kDefaultSegmentBytes = 2ull * 1024 * 1024 * 1024;

    SegmentWriter(std::string directory, std::string name = "session",
                  std::uint64_t segment_bytes = kDefaultSegmentBytes,
                  unsigned fsync_every = 0);
    ~SegmentWriter();

    SegmentWriter(const SegmentWriter&) = delete;
    SegmentWriter& operator=(const SegmentWriter&) = delete;

    void write(const Frame& frame);
    void close();

    const std::vector<SegmentInfo>& segments() const { return segments_; }
    std::uint64_t frames_written = 0, bytes_written = 0, frames_flagged = 0;

private:
    void roll();
    void close_segment();

    std::string directory_, name_;
    std::uint64_t segment_bytes_;
    unsigned fsync_every_, since_sync_ = 0;

    std::FILE* stream_ = nullptr;
    std::FILE* index_ = nullptr;
    int segment_no_ = -1;
    std::uint64_t segment_offset_ = 0;
    std::vector<SegmentInfo> segments_;
};

// A SegmentWriter on its own thread, so a disk hiccup does not stall capture.
//
// The capture loop must return to DQBUF promptly: there are only as many kernel buffers as
// were requested, and once they fill the driver starts dropping. A 300 KB write usually
// returns in microseconds, but a filesystem flush or a full page cache can block for tens
// of milliseconds, which at 60 Hz exhausts four buffers.
//
// The queue is bounded. An unbounded one does not solve a slow disk, it converts it into
// unbounded memory growth and then an OOM kill partway through a session.
class AsyncWriter {
public:
    explicit AsyncWriter(SegmentWriter& writer, std::size_t depth = 240);
    ~AsyncWriter();

    AsyncWriter(const AsyncWriter&) = delete;
    AsyncWriter& operator=(const AsyncWriter&) = delete;

    void write(Frame frame);
    void close();

    std::size_t high_water() const { return high_water_; }

private:
    void run();

    SegmentWriter& writer_;
    std::size_t depth_, high_water_ = 0;
    std::deque<Frame> queue_;
    std::mutex mutex_;
    std::condition_variable not_empty_;
    bool stopping_ = false, closed_ = false;
    std::string error_;
    std::thread thread_;
};

}  // namespace vcap
