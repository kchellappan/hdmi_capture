// SPDX-License-Identifier: MIT
//
// Write a recording from synthetic frames, with no capture device.
//
// Everything from SegmentWriter onwards is hardware-independent: it takes Frames and
// produces files. The only thing it needs from a real card is plausible JPEGs, and a
// minimal structurally valid one is short enough to build by hand -- which is what makes
// the C++-writes/Python-reads contract testable on a CI runner with no USB.
//
// Mirrors tests/fake.py on the Python side, including the first-frame fragment: the card
// reliably delivers a headless partial frame at stream start, so a recording that does
// not contain one is not representative of a real session.
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "vcap/frame.hpp"
#include "vcap/manifest.hpp"
#include "vcap/writer.hpp"

namespace {

std::vector<std::uint8_t> make_jpeg(std::uint32_t w, std::uint32_t h, std::size_t payload,
                                    bool headless) {
    std::vector<std::uint8_t> d{0xFF, 0xD8};
    const std::uint8_t sof[] = {0xFF, 0xC0, 0x00, 0x11, 0x08,
                                std::uint8_t(h >> 8), std::uint8_t(h & 0xFF),
                                std::uint8_t(w >> 8), std::uint8_t(w & 0xFF),
                                0x03, 0x01, 0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01};
    d.insert(d.end(), sof, sof + sizeof sof);
    d.insert(d.end(), {0xFF, 0xFE, std::uint8_t((payload + 2) >> 8), std::uint8_t((payload + 2) & 0xFF)});
    d.insert(d.end(), payload, 0x00);
    d.insert(d.end(), {0xFF, 0xD9});
    // A headless fragment keeps its valid end-of-image and loses its head, which is what
    // STREAMON produces on a card that transmits continuously.
    if (headless) d.erase(d.begin(), d.begin() + std::ptrdiff_t(d.size() / 2));
    return d;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "usage: write_synthetic DIRECTORY [FRAMES]\n"); return 2; }
    const std::string dir = argv[1];
    const int count = argc > 2 ? std::atoi(argv[2]) : 200;

    // A synthetic frame is about 150 bytes, so an 8 KB segment holds roughly 50 of
    // them and the default 400 frames cross the boundary seven times. Sized against the
    // frame, not picked round: at 64 KB the whole run fit in one segment and the
    // rollover path went untested while the test still passed.
    vcap::SegmentWriter writer(dir, "session", 8 * 1024, 0);

    vcap::Device device;
    device.path = "/dev/v4l/by-id/synthetic-video-index0";
    device.node = "/dev/video99";
    device.driver = "synthetic";
    device.card = "synthetic capture";
    device.bus_info = "synthetic";

    vcap::Negotiated negotiated;
    negotiated.pixelformat = "MJPG";
    negotiated.width = 64;
    negotiated.height = 48;
    negotiated.fps_requested = 60.0;
    negotiated.fps_granted = 60.0;
    negotiated.timestamp_clock = "monotonic";
    negotiated.timestamp_source = "start-of-frame";

    vcap::Manifest manifest("session", device, negotiated, "MJPG", 64, 48, 60.0);
    manifest.set_note("writer", "cpp-synthetic");
    manifest.write(dir, writer, false);

    constexpr std::int64_t kPeriodNs = 16666667;
    std::int64_t ts = 1000000000LL;
    std::uint32_t seq = 0;
    for (int i = 0; i < count; ++i) {
        vcap::Frame f;
        const bool headless = (i == 0);
        // A gap partway through, so the reader's gap handling is exercised too.
        if (i == count / 2) { seq += 3; ts += kPeriodNs * 3; f.dropped_before = 3;
                              f.flags |= vcap::kFlagGapBefore; }
        if (headless) f.flags |= vcap::kFlagCorrupt;
        f.data = make_jpeg(64, 48, 100 + std::size_t(i % 50), headless);
        f.ts_mono_ns = ts;
        f.seq = seq;
        writer.write(f);
        ++seq;
        ts += kPeriodNs;
    }
    writer.close();
    manifest.write(dir, writer, true, nullptr, 0, 240);
    std::printf("%llu frames in %zu segments\n",
                (unsigned long long)writer.frames_written, writer.segments().size());
    return 0;
}
