#pragma once
// Project monitor envelope, not the electrical 1553B wire format or a vendor SDK ABI.
#include <array>
#include <bit>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <vector>

namespace avionics::mil1553b {
inline constexpr char capture_representation[] = "mil1553b.monitor-v1";
inline constexpr std::array<std::uint8_t, 8> capture_magic{'M','1','5','5','3','B','0','1'};
enum class Sync : std::uint8_t { CommandStatus, Data };
struct CapturedWord { std::uint16_t value; Sync sync; std::uint8_t parity; };
inline CapturedWord syntheticWord(std::uint16_t value, Sync sync) {
    return {value, sync, static_cast<std::uint8_t>((std::popcount(value) & 1) == 0)};
}
inline std::vector<std::uint8_t> encodeCapture(std::uint8_t bus, std::span<const CapturedWord> words) {
    if (bus > 1 || words.empty() || words.size() > 36) throw std::invalid_argument("invalid 1553B monitor envelope");
    std::vector<std::uint8_t> bytes(capture_magic.begin(), capture_magic.end());
    bytes.insert(bytes.end(), {bus, static_cast<std::uint8_t>(words.size()), 0, 0});
    for (const auto& word : words) {
        bytes.push_back(static_cast<std::uint8_t>(word.value));
        bytes.push_back(static_cast<std::uint8_t>(word.value >> 8));
        bytes.push_back(static_cast<std::uint8_t>(word.sync));
        bytes.push_back(word.parity);
    }
    return bytes;
}
}
