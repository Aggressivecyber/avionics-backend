#pragma once
#include <cstdint>
#include <span>
#include <stdexcept>

namespace avionics {
// Bit numbering follows the existing dictionary: bit 0 is the least significant
// bit of the first payload byte when little endian, and the most significant bit
// of the first payload byte when big endian. Offset and width are in bits.
inline std::uint64_t extractBits(std::span<const std::uint8_t> payload,
    std::uint32_t offset, std::uint32_t width, bool big_endian) {
    if (std::uint64_t(offset) + width > std::uint64_t(payload.size()) * 8ull)
        throw std::out_of_range("short payload");
    std::uint64_t result{};
    for (std::uint32_t i = 0; i < width; ++i) {
        const auto at = offset + i;
        const auto bit = (payload[at / 8] >> (big_endian ? 7 - at % 8 : at % 8)) & 1u;
        if (big_endian) result = (result << 1) | bit;
        else result |= std::uint64_t(bit) << i;
    }
    return result;
}
}
