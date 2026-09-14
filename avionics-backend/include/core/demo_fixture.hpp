#pragma once
#include "core/types.hpp"
#include "plugin_api/bus_plugin.h"
#include <bit>
#include <map>

namespace avionics {
// Deterministic synthetic data only. No aircraft parameter mapping is implied.
inline std::vector<RawFrame> correlationFixture() {
    std::vector<RawFrame> frames;
    std::map<std::string, std::uint64_t> sequences;
    auto add = [&](const std::string& source, std::uint64_t ms, std::int64_t milli) {
        RawFrame frame;
        frame.source = source; frame.generation = 1; frame.protocol = BUS_PROTOCOL_SIMULATED;
        frame.clock_domain = BUS_CLOCK_HOST_MONOTONIC;
        frame.sequence = sequences[source]++; frame.record_index = frames.size() + 1;
        frame.capture_time_ns = 1000000000 + ms * 1000000; frame.ingest_time_ns = frame.capture_time_ns + 100;
        const auto bits = std::bit_cast<std::uint64_t>(milli);
        for (std::size_t i = 0; i < 8; ++i) frame.payload.push_back(static_cast<std::uint8_t>(bits >> (8 * i)));
        frames.push_back(std::move(frame));
    };
    add("feedback", 0, 0); add("control", 0, 0);
    add("sensor_a", 0, 10000); add("sensor_b", 0, 10100);
    add("control", 10, 1000); add("feedback", 15, 0); add("feedback", 22, 1000);
    add("sensor_a", 30, 10000); add("sensor_b", 30, 11000);
    add("control", 40, 0); add("feedback", 40, 0);
    add("control", 50, 1000); add("feedback", 60, 0); add("feedback", 75, 0);
    add("control", 85, 1000); add("control", 90, 0); add("control", 100, 1000);
    return frames;
}
}
