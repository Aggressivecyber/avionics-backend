#pragma once
#include "core/bitfield.hpp"
#include "core/configuration.hpp"
#include <bit>
#include <cmath>
#include <type_traits>

namespace avionics {
// Shared bit-field conversion used by both the frame dictionary decoder and the
// protocol message decoder. Callers keep their own provenance and quality rules.
inline void decodeFieldValue(const FieldDefinition& field, std::span<const std::uint8_t> payload,
    ParameterValue& value, bool& valid) {
    const auto raw = extractBits(payload, field.bit_offset, field.bit_width, field.big_endian);
    switch (field.type) {
    case FieldType::Signed: {
        auto extended = raw;
        if (field.bit_width < 64 && (raw & (1ull << (field.bit_width - 1)))) extended |= ~((1ull << field.bit_width) - 1);
        value = std::bit_cast<std::int64_t>(extended);
        break;
    }
    case FieldType::Unsigned: value = raw; break;
    case FieldType::Boolean: value = raw != 0; break;
    case FieldType::Enumeration: {
        const auto code = static_cast<std::int64_t>(raw);
        const auto label = field.enum_values.find(code);
        value = EnumValue{code, label == field.enum_values.end() ? "UNKNOWN" : label->second};
        if (label == field.enum_values.end()) valid = false;
        break;
    }
    case FieldType::Float64: value = std::bit_cast<double>(raw); break;
    }
    if (field.scale != 1 || field.offset != 0) {
        const auto numeric = std::visit([](const auto& entry) -> long double {
            if constexpr (std::is_arithmetic_v<std::decay_t<decltype(entry)>>) return static_cast<long double>(entry);
            else return 0;
        }, value);
        value = static_cast<double>(numeric * field.scale + field.offset);
    }
    if (const auto number = std::get_if<double>(&value); number && !std::isfinite(*number)) valid = false;
    if (field.valid_bit && !extractBits(payload, *field.valid_bit, 1, field.big_endian)) valid = false;
}
}
