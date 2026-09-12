// SPDX-License-Identifier: MIT
#include "vcap/source.hpp"

#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <cmath>
#include <cstring>

// The kernel renamed V4L2_BUF_FLAG_TSTAMP_SRC_SOF to ..._SOE ("start of exposure") for
// the benefit of camera sensors, keeping the value 0x00010000. A capture card has no
// exposure -- for UVC this bit means the timestamp came from the USB start-of-frame
// counter -- so the string this client reports stays "start-of-frame", matching what the
// Python client writes into a manifest. The two must agree: a manifest is read by
// whichever client did not write it.
//
// Worth noting that the Python client is immune to this particular churn, because it
// hardcodes the value rather than the name.
#ifndef V4L2_BUF_FLAG_TSTAMP_SRC_SOF
#define V4L2_BUF_FLAG_TSTAMP_SRC_SOF V4L2_BUF_FLAG_TSTAMP_SRC_SOE
#endif

namespace vcap {
namespace {

std::uint32_t fourcc_to_int(const std::string& code) {
    std::string c = code;
    c.resize(4, ' ');
    return std::uint32_t(std::uint8_t(c[0])) | (std::uint32_t(std::uint8_t(c[1])) << 8) |
           (std::uint32_t(std::uint8_t(c[2])) << 16) | (std::uint32_t(std::uint8_t(c[3])) << 24);
}

std::string fourcc_to_string(std::uint32_t v) {
    std::string s(4, ' ');
    for (int i = 0; i < 4; ++i) s[i] = char((v >> (8 * i)) & 0xFF);
    return s;
}

// Retry on EINTR. A signal arriving mid-ioctl is not an error, and treating it as one
// makes a recorder die when the shell resizes.
int xioctl(int fd, unsigned long request, void* arg) {
    int r;
    do { r = ::ioctl(fd, request, arg); } while (r < 0 && errno == EINTR);
    return r;
}

}  // namespace

VideoSource::VideoSource(Device device, std::string pixelformat, std::uint32_t width,
                         std::uint32_t height, double fps, unsigned buffers)
    : device_(std::move(device)), pixelformat_(std::move(pixelformat)), width_(width),
      height_(height), fps_(fps), n_buffers_(buffers) {}

VideoSource::~VideoSource() { close(); }

const Negotiated& VideoSource::open() {
    fd_ = ::open(device_.node.c_str(), O_RDWR | O_NONBLOCK);
    if (fd_ < 0) throw StreamError("cannot open " + device_.node + ": " + std::strerror(errno));
    try {
        set_format();
        set_rate();
        request_buffers();
        map_buffers();
        for (unsigned i = 0; i < n_buffers_; ++i) queue(i);
        stream_on();
        read_timestamp_flags();
    } catch (...) {
        close();
        throw;
    }
    return negotiated_;
}

void VideoSource::set_format() {
    v4l2_format fmt{};
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width = width_;
    fmt.fmt.pix.height = height_;
    fmt.fmt.pix.pixelformat = fourcc_to_int(pixelformat_);
    fmt.fmt.pix.field = V4L2_FIELD_NONE;
    if (xioctl(fd_, VIDIOC_S_FMT, &fmt) < 0) {
        throw StreamError(std::string("VIDIOC_S_FMT: ") + std::strerror(errno));
    }
    negotiated_.pixelformat = fourcc_to_string(fmt.fmt.pix.pixelformat);
    negotiated_.width = fmt.fmt.pix.width;
    negotiated_.height = fmt.fmt.pix.height;
    negotiated_.sizeimage = fmt.fmt.pix.sizeimage;
    negotiated_.fps_requested = fps_;
}

void VideoSource::set_rate() {
    if (fps_ <= 0) return;
    v4l2_streamparm parm{};
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(fd_, VIDIOC_G_PARM, &parm) < 0) return;  // advisory; some devices refuse
    parm.parm.capture.timeperframe.numerator = 1;
    parm.parm.capture.timeperframe.denominator = static_cast<std::uint32_t>(std::lround(fps_));
    if (xioctl(fd_, VIDIOC_S_PARM, &parm) < 0) return;
    const auto& tpf = parm.parm.capture.timeperframe;
    if (tpf.numerator) negotiated_.fps_granted = double(tpf.denominator) / tpf.numerator;
}

void VideoSource::request_buffers() {
    v4l2_requestbuffers req{};
    req.count = n_buffers_;
    req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (xioctl(fd_, VIDIOC_REQBUFS, &req) < 0) {
        throw StreamError(std::string("VIDIOC_REQBUFS: ") + std::strerror(errno));
    }
    if (req.count < 1) throw StreamError("driver granted no buffers");
    // The driver may grant fewer than asked; the surplus indices are not valid.
    n_buffers_ = req.count;
}

void VideoSource::map_buffers() {
    maps_.resize(n_buffers_);
    for (unsigned i = 0; i < n_buffers_; ++i) {
        v4l2_buffer buf{};
        buf.index = i;
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (xioctl(fd_, VIDIOC_QUERYBUF, &buf) < 0) {
            throw StreamError(std::string("VIDIOC_QUERYBUF: ") + std::strerror(errno));
        }
        void* p = ::mmap(nullptr, buf.length, PROT_READ, MAP_SHARED, fd_, buf.m.offset);
        if (p == MAP_FAILED) throw StreamError(std::string("mmap: ") + std::strerror(errno));
        maps_[i] = {p, buf.length};
    }
}

void VideoSource::queue(unsigned index) {
    v4l2_buffer buf{};
    buf.index = index;
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    if (xioctl(fd_, VIDIOC_QBUF, &buf) < 0) {
        throw StreamError(std::string("VIDIOC_QBUF: ") + std::strerror(errno));
    }
}

void VideoSource::stream_on() {
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (xioctl(fd_, VIDIOC_STREAMON, &type) < 0) {
        throw StreamError(std::string("VIDIOC_STREAMON: ") + std::strerror(errno));
    }
    streaming_ = true;
}

void VideoSource::read_timestamp_flags() {
    // Read before the first frame, so a recording can state its own time semantics even
    // if it captures nothing. The timestamp-source bits are set by the driver at setup
    // rather than per frame.
    v4l2_buffer buf{};
    buf.index = 0;
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    if (xioctl(fd_, VIDIOC_QUERYBUF, &buf) < 0) {
        negotiated_.timestamp_clock = "unknown";
        negotiated_.timestamp_source = "unknown";
        return;
    }
    switch (buf.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) {
        case V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC: negotiated_.timestamp_clock = "monotonic"; break;
        case V4L2_BUF_FLAG_TIMESTAMP_COPY:      negotiated_.timestamp_clock = "copy"; break;
        case V4L2_BUF_FLAG_TIMESTAMP_UNKNOWN:   negotiated_.timestamp_clock = "unknown"; break;
        default:                                negotiated_.timestamp_clock = "unrecognised"; break;
    }
    switch (buf.flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK) {
        case V4L2_BUF_FLAG_TSTAMP_SRC_SOF: negotiated_.timestamp_source = "start-of-frame"; break;
        case V4L2_BUF_FLAG_TSTAMP_SRC_EOF: negotiated_.timestamp_source = "end-of-frame"; break;
        default:                           negotiated_.timestamp_source = "unrecognised"; break;
    }
}

void VideoSource::close() {
    if (streaming_) {
        int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        xioctl(fd_, VIDIOC_STREAMOFF, &type);  // already gone is fine; nothing to salvage
        streaming_ = false;
    }
    for (auto& m : maps_) {
        if (m.start) ::munmap(m.start, m.length);
    }
    maps_.clear();
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
}

Frame VideoSource::read(double timeout_seconds) {
    if (!streaming_) throw StreamError("not streaming");

    fd_set fds;
    FD_ZERO(&fds);
    FD_SET(fd_, &fds);
    timeval tv{};
    tv.tv_sec = static_cast<time_t>(timeout_seconds);
    tv.tv_usec = static_cast<suseconds_t>((timeout_seconds - tv.tv_sec) * 1e6);

    int r;
    do { r = ::select(fd_ + 1, &fds, nullptr, nullptr, &tv); } while (r < 0 && errno == EINTR);
    if (r < 0) throw StreamError(std::string("select: ") + std::strerror(errno));
    if (r == 0) {
        throw NoSignal("no frame within the timeout -- nothing connected to the capture "
                       "card's input, or the source is off");
    }

    v4l2_buffer buf{};
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    if (xioctl(fd_, VIDIOC_DQBUF, &buf) < 0) {
        // select() said readable but nothing was ready: a race, not a fault.
        if (errno == EAGAIN) throw NoSignal("device reported ready then had nothing");
        if (errno == ENODEV || errno == EIO) {
            throw StreamError(std::string("device went away mid-stream: ") + std::strerror(errno));
        }
        throw StreamError(std::string("VIDIOC_DQBUF: ") + std::strerror(errno));
    }

    Frame frame;
    if (buf.flags & V4L2_BUF_FLAG_ERROR) { frame.flags |= kFlagDriverError; ++driver_errors; }

    const auto* src = static_cast<const std::uint8_t*>(maps_[buf.index].start);
    frame.data.assign(src, src + buf.bytesused);
    frame.ts_mono_ns = std::int64_t(buf.timestamp.tv_sec) * 1000000000LL +
                       std::int64_t(buf.timestamp.tv_usec) * 1000LL;
    frame.seq = buf.sequence;

    if (have_last_seq_ && buf.sequence > last_seq_ + 1) {
        frame.dropped_before = buf.sequence - last_seq_ - 1;
        frame.flags |= kFlagGapBefore;
        driver_dropped += frame.dropped_before;
    }
    have_last_seq_ = true;
    last_seq_ = buf.sequence;

    // Only meaningful for MJPEG. For raw formats the driver pads a short frame to
    // sizeimage rather than reporting it, so there is no signal to test.
    if (negotiated_.pixelformat == "MJPG" && !looks_like_jpeg(frame.data)) {
        frame.flags |= kFlagCorrupt;
        ++corrupt;
    }

    queue(buf.index);
    ++frames_yielded;
    return frame;
}

}  // namespace vcap
