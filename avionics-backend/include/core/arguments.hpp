#pragma once
#include <stdexcept>
#include <string>
#include <vector>
#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shellapi.h>
#endif

namespace avionics {
inline std::vector<std::string> utf8Arguments(int argc, char** argv) {
#if defined(_WIN32)
    (void)argc; (void)argv;
    int count{};
    auto** wide = CommandLineToArgvW(GetCommandLineW(), &count);
    if (!wide) throw std::runtime_error("cannot read Unicode command line");
    std::vector<std::string> result;
    for (int i = 0; i < count; ++i) {
        const auto bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide[i], -1, nullptr, 0, nullptr, nullptr);
        if (!bytes) { LocalFree(wide); throw std::runtime_error("invalid Unicode command argument"); }
        std::string text(static_cast<std::size_t>(bytes), '\0');
        const auto written = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide[i], -1, text.data(), bytes, nullptr, nullptr);
        if (written != bytes) { LocalFree(wide); throw std::runtime_error("cannot convert Unicode command argument"); }
        text.pop_back(); result.push_back(std::move(text));
    }
    LocalFree(wide);
    return result;
#else
    return {argv, argv + argc};
#endif
}
}
