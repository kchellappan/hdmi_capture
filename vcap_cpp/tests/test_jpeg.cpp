// SPDX-License-Identifier: MIT
//
// The JPEG marker walker. Mirrors tests/test_jpeg.py, including the cases that were got
// wrong there: a fragment missing its head is the common failure on this card, and a
// Huffman table shares the marker range with start-of-frame.
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "vcap/frame.hpp"

namespace {

int failures = 0;

void check(const char* name, bool got, bool want) {
    if (got != want) {
        ++failures;
        std::printf("  FAIL  %s: got %s, want %s\n", name, got ? "true" : "false",
                    want ? "true" : "false");
    }
}

void check_dims(const char* name, const std::vector<std::uint8_t>& d,
                bool want_ok, std::uint32_t want_w = 0, std::uint32_t want_h = 0) {
    std::uint32_t w = 0, h = 0;
    const bool ok = vcap::jpeg_dimensions(d.data(), d.size(), &w, &h);
    if (ok != want_ok || (ok && (w != want_w || h != want_h))) {
        ++failures;
        std::printf("  FAIL  %s: got ok=%d %ux%u, want ok=%d %ux%u\n", name, int(ok), w, h,
                    int(want_ok), want_w, want_h);
    }
}

// A structurally valid JPEG: SOI, a baseline SOF0, filler, and optionally EOI. Not
// decodable -- no Huffman table or scan data -- but it has exactly the structure the
// marker walker reads.
std::vector<std::uint8_t> make_jpeg(std::uint32_t w, std::uint32_t h,
                                    bool complete = true, bool headless = false) {
    std::vector<std::uint8_t> d{0xFF, 0xD8};
    const std::uint8_t sof[] = {0xFF, 0xC0, 0x00, 0x11, 0x08,
                                std::uint8_t(h >> 8), std::uint8_t(h & 0xFF),
                                std::uint8_t(w >> 8), std::uint8_t(w & 0xFF),
                                0x03, 0x01, 0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01};
    d.insert(d.end(), sof, sof + sizeof sof);
    d.insert(d.end(), {0xFF, 0xFE, 0x00, 0x42});
    d.insert(d.end(), 0x40 - 2, 0x00);
    if (complete) d.insert(d.end(), {0xFF, 0xD9});
    if (headless) d.erase(d.begin(), d.begin() + std::ptrdiff_t(d.size() / 2));
    return d;
}

}  // namespace

int main() {
    check_dims("dimensions of 1920x1080", make_jpeg(1920, 1080), true, 1920, 1080);
    check_dims("dimensions of 2560x1600", make_jpeg(2560, 1600), true, 2560, 1600);
    check_dims("empty input", {}, false);
    check_dims("not a JPEG", {0x89, 'P', 'N', 'G'}, false);

    check("complete JPEG is complete", vcap::looks_like_jpeg(make_jpeg(64, 48)), true);
    check("frame without EOI is incomplete",
          vcap::looks_like_jpeg(make_jpeg(64, 48, false)), false);

    // The common case on this card by far: the stream-start fragment, which has a
    // perfectly valid EOI and is missing its head. A check testing only the tail -- the
    // intuitive choice, since signal loss costs the tail -- would pass this and let an
    // undecodable frame into a dataset unflagged.
    const auto headless = make_jpeg(64, 48, true, true);
    check("a headless fragment is not a whole JPEG", vcap::looks_like_jpeg(headless), false);
    check("and it does end with a valid EOI",
          headless.size() >= 2 && headless[headless.size() - 2] == 0xFF &&
              headless.back() == 0xD9, true);
    check_dims("a headless fragment has no readable geometry", headless, false);

    // DHT shares the 0xC0-0xCF range with the start-of-frame markers but is not one. A
    // walker that treats 0xC4 as an SOF reads its table bytes as a resolution.
    std::vector<std::uint8_t> with_dht{0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x06, 0x00, 0x01, 0x02, 0x03};
    const auto tail = make_jpeg(320, 240);
    with_dht.insert(with_dht.end(), tail.begin() + 2, tail.end());
    check_dims("DHT is not mistaken for a start-of-frame", with_dht, true, 320, 240);

    if (failures == 0) std::printf("  ok    jpeg marker walking\n");
    return failures ? 1 : 0;
}
