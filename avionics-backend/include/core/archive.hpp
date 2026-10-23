#pragma once
#include "core/types.hpp"
#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <memory>
#include <stdexcept>

namespace avionics {
class IFrameRecorder {
public:
    virtual ~IFrameRecorder() = default;
    virtual void write(const RawFrame&) = 0;
    virtual void flush() = 0;
    virtual void finish() { flush(); }
};
enum class ArchiveIntegrity { Unsealed, Sealed };
struct RecordingOptions {
    std::uint64_t volume_bytes{}, volume_seconds{};
    bool recovered{};
};
struct ArchiveSummary {
    std::uint32_t format_version{2};
    ArchiveIntegrity integrity{ArchiveIntegrity::Unsealed};
    std::optional<SessionId> session_id;
    std::uint64_t records{}, data_bytes{}, last_record_index{};
    std::optional<ContentDigest> data_sha256, file_sha256;
    std::uint32_t volumes{1};
    bool recovered{};
};
class ArchiveReadError : public std::runtime_error { public: using std::runtime_error::runtime_error; };
std::filesystem::path archiveVolumePath(const std::filesystem::path&, std::uint32_t);
const char* archiveIntegrityName(ArchiveIntegrity);
void writeArchiveSummary(std::ostream&, const ArchiveSummary&);
class ArchiveWriter final : public IFrameRecorder {
public:
    explicit ArchiveWriter(const std::filesystem::path&, std::optional<SessionId> = std::nullopt, RecordingOptions = {});
    void write(const RawFrame&) override;
    void flush() override;
    void finish() override;
    const ArchiveSummary& summary() const noexcept { return summary_; }
    std::uint32_t currentVolume() const noexcept { return current_volume_.load(); }
    std::uint64_t volumeBytes() const noexcept { return volume_bytes_.load(); }
private:
    void writeBytes(std::span<const std::uint8_t>);
    void writeData(std::span<const std::uint8_t>);
    void openVolume(const ContentDigest& previous = {});
    ContentDigest finishVolume(std::uint8_t end);
    std::filesystem::path path_;
    RecordingOptions options_;
    std::unique_ptr<std::FILE, int(*)(std::FILE*)> stream_{nullptr, &std::fclose};
    Sha256 all_data_hash_, file_hash_;
    std::unique_ptr<Sha256> data_hash_, volume_hash_;
    ArchiveSummary summary_, volume_;
    std::chrono::steady_clock::time_point volume_start_;
    std::atomic<std::uint32_t> current_volume_{1};
    std::atomic<std::uint64_t> volume_bytes_{};
};
class ArchiveReader {
public:
    explicit ArchiveReader(const std::filesystem::path&);
    bool next(RawFrame&); // EOF requires a verified seal.
    const ArchiveSummary& summary() const noexcept { return summary_; }
    const std::filesystem::path& currentPath() const noexcept { return current_path_; }
    std::uint64_t validVolumeBytes() const noexcept { return volume_.data_bytes; }
private:
    void openVolume(const ContentDigest& previous = {});
    void hashData(std::span<const std::uint8_t>);
    std::filesystem::path path_, current_path_;
    std::ifstream stream_;
    Sha256 all_data_hash_, file_hash_;
    std::unique_ptr<Sha256> data_hash_, volume_hash_;
    ArchiveSummary summary_, volume_;
    bool ended_{};
};
struct ArchiveCheck {
    ArchiveSummary archive;
    bool complete{};
    std::string error;
    std::filesystem::path stopped_file;
    std::uint64_t valid_volume_bytes{};
};
ArchiveCheck checkArchive(const std::filesystem::path&);
ArchiveCheck recoverArchive(const std::filesystem::path& source, const std::filesystem::path& destination);
void writeArchiveCheck(std::ostream&, const ArchiveCheck&);
}
