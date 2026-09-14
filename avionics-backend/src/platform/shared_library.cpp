#include "core/shared_library.hpp"
#include <stdexcept>
#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace avionics {
SharedLibrary::SharedLibrary(const std::filesystem::path& path) {
#if defined(_WIN32)
    handle_ = reinterpret_cast<void*>(LoadLibraryExW(path.c_str(), nullptr,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS));
    if (!handle_) throw std::runtime_error("LoadLibrary failed, code " + std::to_string(GetLastError()));
#else
    handle_ = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle_) throw std::runtime_error(std::string("dlopen: ") + dlerror());
#endif
}
SharedLibrary::~SharedLibrary() {
#if defined(_WIN32)
    if (handle_) FreeLibrary(reinterpret_cast<HMODULE>(handle_));
#else
    if (handle_) dlclose(handle_);
#endif
}
void* SharedLibrary::symbol(const char* name) const {
#if defined(_WIN32)
    auto result = reinterpret_cast<void*>(GetProcAddress(reinterpret_cast<HMODULE>(handle_), name));
#else
    auto result = dlsym(handle_, name);
#endif
    if (!result) throw std::runtime_error(std::string("missing plugin symbol: ") + name);
    return result;
}
}
