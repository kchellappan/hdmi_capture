// SPDX-License-Identifier: MIT
//
// Record a session to a .mjpg/.idx/manifest.json triple, in the format the Python client
// reads. Deliberately a close mirror of tools/vcap-record so the two are comparable when
// one of them misbehaves.
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>

#include "vcap/device.hpp"
#include "vcap/manifest.hpp"
#include "vcap/source.hpp"
#include "vcap/writer.hpp"

namespace {

volatile std::sig_atomic_t g_stopping = 0;
void on_signal(int) { g_stopping = 1; }

double monotonic_seconds() {
    timespec ts{};
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return double(ts.tv_sec) + double(ts.tv_nsec) / 1e9;
}

void usage() {
    std::fprintf(stderr,
        "usage: vcap-record-cpp DIRECTORY [options]\n"
        "\n"
        "  -d, --device HINT     by-id path, /dev/videoN, or a name substring\n"
        "      --format FOURCC   pixel format (default MJPG)\n"
        "      --size WxH        geometry (default 1920x1080)\n"
        "      --fps N           frame rate (default 60)\n"
        "      --seconds N       stop after this long\n"
        "      --frames N        stop after this many frames\n"
        "      --name NAME       basename for the files (default session)\n"
        "      --buffers N       kernel buffers (default 4)\n"
        "      --queue N         writer queue depth in frames (default 240)\n"
        "      --fsync-every N   fsync every N frames; 0 lets the OS decide\n"
        "      --segment-mb N    roll over at this size; 0 disables (default 2048)\n"
        "      --note K=V        metadata recorded in the manifest\n"
        "  -q, --quiet\n");
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2 || std::strcmp(argv[1], "-h") == 0 || std::strcmp(argv[1], "--help") == 0) {
        usage();
        return argc < 2 ? 2 : 0;
    }

    std::string directory = argv[1], hint, format = "MJPG", name = "session";
    std::uint32_t width = 1920, height = 1080;
    double fps = 60.0, seconds = 0.0;
    long frames_limit = 0, segment_mb = 2048;
    unsigned buffers = 4, fsync_every = 0;
    std::size_t queue_depth = 240;
    bool quiet = false;
    std::vector<std::pair<std::string, std::string>> notes;

    for (int i = 2; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) { std::fprintf(stderr, "%s needs a value\n", a.c_str()); std::exit(2); }
            return argv[++i];
        };
        if (a == "-d" || a == "--device") hint = next();
        else if (a == "--format") format = next();
        else if (a == "--size") {
            const std::string v = next();
            const auto x = v.find('x');
            if (x == std::string::npos) { std::fprintf(stderr, "--size wants WxH\n"); return 2; }
            width = std::stoul(v.substr(0, x));
            height = std::stoul(v.substr(x + 1));
        }
        else if (a == "--fps") fps = std::stod(next());
        else if (a == "--seconds") seconds = std::stod(next());
        else if (a == "--frames") frames_limit = std::stol(next());
        else if (a == "--name") name = next();
        else if (a == "--buffers") buffers = unsigned(std::stoul(next()));
        else if (a == "--queue") queue_depth = std::stoul(next());
        else if (a == "--fsync-every") fsync_every = unsigned(std::stoul(next()));
        else if (a == "--segment-mb") segment_mb = std::stol(next());
        else if (a == "--note") {
            const std::string kv = next();
            const auto eq = kv.find('=');
            notes.emplace_back(kv.substr(0, eq == std::string::npos ? kv.size() : eq),
                               eq == std::string::npos ? "" : kv.substr(eq + 1));
        }
        else if (a == "-q" || a == "--quiet") quiet = true;
        else { std::fprintf(stderr, "unknown option: %s\n", a.c_str()); usage(); return 2; }
    }

    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);

    try {
        vcap::Device device = vcap::find_capture_card(hint);
        vcap::VideoSource source(device, format, width, height, fps, buffers);
        const vcap::Negotiated& neg = source.open();

        if (!quiet) {
            std::printf("%s\n", device.path.c_str());
            std::printf("  granted: %s %ux%u @ %g fps\n", neg.pixelformat.c_str(),
                        neg.width, neg.height, neg.fps_granted);
            // A rate or geometry the device does not offer is coerced to one it does, and
            // on the target card the rate rounds *up*. The line above otherwise reads as
            // confirmation.
            if (neg.fps_granted > 0 && std::abs(neg.fps_granted - fps) > 0.5) {
                std::printf("  NOTE: asked for %g fps, device gave %g.\n", fps, neg.fps_granted);
            }
            if (neg.width != width || neg.height != height) {
                std::printf("  NOTE: asked for %ux%u, device gave %ux%u.\n",
                            width, height, neg.width, neg.height);
            }
            std::printf("  timestamps: %s, %s\n", neg.timestamp_clock.c_str(),
                        neg.timestamp_source.c_str());
            if (!neg.timestamps_are_monotonic()) {
                std::fprintf(stderr, "  WARNING: timestamps are not on CLOCK_MONOTONIC, so "
                             "they cannot be aligned against controls captured here\n");
            }
            std::printf("  writing to %s/\n\n", directory.c_str());
            std::fflush(stdout);
        }

        vcap::SegmentWriter writer(directory, name,
                                   segment_mb > 0 ? std::uint64_t(segment_mb) * 1024 * 1024 : 0,
                                   fsync_every);
        vcap::Manifest manifest(name, device, neg, format, width, height, fps);
        for (const auto& [k, v] : notes) manifest.set_note(k, v);
        // Written before the first frame so a session killed by a crash still describes
        // itself; rewritten on the way out with the counters.
        manifest.write(directory, writer, false);

        int exit_code = 0;
        {
            vcap::AsyncWriter async(writer, queue_depth);
            const double deadline = seconds > 0 ? monotonic_seconds() + seconds : 0.0;
            long count = 0;
            double last_report = monotonic_seconds();
            try {
                for (;;) {
                    vcap::Frame frame = source.read(3.0);
                    async.write(std::move(frame));
                    ++count;
                    if (g_stopping) break;
                    if (frames_limit && count >= frames_limit) break;
                    if (deadline > 0 && monotonic_seconds() >= deadline) break;
                    const double now = monotonic_seconds();
                    if (!quiet && now - last_report >= 1.0) {
                        std::fprintf(stderr, "\r  %llu frames  %llu MB  dropped %llu  "
                                     "flagged %llu  queue peak %zu   ",
                                     (unsigned long long)writer.frames_written,
                                     (unsigned long long)(writer.bytes_written / 1000000),
                                     (unsigned long long)source.driver_dropped,
                                     (unsigned long long)writer.frames_flagged,
                                     async.high_water());
                        last_report = now;
                    }
                }
            } catch (const vcap::NoSignal& e) {
                std::fprintf(stderr, "\nno signal: %s\n", e.what());
                exit_code = 1;
            } catch (const vcap::WriterOverrun& e) {
                std::fprintf(stderr, "\n%s\n", e.what());
                exit_code = 1;
            }
            async.close();
            if (!quiet) std::fprintf(stderr, "\n");
            manifest.write(directory, writer, true, &source, async.high_water(), queue_depth);
        }

        if (!quiet) {
            std::printf("frames %llu  bytes %llu  dropped %llu  corrupt %llu  flagged %llu\n",
                        (unsigned long long)writer.frames_written,
                        (unsigned long long)writer.bytes_written,
                        (unsigned long long)source.driver_dropped,
                        (unsigned long long)source.corrupt,
                        (unsigned long long)writer.frames_flagged);
        }
        return exit_code;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 2;
    }
}
