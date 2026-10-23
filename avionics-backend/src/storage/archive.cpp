#include "core/archive.hpp"
#include "core/json.hpp"
#include "plugin_api/bus_plugin.h"
#include <algorithm>
#include <array>
#include <cerrno>
#include <fcntl.h>
#include <limits>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <sys/stat.h>
#if defined(_WIN32)
#include <io.h>
#else
#include <unistd.h>
#endif

namespace avionics {
namespace {
constexpr std::array<std::uint8_t, 8> magic_v2{'A','V','B','U','S','0','2','\n'};
constexpr std::array<std::uint8_t, 8> magic_v3{'A','V','B','U','S','0','3','\n'};
constexpr std::uint32_t max_body = BUS_MAX_PAYLOAD + 512;
template<class T> void put(std::vector<std::uint8_t>& out, T value) {
    static_assert(std::is_unsigned_v<T>);
    for (std::size_t i = 0; i < sizeof(T); ++i)
        out.push_back(static_cast<std::uint8_t>((value >> (8 * i)) & 0xffu));
}
template<class T> T get(const std::vector<std::uint8_t>& in, std::size_t& at) {
    static_assert(std::is_unsigned_v<T>);
    if (at + sizeof(T) > in.size()) throw ArchiveReadError("truncated archive field");
    std::uint64_t value{};
    for (std::size_t i = 0; i < sizeof(T); ++i) value |= std::uint64_t(in[at++]) << (8 * i);
    return static_cast<T>(value);
}
std::uint32_t crc32(std::span<const std::uint8_t> bytes) {
    std::uint32_t crc = 0xffffffffu;
    for (auto b : bytes) {
        crc ^= b;
        for (int bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}
void exact(std::istream& input, char* destination, std::streamsize size) {
    if (!input.read(destination, size)) throw ArchiveReadError("truncated or unreadable archive");
}
}
std::filesystem::path archiveVolumePath(const std::filesystem::path& first, std::uint32_t number) {
    if (number == 1) return first;
    std::ostringstream suffix; suffix << ".part" << std::setfill('0') << std::setw(6) << number << ".avbus";
    auto path = first; path += suffix.str(); return path;
}
const char* archiveIntegrityName(ArchiveIntegrity state) {
    constexpr const char* names[]{"unsealed", "sealed"};
    return names[static_cast<std::size_t>(state)];
}
void writeArchiveSummary(std::ostream& out, const ArchiveSummary& s) {
    out << "{\"format_version\":" << s.format_version << ",\"integrity\":" << jsonString(archiveIntegrityName(s.integrity))
        << ",\"recording_session_id\":" << jsonIdentity(s.session_id) << ",\"records\":" << s.records
        << ",\"data_bytes\":" << s.data_bytes << ",\"last_record_index\":" << s.last_record_index
        << ",\"data_sha256\":" << jsonIdentity(s.data_sha256) << ",\"file_sha256\":" << jsonIdentity(s.file_sha256)
        << ",\"volumes\":" << s.volumes << ",\"recovered\":" << (s.recovered ? "true" : "false") << '}';
}
ArchiveWriter::ArchiveWriter(const std::filesystem::path& path, std::optional<SessionId> identity, RecordingOptions options)
    : path_(path), options_(options) {
    if (options.volume_bytes && options.volume_bytes < 256) throw std::invalid_argument("volume_bytes must be zero or at least 256");
    if (options.volume_seconds > std::uint64_t(std::numeric_limits<std::int64_t>::max() / 1000000000))
        throw std::invalid_argument("volume_seconds exceeds monotonic clock range");
    summary_.session_id = identity ? *identity : newSessionId();
    summary_.format_version = options.volume_bytes || options.volume_seconds || options.recovered ? 3 : 2;
    summary_.recovered = options.recovered;
    openVolume();
}
void ArchiveWriter::openVolume(const ContentDigest& previous) {
    const auto path = archiveVolumePath(path_, summary_.volumes);
#if defined(_WIN32)
    const auto descriptor = _wopen(path.c_str(), _O_WRONLY | _O_CREAT | _O_EXCL | _O_BINARY | _O_NOINHERIT, _S_IREAD | _S_IWRITE);
#else
    const auto descriptor = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0666);
#endif
    if (descriptor < 0) throw std::system_error(errno, std::generic_category(), "exclusive archive creation failed");
#if defined(_WIN32)
    stream_.reset(_fdopen(descriptor, "wb"));
#else
    stream_.reset(fdopen(descriptor, "wb"));
#endif
    if (!stream_) {
        const auto reason = errno;
#if defined(_WIN32)
        _close(descriptor);
#else
        ::close(descriptor);
#endif
        throw std::system_error(reason, std::generic_category(), "archive stream creation failed");
    }
    volume_ = {}; volume_.data_bytes = 0; volume_bytes_ = 0;
    data_hash_ = std::make_unique<Sha256>(); volume_hash_ = std::make_unique<Sha256>();
    writeData(summary_.format_version == 3 ? magic_v3 : magic_v2); writeData(*summary_.session_id);
    if (summary_.format_version == 3) {
        std::vector<std::uint8_t> header; put(header, summary_.volumes);
        header.insert(header.end(), previous.begin(), previous.end()); writeData(header);
    }
    current_volume_ = summary_.volumes;
    flush();
}
void ArchiveWriter::writeBytes(std::span<const std::uint8_t> bytes) {
    if (std::fwrite(bytes.data(), 1, bytes.size(), stream_.get()) != bytes.size())
        throw std::system_error(errno, std::generic_category(), "archive write failed");
    file_hash_.update(bytes); volume_hash_->update(bytes); volume_bytes_.fetch_add(bytes.size());
}
void ArchiveWriter::writeData(std::span<const std::uint8_t> bytes) {
    writeBytes(bytes); data_hash_->update(bytes); all_data_hash_.update(bytes);
    summary_.data_bytes += bytes.size(); volume_.data_bytes += bytes.size();
}
void ArchiveWriter::write(const RawFrame& f) {
    if (summary_.integrity == ArchiveIntegrity::Sealed) throw std::logic_error("archive is already sealed");
    if (f.source.empty() || f.source.size() > 128 || f.origin_source.size() > 128 || f.payload.size() > BUS_MAX_PAYLOAD)
        throw std::invalid_argument("archive frame exceeds bounds");
    if (!f.record_index || f.record_index <= summary_.last_record_index)
        throw std::invalid_argument("archive record indices must be positive and increasing");
    if (f.recording_session_id && f.recording_session_id != summary_.session_id)
        throw std::invalid_argument("frame belongs to another recording session");
    std::vector<std::uint8_t> body;
    body.reserve(100 + f.source.size() + f.origin_source.size() + f.payload.size());
    put(body, f.protocol); put(body, f.channel); put(body, f.flags); put(body, f.clock_domain);
    put(body, f.capture_time_ns); put(body, f.ingest_time_ns); put(body, f.sequence);
    put(body, f.generation); put(body, f.record_index);
    put(body, f.origin_generation); put(body, f.origin_record_index);
    put(body, static_cast<std::uint16_t>(f.source.size()));
    put(body, static_cast<std::uint16_t>(f.origin_source.size()));
    put(body, static_cast<std::uint32_t>(f.payload.size()));
    body.insert(body.end(), f.source.begin(), f.source.end());
    body.insert(body.end(), f.origin_source.begin(), f.origin_source.end());
    body.insert(body.end(), f.payload.begin(), f.payload.end());
    if (f.origin_session_id) {
        put(body, std::uint16_t(1)); put(body, std::uint16_t(16));
        body.insert(body.end(), f.origin_session_id->begin(), f.origin_session_id->end());
    }
    std::vector<std::uint8_t> header;
    put(header, static_cast<std::uint32_t>(body.size())); put(header, crc32(body));
    const auto now = std::chrono::steady_clock::now();
    if (volume_.records && ((options_.volume_bytes && volume_.data_bytes + header.size() + body.size() + 65 > options_.volume_bytes) ||
        (options_.volume_seconds && now - volume_start_ >= std::chrono::seconds(options_.volume_seconds)))) {
        const auto previous = finishVolume(1); ++summary_.volumes; openVolume(previous);
    }
    if (!volume_.records) volume_start_ = now;
    writeData(header); writeData(body);
    ++summary_.records; ++volume_.records;
    summary_.last_record_index = volume_.last_record_index = f.record_index;
}
void ArchiveWriter::flush() {
    if (summary_.integrity == ArchiveIntegrity::Sealed) return;
    if (std::fflush(stream_.get()) != 0) throw std::system_error(errno, std::generic_category(), "archive flush failed");
}
void ArchiveWriter::finish() {
    if (summary_.integrity == ArchiveIntegrity::Sealed) return;
    finishVolume(options_.recovered ? 2 : 0);
    summary_.data_sha256 = all_data_hash_.finish(); summary_.file_sha256 = file_hash_.finish();
    summary_.integrity = ArchiveIntegrity::Sealed;
}
ContentDigest ArchiveWriter::finishVolume(std::uint8_t end) {
    const auto digest = data_hash_->finish();
    std::vector<std::uint8_t> body;
    put(body, volume_.records); put(body, volume_.data_bytes); put(body, volume_.last_record_index);
    body.insert(body.end(), digest.begin(), digest.end());
    if (summary_.format_version == 3) put(body, end);
    std::vector<std::uint8_t> header; put(header, std::uint32_t(0)); put(header, crc32(body));
    writeBytes(header); writeBytes(body);
    flush();
    if (std::fclose(stream_.release()) != 0) throw std::system_error(errno, std::generic_category(), "archive close failed");
    return volume_hash_->finish();
}
ArchiveReader::ArchiveReader(const std::filesystem::path& path) : path_(path) { openVolume(); }
void ArchiveReader::hashData(std::span<const std::uint8_t> bytes) {
    data_hash_->update(bytes); all_data_hash_.update(bytes); file_hash_.update(bytes); volume_hash_->update(bytes);
    summary_.data_bytes += bytes.size(); volume_.data_bytes += bytes.size();
}
void ArchiveReader::openVolume(const ContentDigest& previous) {
    const auto number = summary_.session_id ? summary_.volumes + 1 : 1;
    current_path_ = archiveVolumePath(path_, number); volume_ = {}; volume_.data_bytes = 0;
    if (stream_.is_open()) stream_.close();
    stream_.clear(); stream_.open(current_path_, std::ios::binary);
    if (!stream_) throw ArchiveReadError("cannot open archive volume: " + current_path_.string());
    std::array<std::uint8_t, 8> header{};
    exact(stream_, reinterpret_cast<char*>(header.data()), static_cast<std::streamsize>(header.size()));
    if (header != magic_v2 && header != magic_v3) throw ArchiveReadError("unsupported archive format");
    const auto version = header == magic_v3 ? 3u : 2u;
    if (number > 1 && version != 3) throw ArchiveReadError("invalid continuation volume format");
    SessionId identity{}; exact(stream_, reinterpret_cast<char*>(identity.data()), static_cast<std::streamsize>(identity.size()));
    if (number > 1 && summary_.session_id != identity) throw ArchiveReadError("volume session identity mismatch");
    std::vector<std::uint8_t> extension;
    if (version == 3) {
        extension.resize(36); exact(stream_, reinterpret_cast<char*>(extension.data()), 36);
        std::size_t at{};
        if (get<std::uint32_t>(extension, at) != number || !std::equal(previous.begin(), previous.end(), extension.begin() + 4))
            throw ArchiveReadError("volume number or previous volume SHA256 mismatch; open the first volume");
    }
    summary_.session_id = identity; summary_.format_version = version; summary_.volumes = number;
    data_hash_ = std::make_unique<Sha256>(); volume_hash_ = std::make_unique<Sha256>();
    hashData(header); hashData(identity); hashData(extension);
}
bool ArchiveReader::next(RawFrame& frame) {
    if (ended_) return false;
    while (true) {
    if (stream_.peek() == std::char_traits<char>::eof()) {
        if (stream_.bad()) throw ArchiveReadError("archive read failed");
        throw ArchiveReadError("unsealed archive: expected session footer");
    }
    std::vector<std::uint8_t> header(8);
    exact(stream_, reinterpret_cast<char*>(header.data()), 8);
    std::size_t at{};
    const auto size = get<std::uint32_t>(header, at);
    const auto expected_crc = get<std::uint32_t>(header, at);
    if (size == 0) {
        std::vector<std::uint8_t> footer(summary_.format_version == 3 ? 57 : 56);
        exact(stream_, reinterpret_cast<char*>(footer.data()), static_cast<std::streamsize>(footer.size()));
        if (crc32(footer) != expected_crc) throw ArchiveReadError("archive footer CRC mismatch");
        at = 0;
        const auto records = get<std::uint64_t>(footer, at), bytes = get<std::uint64_t>(footer, at), last = get<std::uint64_t>(footer, at);
        const auto digest = data_hash_->finish();
        if (records != volume_.records || bytes != volume_.data_bytes || last != volume_.last_record_index ||
            !std::equal(digest.begin(), digest.end(), footer.begin() + static_cast<std::ptrdiff_t>(at)))
            throw ArchiveReadError("archive seal mismatch: records, length, identity or SHA256");
        const auto end = summary_.format_version == 3 ? footer.back() : 0;
        if (end > 2) throw ArchiveReadError("invalid archive volume end marker");
        if (stream_.peek() != std::char_traits<char>::eof() || stream_.bad()) throw ArchiveReadError("archive has data after its seal");
        file_hash_.update(header); file_hash_.update(footer); volume_hash_->update(header); volume_hash_->update(footer);
        if (end == 1) { openVolume(volume_hash_->finish()); continue; }
        summary_.recovered = end == 2;
        summary_.data_sha256 = all_data_hash_.finish(); summary_.file_sha256 = file_hash_.finish();
        summary_.integrity = ArchiveIntegrity::Sealed; ended_ = true; return false;
    }
    if (size < 80 || size > max_body) throw ArchiveReadError("invalid archive record size");
    std::vector<std::uint8_t> body(size);
    exact(stream_, reinterpret_cast<char*>(body.data()), static_cast<std::streamsize>(body.size()));
    if (crc32(body) != expected_crc) throw ArchiveReadError("archive CRC mismatch");
    at = 0;
    RawFrame f;
    f.protocol = get<std::uint32_t>(body, at); f.channel = get<std::uint32_t>(body, at);
    f.flags = get<std::uint32_t>(body, at); f.clock_domain = get<std::uint32_t>(body, at);
    f.capture_time_ns = get<std::uint64_t>(body, at); f.ingest_time_ns = get<std::uint64_t>(body, at);
    f.sequence = get<std::uint64_t>(body, at); f.generation = get<std::uint64_t>(body, at);
    f.record_index = get<std::uint64_t>(body, at); f.origin_generation = get<std::uint64_t>(body, at);
    f.origin_record_index = get<std::uint64_t>(body, at);
    const auto source_size = get<std::uint16_t>(body, at);
    const auto origin_size = get<std::uint16_t>(body, at);
    const auto payload_size = get<std::uint32_t>(body, at);
    const auto data_end = at + source_size + origin_size + payload_size;
    if (source_size == 0 || source_size > 128 || origin_size > 128 || payload_size > BUS_MAX_PAYLOAD || data_end > body.size())
        throw ArchiveReadError("invalid archive field sizes");
    f.source.assign(reinterpret_cast<const char*>(body.data() + at), source_size); at += source_size;
    f.origin_source.assign(reinterpret_cast<const char*>(body.data() + at), origin_size); at += origin_size;
    f.payload.assign(body.begin() + static_cast<std::ptrdiff_t>(at), body.begin() + static_cast<std::ptrdiff_t>(data_end)); at = data_end;
    while (at < body.size()) {
        const auto type = get<std::uint16_t>(body, at), length = get<std::uint16_t>(body, at);
        if (length > body.size() - at) throw ArchiveReadError("truncated archive extension");
        if (type == 1) {
            if (length != 16 || f.origin_session_id) throw ArchiveReadError("invalid origin session extension");
            SessionId identity{}; std::copy_n(body.begin() + static_cast<std::ptrdiff_t>(at), 16, identity.begin()); f.origin_session_id = identity;
        } else if (!type || (type & 0x8000u)) throw ArchiveReadError("unsupported required archive extension");
        at += length;
    }
    if (!f.record_index || f.record_index <= summary_.last_record_index)
        throw ArchiveReadError("invalid archive record identity order");
    f.recording_session_id = summary_.session_id;
    ++summary_.records; ++volume_.records; summary_.last_record_index = volume_.last_record_index = f.record_index;
    hashData(header); hashData(body);
    frame = std::move(f);
    return true;
    }
}
void writeArchiveCheck(std::ostream& out, const ArchiveCheck& check) {
    const auto name = check.stopped_file.u8string();
    out << "{\"status\":" << jsonString(check.complete ? "complete" : "incomplete") << ",\"archive\":";
    writeArchiveSummary(out, check.archive);
    out << ",\"error\":" << jsonString(check.error) << ",\"stopped_file\":" << jsonString(std::string(name.begin(), name.end()))
        << ",\"valid_volume_bytes\":" << check.valid_volume_bytes << '}';
}
ArchiveCheck checkArchive(const std::filesystem::path& path) {
    ArchiveCheck result; result.stopped_file = path;
    std::unique_ptr<ArchiveReader> reader;
    try {
        reader = std::make_unique<ArchiveReader>(path); RawFrame frame;
        while (reader->next(frame)) {}
        result.complete = true;
    } catch (const ArchiveReadError& error) { result.error = error.what(); }
    if (reader) {
        result.archive = reader->summary(); result.stopped_file = reader->currentPath(); result.valid_volume_bytes = reader->validVolumeBytes();
    }
    return result;
}
ArchiveCheck recoverArchive(const std::filesystem::path& source, const std::filesystem::path& destination) {
    ArchiveReader reader(source);
    ArchiveWriter writer(destination, reader.summary().session_id, {268435456, 0, true});
    ArchiveCheck result;
    try {
        RawFrame frame;
        while (reader.next(frame)) writer.write(frame);
        result.complete = true;
    } catch (const ArchiveReadError& error) { result.error = error.what(); }
    writer.finish();
    result.archive = reader.summary(); result.stopped_file = reader.currentPath(); result.valid_volume_bytes = reader.validVolumeBytes();
    return result;
}
}
