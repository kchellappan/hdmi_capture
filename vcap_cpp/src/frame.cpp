// SPDX-License-Identifier: MIT
#include "vcap/frame.hpp"

namespace vcap {

bool looks_like_jpeg(const std::uint8_t* d, std::size_t n) {
    return n >= 4 && d[0] == 0xFF && d[1] == 0xD8 && d[n - 2] == 0xFF && d[n - 1] == 0xD9;
}

bool jpeg_dimensions(const std::uint8_t* d, std::size_t n,
                     std::uint32_t* width, std::uint32_t* height) {
    if (n < 4 || d[0] != 0xFF || d[1] != 0xD8) return false;
    std::size_t i = 2;
    while (i + 3 < n) {
        if (d[i] != 0xFF) return false;
        const std::uint8_t marker = d[i + 1];
        // Padding between segments is legal and encoded as repeated 0xFF.
        if (marker == 0xFF) { ++i; continue; }
        // Standalone markers carry no length field.
        if (marker == 0x01 || marker == 0xD8 || marker == 0xD9 ||
            (marker >= 0xD0 && marker <= 0xD7)) { i += 2; continue; }
        if (i + 3 >= n) return false;
        const std::size_t length = (std::size_t(d[i + 2]) << 8) | d[i + 3];
        // SOF0..SOF15, excluding the Huffman-table and arithmetic-coding markers that
        // share the range. Treating 0xC4 as a start-of-frame reads its table bytes as a
        // resolution, which looks plausible and is wrong.
        if (marker >= 0xC0 && marker <= 0xCF &&
            marker != 0xC4 && marker != 0xC8 && marker != 0xCC) {
            if (i + 9 >= n) return false;
            *height = (std::uint32_t(d[i + 5]) << 8) | d[i + 6];
            *width  = (std::uint32_t(d[i + 7]) << 8) | d[i + 8];
            return true;
        }
        if (length < 2) return false;
        i += 2 + length;
    }
    return false;
}

}  // namespace vcap
