// SPDX-License-Identifier: MIT
#include "vcap/writer.hpp"

#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

#include "vcap/index.hpp"

namespace vcap {
namespace {

void make_dirs(const std::string& path) {
    std::string acc;
    for (std::size_t i = 0; i <= path.size(); ++i) {
        if (i == path.size() || path[i] == '/') {
            if (!acc.empty() && ::mkdir(acc.c_str(), 0777) < 0 && errno != EEXIST) {
                throw std::runtime_error("mkdir " + acc + ": " + std::strerror(errno));
            }
        }
        if (i < path.size()) acc.push_back(path[i]);
    }
}

std::string zero_pad(unsigned n, int width) {
    std::string s = std::to_string(n);
    while (int(s.size()) < width) s.insert(s.begin(), '0');
    return s;
}

}  // namespace

SegmentWriter::SegmentWriter(std::string directory, std::string name,
                             std::uint64_t segment_bytes, unsigned fsync_every)
    : directory_(std::move(directory)), name_(std::move(name)),
      segment_bytes_(segment_bytes), fsync_every_(fsync_every) {
    make_dirs(directory_);
    roll();
}

SegmentWriter::~SegmentWriter() { close(); }

void SegmentWriter::roll() {
    close_segment();
    ++segment_no_;
    const std::string stem = segment_no_ == 0
        ? name_ : name_ + "." + zero_pad(unsigned(segment_no_), 3);
    const std::string stream_path = directory_ + "/" + stem + ".mjpg";
    const std::string index_path  = directory_ + "/" + stem + ".idx";

    stream_ = std::fopen(stream_path.c_str(), "wb");
    index_  = std::fopen(index_path.c_str(), "wb");
    if (!stream_ || !index_) {
        throw std::runtime_error("cannot create segment in " + directory_ + ": " +
                                 std::strerror(errno));
    }
    segment_offset_ = 0;

    SegmentInfo info;
    info.number = unsigned(segment_no_);
    info.stream = stem + ".mjpg";
    info.index  = stem + ".idx";
    segments_.push_back(info);
}

void SegmentWriter::close_segment() {
    for (std::FILE** f : {&stream_, &index_}) {
        if (*f) {
            std::fflush(*f);
            ::fsync(::fileno(*f));
            std::fclose(*f);
            *f = nullptr;
        }
    }
}

void SegmentWriter::write(const Frame& frame) {
    if (segment_bytes_ && segment_offset_ + frame.size() > segment_bytes_) roll();

    if (std::fwrite(frame.data.data(), 1, frame.data.size(), stream_) != frame.data.size()) {
        throw std::runtime_error(std::string("writing frame: ") + std::strerror(errno));
    }
    std::uint8_t record[kIndexRecordSize];
    pack_index_record(record, segment_offset_, std::uint32_t(frame.size()),
                      frame.ts_mono_ns, frame.seq, frame.flags, frame.dropped_before);
    if (std::fwrite(record, 1, sizeof record, index_) != sizeof record) {
        throw std::runtime_error(std::string("writing index: ") + std::strerror(errno));
    }

    SegmentInfo& seg = segments_.back();
    if (!seg.has_frames) { seg.first_ts_mono_ns = frame.ts_mono_ns; seg.has_frames = true; }
    seg.last_ts_mono_ns = frame.ts_mono_ns;
    ++seg.frames;
    seg.bytes += frame.size();

    segment_offset_ += frame.size();
    ++frames_written;
    bytes_written += frame.size();
    if (frame.flags) ++frames_flagged;

    // Push both files to the OS after every frame. Not an fsync -- no platter is touched
    // -- it only moves bytes out of this process's buffers into the page cache.
    //
    // It matters because the index is 32 bytes per frame against stdio's buffer: without
    // this, a process killed with SIGKILL loses tens of seconds of index while the frames
    // it describes are already on disk. The pixels would survive and their timestamps
    // would not, which is the wrong half to keep. The stream is flushed first, so the
    // index never references bytes the OS has not seen.
    std::fflush(stream_);
    std::fflush(index_);

    // Surviving the machine losing power is a different question, and fsync is not free:
    // it is a round trip to the device. Zero leaves it to the OS.
    if (fsync_every_ && ++since_sync_ >= fsync_every_) {
        ::fsync(::fileno(stream_));
        ::fsync(::fileno(index_));
        since_sync_ = 0;
    }
}

void SegmentWriter::close() { close_segment(); }

AsyncWriter::AsyncWriter(SegmentWriter& writer, std::size_t depth)
    : writer_(writer), depth_(depth), thread_([this] { run(); }) {}

AsyncWriter::~AsyncWriter() {
    try { close(); } catch (...) {}  // a destructor must not throw
}

void AsyncWriter::run() {
    for (;;) {
        Frame frame;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            not_empty_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
            if (queue_.empty()) return;  // stopping and drained
            frame = std::move(queue_.front());
            queue_.pop_front();
        }
        try {
            writer_.write(frame);
        } catch (const std::exception& e) {
            std::lock_guard<std::mutex> lock(mutex_);
            error_ = e.what();
            stopping_ = true;
            return;
        }
    }
}

void AsyncWriter::write(Frame frame) {
    std::unique_lock<std::mutex> lock(mutex_);
    if (!error_.empty()) throw std::runtime_error(error_);
    if (queue_.size() > high_water_) high_water_ = queue_.size();
    if (queue_.size() >= depth_) {
        throw WriterOverrun("write queue full (" + std::to_string(depth_) +
                            " frames); the disk is not keeping up with capture");
    }
    queue_.push_back(std::move(frame));
    lock.unlock();
    not_empty_.notify_one();
}

void AsyncWriter::close() {
    if (closed_) return;
    closed_ = true;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
    }
    not_empty_.notify_all();
    // Drains rather than discards: frames already accepted exist only in this queue, so
    // dropping them at shutdown would lose the end of every session.
    if (thread_.joinable()) thread_.join();
    writer_.close();
    if (!error_.empty()) throw std::runtime_error(error_);
}

}  // namespace vcap
