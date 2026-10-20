#pragma once
#include <filesystem>
#include <string_view>

namespace avionics {
inline std::filesystem::path absolutePath(const std::filesystem::path& path) {
#ifdef _WIN32
    if (path.native().starts_with(L"\\\\?\\")) return path;
#endif
    return std::filesystem::absolute(path);
}
inline std::filesystem::path pathFromUtf8(std::string_view text) {
    std::u8string utf8;
    utf8.reserve(text.size());
    for (const unsigned char byte : text) utf8.push_back(static_cast<char8_t>(byte));
    auto path = std::filesystem::path(utf8);
#ifdef _WIN32
    // Project/run/configuration paths can legitimately exceed MAX_PATH.
    path.make_preferred();
    if (path.native().starts_with(L"\\\\?\\")) return path;
    path = absolutePath(path).lexically_normal().make_preferred();
    const auto& native = path.native();
    if (!native.starts_with(L"\\\\?\\")) {
        return std::filesystem::path(native.starts_with(L"\\\\") ? L"\\\\?\\UNC\\" + native.substr(2) : L"\\\\?\\" + native);
    }
#endif
    return path;
}
}
