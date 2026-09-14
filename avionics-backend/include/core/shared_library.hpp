#pragma once
#include <filesystem>

namespace avionics {
class SharedLibrary {
public:
    explicit SharedLibrary(const std::filesystem::path&);
    ~SharedLibrary();
    SharedLibrary(const SharedLibrary&) = delete;
    SharedLibrary& operator=(const SharedLibrary&) = delete;
    void* symbol(const char*) const;
private:
    void* handle_{};
};
}
