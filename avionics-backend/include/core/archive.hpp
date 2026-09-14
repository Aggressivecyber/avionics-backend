#pragma once
#include "core/types.hpp"
#include <filesystem>
#include <fstream>
#include <memory>

namespace avionics {
class IFrameRecorder {
public:
    virtual ~IFrameRecorder() = default;
    virtual void write(const RawFrame&) = 0;
    virtual void flush() = 0;
};
class ArchiveWriter final : public IFrameRecorder {
public:
    explicit ArchiveWriter(const std::filesystem::path&);
    void write(const RawFrame&) override;
    void flush() override;
private:
    std::ofstream stream_;
};
class ArchiveReader {
public:
    explicit ArchiveReader(const std::filesystem::path&);
    bool next(RawFrame&); // false only at a clean record boundary at EOF.
private:
    std::ifstream stream_;
};
}
