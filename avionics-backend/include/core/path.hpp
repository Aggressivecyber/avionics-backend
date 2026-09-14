#pragma once
#include <filesystem>
#include <string_view>

namespace avionics {
inline std::filesystem::path pathFromUtf8(std::string_view text) {
    std::u8string utf8;
    utf8.reserve(text.size());
    for (const unsigned char byte : text) utf8.push_back(static_cast<char8_t>(byte));
    return std::filesystem::path(utf8);
}
}
