#include "core/configuration.hpp"
#include "plugin_api/bus_plugin.h"
#include <bit>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace avionics {
namespace {
std::uint64_t bits(const RawFrame& frame, std::uint32_t offset, std::uint32_t width, bool big) {
    if (std::uint64_t(offset) + width > frame.payload.size() * 8ull) throw std::out_of_range("short payload");
    std::uint64_t result{};
    for (std::uint32_t i = 0; i < width; ++i) {
        const auto at = offset + i;
        const auto bit = (frame.payload[at / 8] >> (big ? 7 - at % 8 : at % 8)) & 1u;
        if (big) result = (result << 1) | bit;
        else result |= std::uint64_t(bit) << i;
    }
    return result;
}
}
std::vector<ParameterSample> DictionaryDecoder::decode(const RawFrame& frame) {
    std::vector<ParameterSample> samples;
    for (const auto& field : config_.fields) {
        if (field.protocol != frame.protocol || field.channel != frame.channel ||
            (field.source != "*" && field.source != frame.source)) continue;
        ParameterSample sample;
        sample.source = frame.source; sample.parameter_id = field.id; sample.unit = field.unit;
        sample.generation = frame.generation; sample.origin_generation = frame.origin_generation;
        sample.capture_time_ns = frame.capture_time_ns; sample.ingest_time_ns = frame.ingest_time_ns;
        sample.clock_domain = frame.clock_domain; sample.raw_record_index = frame.record_index;
        sample.sequence = frame.sequence; sample.sequence_step = field.sequence_step; sample.max_age_ns = field.max_age_ns;
        sample.decoder_version = config_.version; sample.valid = (frame.flags & BUS_FRAME_INVALID) == 0;
        try {
            const auto raw = bits(frame, field.bit_offset, field.bit_width, field.big_endian);
            switch (field.type) {
            case FieldType::Signed: {
                auto extended = raw;
                if (field.bit_width < 64 && (raw & (1ull << (field.bit_width - 1)))) extended |= ~((1ull << field.bit_width) - 1);
                sample.value = std::bit_cast<std::int64_t>(extended); break;
            }
            case FieldType::Unsigned: sample.value = raw; break;
            case FieldType::Boolean: sample.value = raw != 0; break;
            case FieldType::Enumeration: {
                const auto code = static_cast<std::int64_t>(raw);
                auto label = field.enum_values.find(code);
                sample.value = EnumValue{code, label == field.enum_values.end() ? "UNKNOWN" : label->second};
                if (label == field.enum_values.end()) sample.valid = false;
                break;
            }
            case FieldType::Float64: sample.value = std::bit_cast<double>(raw); break;
            }
            if (field.scale != 1 || field.offset != 0) {
                const auto numeric = std::visit([](const auto& value) -> long double {
                    if constexpr (std::is_arithmetic_v<std::decay_t<decltype(value)>>) return static_cast<long double>(value);
                    else return 0;
                }, sample.value);
                sample.value = static_cast<double>(numeric * field.scale + field.offset);
            }
            if (const auto value = std::get_if<double>(&sample.value); value && !std::isfinite(*value)) sample.valid = false;
            if (field.valid_bit && !bits(frame, *field.valid_bit, 1, field.big_endian)) sample.valid = false;
        } catch (const std::out_of_range&) { sample.valid = false; sample.value = 0.0; }
        samples.push_back(std::move(sample));
    }
    return samples;
}
}
