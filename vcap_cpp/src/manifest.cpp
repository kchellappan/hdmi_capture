// SPDX-License-Identifier: MIT
#include "vcap/manifest.hpp"

#include <sys/utsname.h>
#include <unistd.h>

#include <cstdio>
#include <ctime>
#include <sstream>
#include <stdexcept>

namespace vcap {
namespace {

std::string escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (const char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
        }
    }
    return out;
}

std::string quoted(const std::string& s) { return "\"" + escape(s) + "\""; }

// JSON has no way to say "a number, but printed the way Python would". Frame rates are
// the only place it matters, since a manifest is compared by eye against one the Python
// client wrote.
std::string number(double v) {
    std::ostringstream os;
    os.precision(10);
    os << v;
    return os.str();
}

std::string hostname() {
    char buf[256];
    if (::gethostname(buf, sizeof buf) != 0) return "";
    buf[sizeof buf - 1] = '\0';
    return buf;
}

std::string kernel_release() {
    utsname u{};
    if (::uname(&u) != 0) return "";
    return u.release;
}

}  // namespace

ClockPair sample_clocks() {
    timespec mono{}, real{}, mono_after{};
    ::clock_gettime(CLOCK_MONOTONIC, &mono);
    ::clock_gettime(CLOCK_REALTIME, &real);
    ::clock_gettime(CLOCK_MONOTONIC, &mono_after);

    ClockPair p;
    p.monotonic_ns = std::int64_t(mono.tv_sec) * 1000000000LL + mono.tv_nsec;
    p.realtime_ns  = std::int64_t(real.tv_sec) * 1000000000LL + real.tv_nsec;
    p.sample_spread_ns =
        (std::int64_t(mono_after.tv_sec) * 1000000000LL + mono_after.tv_nsec) - p.monotonic_ns;
    return p;
}

Manifest::Manifest(std::string session, const Device& device, const Negotiated& negotiated,
                   std::string requested_pixelformat, std::uint32_t requested_width,
                   std::uint32_t requested_height, double requested_fps)
    : session_(std::move(session)), device_(device), negotiated_(negotiated),
      req_pixelformat_(std::move(requested_pixelformat)), req_width_(requested_width),
      req_height_(requested_height), req_fps_(requested_fps),
      started_(sample_clocks()) {}

void Manifest::set_note(const std::string& key, const std::string& value) {
    notes_.emplace_back(key, value);
}

void Manifest::write(const std::string& directory, const SegmentWriter& writer,
                     bool finalized, const VideoSource* source,
                     std::size_t queue_high_water, std::size_t queue_depth) const {
    std::ostringstream j;
    j << "{\n";
    j << "  \"format_version\": " << kFormatVersion << ",\n";
    j << "  \"session\": " << quoted(session_) << ",\n";
    j << "  \"written_by\": \"vcap_cpp\",\n";

    const auto& segs = writer.segments();
    j << "  \"files\": {\n";
    j << "    \"stream\": " << quoted(segs.empty() ? "" : segs.front().stream) << ",\n";
    j << "    \"index\": " << quoted(segs.empty() ? "" : segs.front().index) << "\n";
    j << "  },\n";

    const UsbInfo usb = device_.usb();
    j << "  \"device\": {\n";
    j << "    \"path\": " << quoted(device_.path) << ",\n";
    j << "    \"node_at_capture\": " << quoted(device_.node) << ",\n";
    j << "    \"driver\": " << quoted(device_.driver) << ",\n";
    j << "    \"card\": " << quoted(device_.card) << ",\n";
    j << "    \"bus_info\": " << quoted(device_.bus_info) << ",\n";
    if (usb.present) {
        j << "    \"usb\": {\n";
        j << "      \"vendor_id\": " << quoted(usb.vendor_id) << ",\n";
        j << "      \"product_id\": " << quoted(usb.product_id) << ",\n";
        j << "      \"vendor\": " << quoted(usb.vendor) << ",\n";
        j << "      \"product\": " << quoted(usb.product) << ",\n";
        j << "      \"serial\": " << quoted(usb.serial) << ",\n";
        j << "      \"speed_mbps\": " << quoted(usb.speed_mbps) << "\n";
        j << "    }\n";
    } else {
        j << "    \"usb\": null\n";
    }
    j << "  },\n";

    // Requested and granted, separately. S_FMT is a negotiation and the driver may return
    // something else; recording only the request would describe a recording that does not
    // exist.
    j << "  \"requested\": {\n";
    j << "    \"pixelformat\": " << quoted(req_pixelformat_) << ",\n";
    j << "    \"width\": " << req_width_ << ",\n";
    j << "    \"height\": " << req_height_ << ",\n";
    j << "    \"fps\": " << number(req_fps_) << "\n";
    j << "  },\n";
    j << "  \"granted\": {\n";
    j << "    \"pixelformat\": " << quoted(negotiated_.pixelformat) << ",\n";
    j << "    \"width\": " << negotiated_.width << ",\n";
    j << "    \"height\": " << negotiated_.height << ",\n";
    j << "    \"fps\": " << number(negotiated_.fps_granted) << "\n";
    j << "  },\n";

    j << "  \"timebase\": {\n";
    j << "    \"clock\": " << quoted(negotiated_.timestamp_clock) << ",\n";
    j << "    \"source\": " << quoted(negotiated_.timestamp_source) << ",\n";
    j << "    \"comparable_with_monotonic_ns\": "
      << (negotiated_.timestamps_are_monotonic() ? "true" : "false") << ",\n";
    // The offset between a frame's timestamp and the instant photons left the source.
    // Unknown unless someone measured it; never guessed, because a guessed offset folded
    // into timestamps cannot later be told apart from a measured one.
    j << "    \"capture_offset_ns\": null,\n";
    j << "    \"capture_offset_method\": null\n";
    j << "  },\n";

    j << "  \"started\": {\n";
    j << "    \"monotonic_ns\": " << started_.monotonic_ns << ",\n";
    j << "    \"realtime_ns\": " << started_.realtime_ns << ",\n";
    j << "    \"sample_spread_ns\": " << started_.sample_spread_ns << "\n";
    j << "  },\n";

    j << "  \"host\": {\n";
    j << "    \"hostname\": " << quoted(hostname()) << ",\n";
    j << "    \"kernel\": " << quoted(kernel_release()) << "\n";
    j << "  },\n";

    j << "  \"notes\": {";
    for (std::size_t i = 0; i < notes_.size(); ++i) {
        j << (i ? ",\n    " : "\n    ") << quoted(notes_[i].first) << ": "
          << quoted(notes_[i].second);
    }
    j << (notes_.empty() ? "}" : "\n  }") << ",\n";

    j << "  \"segments\": [";
    for (std::size_t i = 0; i < segs.size(); ++i) {
        const auto& s = segs[i];
        j << (i ? ",\n" : "\n");
        j << "    {\n";
        j << "      \"number\": " << s.number << ",\n";
        j << "      \"stream\": " << quoted(s.stream) << ",\n";
        j << "      \"index\": " << quoted(s.index) << ",\n";
        j << "      \"frames\": " << s.frames << ",\n";
        j << "      \"bytes\": " << s.bytes << ",\n";
        if (s.has_frames) {
            j << "      \"first_ts_mono_ns\": " << s.first_ts_mono_ns << ",\n";
            j << "      \"last_ts_mono_ns\": " << s.last_ts_mono_ns << "\n";
        } else {
            j << "      \"first_ts_mono_ns\": null,\n";
            j << "      \"last_ts_mono_ns\": null\n";
        }
        j << "    }";
    }
    j << (segs.empty() ? "]" : "\n  ]");

    if (finalized) {
        const ClockPair ended = sample_clocks();
        const std::int64_t duration_ns = ended.monotonic_ns - started_.monotonic_ns;
        j << ",\n";
        j << "  \"ended\": {\n";
        j << "    \"monotonic_ns\": " << ended.monotonic_ns << ",\n";
        j << "    \"realtime_ns\": " << ended.realtime_ns << ",\n";
        j << "    \"sample_spread_ns\": " << ended.sample_spread_ns << "\n";
        j << "  },\n";
        j << "  \"frames_written\": " << writer.frames_written << ",\n";
        j << "  \"bytes_written\": " << writer.bytes_written << ",\n";
        j << "  \"counters\": {\n";
        j << "    \"frames\": " << (source ? source->frames_yielded : 0) << ",\n";
        j << "    \"driver_dropped\": " << (source ? source->driver_dropped : 0) << ",\n";
        j << "    \"driver_errors\": " << (source ? source->driver_errors : 0) << ",\n";
        j << "    \"corrupt\": " << (source ? source->corrupt : 0) << "\n";
        j << "  },\n";
        j << "  \"duration_ns\": " << duration_ns << ",\n";
        // Measured, not requested: frames actually written over wall time elapsed.
        if (duration_ns > 0) {
            j << "  \"effective_fps\": "
              << number(double(writer.frames_written) / (double(duration_ns) / 1e9)) << ",\n";
        } else {
            j << "  \"effective_fps\": null,\n";
        }
        j << "  \"writer\": {\n";
        j << "    \"queue_high_water\": " << queue_high_water << ",\n";
        j << "    \"queue_depth\": " << queue_depth << ",\n";
        j << "    \"frames_flagged\": " << writer.frames_flagged << "\n";
        j << "  }";
    }
    j << "\n}\n";

    const std::string final_path = directory + "/manifest.json";
    const std::string tmp_path = final_path + ".tmp";
    std::FILE* f = std::fopen(tmp_path.c_str(), "wb");
    if (!f) throw std::runtime_error("cannot write " + tmp_path);
    const std::string text = j.str();
    std::fwrite(text.data(), 1, text.size(), f);
    std::fflush(f);
    ::fsync(::fileno(f));
    std::fclose(f);
    if (std::rename(tmp_path.c_str(), final_path.c_str()) != 0) {
        throw std::runtime_error("cannot rename " + tmp_path);
    }
}

}  // namespace vcap
