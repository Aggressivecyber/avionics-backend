#pragma once
#include "core/types.hpp"
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <memory>

namespace avionics {
class IFrameRecorder {
public:
    virtual ~IFrameRecorder() = default;
    virtual void write(const RawFrame&) = 0;
    virtual void flush() = 0;
    virtual void finish() { flush(); }
};
enum class ArchiveIntegrity { Unsealed, LegacyUnverified, Sealed };
struct ArchiveSummary {
    std::uint32_t format_version{2};
    ArchiveIntegrity integrity{ArchiveIntegrity::Unsealed};
    std::optional<SessionId> session_id;
    std::uint64_t records{}, data_bytes{}, last_record_index{};
    std::optional<ContentDigest> data_sha256, file_sha256;
};
const char* archiveIntegrityName(ArchiveIntegrity);
void writeArchiveSummary(std::ostream&, const ArchiveSummary&);
class ArchiveWriter final : public IFrameRecorder {
public:
    explicit ArchiveWriter(const std::filesystem::path&, std::optional<SessionId> = std::nullopt);
    void write(const RawFrame&) override;
    void flush() override;
    void finish() override;
    const ArchiveSummary& summary() const noexcept { return summary_; }
private:
    void writeBytes(std::span<const std::uint8_t>);
    std::unique_ptr<std::FILE, int(*)(std::FILE*)> stream_{nullptr, &std::fclose};
    Sha256 data_hash_, file_hash_;
    ArchiveSummary summary_;
};
class ArchiveReader {
public:
    explicit ArchiveReader(const std::filesystem::path&);
    bool next(RawFrame&); // v2 EOF requires a verified seal; v1 remains explicitly unverified.
    const ArchiveSummary& summary() const noexcept { return summary_; }
private:
    std::ifstream stream_;
    Sha256 data_hash_, file_hash_;
    ArchiveSummary summary_;
    bool ended_{};
};
}
