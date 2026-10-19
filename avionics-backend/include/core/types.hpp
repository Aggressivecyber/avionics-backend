#pragma once
#include "core/identity.hpp"
#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace avionics {
inline std::uint64_t monotonicNowNs() noexcept {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}
struct RawFrame {
    std::string source;
    std::uint64_t generation{};
    std::uint32_t protocol{}, channel{}, flags{}, clock_domain{};
    std::uint64_t capture_time_ns{}, ingest_time_ns{}, sequence{}, record_index{};
    std::string origin_source;
    std::uint64_t origin_generation{}, origin_record_index{};
    std::vector<std::uint8_t> payload;
    std::optional<SessionId> recording_session_id, origin_session_id;
};
struct EnumValue { std::int64_t code{}; std::string label; };
using ParameterValue = std::variant<std::int64_t, std::uint64_t, double, bool, EnumValue>;
struct ParameterSample {
    std::string parameter_id, source, unit, decoder_version;
    std::uint64_t generation{}, capture_time_ns{}, ingest_time_ns{}, raw_record_index{};
    std::uint64_t origin_generation{};
    std::uint64_t sequence{}, max_age_ns{}, sequence_step{};
    std::uint32_t clock_domain{};
    ParameterValue value{0.0};
    bool valid{};
    std::optional<SessionId> recording_session_id, origin_session_id;
    std::optional<ContentDigest> configuration_sha256;
    std::string origin_source;
    std::uint64_t origin_record_index{};
};
struct AnalysisEvent {
    std::string rule_id, source, parameter_id, description;
    std::uint64_t generation{}, time_ns{}, raw_record_index{};
    double value{};
};
}
