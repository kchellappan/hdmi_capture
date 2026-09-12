// SPDX-License-Identifier: MIT
//
// The index record. This is the one piece of this client that MUST agree with the Python
// side byte for byte, because a recording written here is read there.
//
// Python packs it with struct.Struct("<QIQIII") -- little-endian, explicitly unaligned,
// 32 bytes. The obvious C++ translation is a packed struct, and it is avoided here on
// purpose: a packed struct depends on a compiler extension for its layout and on the host
// being little-endian for its contents, and neither dependency announces itself when it
// breaks. Writing the fields one at a time costs nothing and is portable by construction.
//
// tests/test_index_format.cpp checks the encoding against bytes produced by Python.
#pragma once

#include <cstdint>
#include <cstring>

namespace vcap {

inline constexpr std::size_t kIndexRecordSize = 32;

namespace detail {

inline void put_u32_le(std::uint8_t* out, std::uint32_t v) {
    out[0] = static_cast<std::uint8_t>(v);
    out[1] = static_cast<std::uint8_t>(v >> 8);
    out[2] = static_cast<std::uint8_t>(v >> 16);
    out[3] = static_cast<std::uint8_t>(v >> 24);
}

inline void put_u64_le(std::uint8_t* out, std::uint64_t v) {
    for (int i = 0; i < 8; ++i) out[i] = static_cast<std::uint8_t>(v >> (8 * i));
}

}  // namespace detail

// byte_offset, length, ts_mono_ns, seq, flags, dropped_before.
inline void pack_index_record(std::uint8_t out[kIndexRecordSize],
                              std::uint64_t byte_offset, std::uint32_t length,
                              std::int64_t ts_mono_ns, std::uint32_t seq,
                              std::uint32_t flags, std::uint32_t dropped_before) {
    detail::put_u64_le(out + 0,  byte_offset);
    detail::put_u32_le(out + 8,  length);
    // Stored unsigned, as Python's "Q" does. A monotonic timestamp is never negative,
    // and the cast keeps the on-disk encoding identical on a platform where int64 and
    // uint64 would otherwise differ in how a shift behaves.
    detail::put_u64_le(out + 12, static_cast<std::uint64_t>(ts_mono_ns));
    detail::put_u32_le(out + 20, seq);
    detail::put_u32_le(out + 24, flags);
    detail::put_u32_le(out + 28, dropped_before);
}

}  // namespace vcap
