#pragma once
#include "core/identity.hpp"
#include <iomanip>
#include <optional>
#include <sstream>
#include <string>

namespace avionics {
inline std::string jsonString(const std::string& text) {
    std::ostringstream output; output << '"';
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') output << '\\' << c;
        else if (c < 32) output << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c) << std::dec;
        else output << c;
    }
    output << '"'; return output.str();
}
template<std::size_t N> std::string jsonIdentity(const std::optional<std::array<std::uint8_t, N>>& identity) {
    return identity ? jsonString(hexIdentity(*identity)) : "null";
}
}
