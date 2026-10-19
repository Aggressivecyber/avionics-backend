#include "core/archive.hpp"
#include "core/json.hpp"
#include "plugin_api/bus_plugin.h"
#include <algorithm>
#include <array>
#include <cerrno>
#include <fcntl.h>
#include <limits>
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
constexpr std::uint32_t max_body = BUS_MAX_PAYLOAD + 512;
template<class T> void put(std::vector<std::uint8_t>& out, T value) {
    static_assert(std::is_unsigned_v<T>);
    for (std::size_t i = 0; i < sizeof(T); ++i)
        out.push_back(static_cast<std::uint8_t>((value >> (8 * i)) & 0xffu));
}
template<class T> T get(const std::vector<std::uint8_t>& in, std::size_t& at) {
    static_assert(std::is_unsigned_v<T>);
    if (at + sizeof(T) > in.size()) throw std::runtime_error("truncated archive field");
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
    if (!input.read(destination, size)) throw std::runtime_error("truncated or unreadable archive");
}
}
const char* archiveIntegrityName(ArchiveIntegrity state) {
    constexpr const char* names[]{"unsealed", "sealed"};
    return names[static_cast<std::size_t>(state)];
}
void writeArchiveSummary(std::ostream& out, const ArchiveSummary& s) {
    out << "{\"format_version\":" << s.format_version << ",\"integrity\":" << jsonString(archiveIntegrityName(s.integrity))
        << ",\"recording_session_id\":" << jsonIdentity(s.session_id) << ",\"records\":" << s.records
        << ",\"data_bytes\":" << s.data_bytes << ",\"last_record_index\":" << s.last_record_index
        << ",\"data_sha256\":" << jsonIdentity(s.data_sha256) << ",\"file_sha256\":" << jsonIdentity(s.file_sha256) << '}';
}
ArchiveWriter::ArchiveWriter(const std::filesystem::path& path, std::optional<SessionId> identity) {
    summary_.session_id = identity ? *identity : newSessionId();
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
    writeBytes(magic_v2); writeBytes(*summary_.session_id);
    data_hash_.update(magic_v2); data_hash_.update(*summary_.session_id);
    file_hash_.update(magic_v2); file_hash_.update(*summary_.session_id);
    summary_.data_bytes = 24;
    flush();
}
void ArchiveWriter::writeBytes(std::span<const std::uint8_t> bytes) {
    if (std::fwrite(bytes.data(), 1, bytes.size(), stream_.get()) != bytes.size())
        throw std::system_error(errno, std::generic_category(), "archive write failed");
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
    writeBytes(header); writeBytes(body);
    data_hash_.update(header); data_hash_.update(body); file_hash_.update(header); file_hash_.update(body);
    ++summary_.records; summary_.data_bytes += header.size() + body.size(); summary_.last_record_index = f.record_index;
}
void ArchiveWriter::flush() {
    if (summary_.integrity == ArchiveIntegrity::Sealed) return;
    if (std::fflush(stream_.get()) != 0) throw std::system_error(errno, std::generic_category(), "archive flush failed");
}
void ArchiveWriter::finish() {
    if (summary_.integrity == ArchiveIntegrity::Sealed) return;
    const auto digest = data_hash_.finish();
    std::vector<std::uint8_t> body;
    put(body, summary_.records); put(body, summary_.data_bytes); put(body, summary_.last_record_index);
    body.insert(body.end(), digest.begin(), digest.end());
    std::vector<std::uint8_t> header; put(header, std::uint32_t(0)); put(header, crc32(body));
    writeBytes(header); writeBytes(body); file_hash_.update(header); file_hash_.update(body);
    flush();
    if (std::fclose(stream_.release()) != 0) throw std::system_error(errno, std::generic_category(), "archive close failed");
    summary_.data_sha256 = digest; summary_.file_sha256 = file_hash_.finish(); summary_.integrity = ArchiveIntegrity::Sealed;
}
ArchiveReader::ArchiveReader(const std::filesystem::path& path) : stream_(path, std::ios::binary) {
    if (!stream_) throw std::runtime_error("cannot open archive: " + path.string());
    std::array<std::uint8_t, 8> header{};
    exact(stream_, reinterpret_cast<char*>(header.data()), static_cast<std::streamsize>(header.size()));
    if (header != magic_v2) throw std::runtime_error("unsupported archive format; rebuild pre-release inputs using v2");
    data_hash_.update(header); file_hash_.update(header); summary_.data_bytes = 8;
    SessionId identity{}; exact(stream_, reinterpret_cast<char*>(identity.data()), static_cast<std::streamsize>(identity.size()));
    summary_.session_id = identity; summary_.data_bytes += identity.size(); data_hash_.update(identity); file_hash_.update(identity);
}
bool ArchiveReader::next(RawFrame& frame) {
    if (ended_) return false;
    if (stream_.peek() == std::char_traits<char>::eof()) {
        if (stream_.bad()) throw std::runtime_error("archive read failed");
        throw std::runtime_error("unsealed archive: expected session footer");
    }
    std::vector<std::uint8_t> header(8);
    exact(stream_, reinterpret_cast<char*>(header.data()), 8);
    std::size_t at{};
    const auto size = get<std::uint32_t>(header, at);
    const auto expected_crc = get<std::uint32_t>(header, at);
    if (size == 0) {
        std::vector<std::uint8_t> footer(56); exact(stream_, reinterpret_cast<char*>(footer.data()), 56);
        if (crc32(footer) != expected_crc) throw std::runtime_error("archive footer CRC mismatch");
        at = 0;
        const auto records = get<std::uint64_t>(footer, at), bytes = get<std::uint64_t>(footer, at), last = get<std::uint64_t>(footer, at);
        const auto digest = data_hash_.finish();
        if (records != summary_.records || bytes != summary_.data_bytes || last != summary_.last_record_index ||
            !std::equal(digest.begin(), digest.end(), footer.begin() + static_cast<std::ptrdiff_t>(at)))
            throw std::runtime_error("archive seal mismatch: records, length, identity or SHA256");
        file_hash_.update(header); file_hash_.update(footer);
        if (stream_.peek() != std::char_traits<char>::eof() || stream_.bad()) throw std::runtime_error("archive has data after its seal");
        summary_.data_sha256 = digest; summary_.file_sha256 = file_hash_.finish();
        summary_.integrity = ArchiveIntegrity::Sealed; ended_ = true; return false;
    }
    if (size < 80 || size > max_body) throw std::runtime_error("invalid archive record size");
    std::vector<std::uint8_t> body(size);
    exact(stream_, reinterpret_cast<char*>(body.data()), static_cast<std::streamsize>(body.size()));
    if (crc32(body) != expected_crc) throw std::runtime_error("archive CRC mismatch");
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
        throw std::runtime_error("invalid archive field sizes");
    f.source.assign(reinterpret_cast<const char*>(body.data() + at), source_size); at += source_size;
    f.origin_source.assign(reinterpret_cast<const char*>(body.data() + at), origin_size); at += origin_size;
    f.payload.assign(body.begin() + static_cast<std::ptrdiff_t>(at), body.begin() + static_cast<std::ptrdiff_t>(data_end)); at = data_end;
    while (at < body.size()) {
        const auto type = get<std::uint16_t>(body, at), length = get<std::uint16_t>(body, at);
        if (length > body.size() - at) throw std::runtime_error("truncated archive extension");
        if (type == 1) {
            if (length != 16 || f.origin_session_id) throw std::runtime_error("invalid origin session extension");
            SessionId identity{}; std::copy_n(body.begin() + static_cast<std::ptrdiff_t>(at), 16, identity.begin()); f.origin_session_id = identity;
        } else if (!type || (type & 0x8000u)) throw std::runtime_error("unsupported required archive extension");
        at += length;
    }
    if (!f.record_index || f.record_index <= summary_.last_record_index)
        throw std::runtime_error("invalid archive record identity order");
    f.recording_session_id = summary_.session_id;
    ++summary_.records; summary_.data_bytes += header.size() + body.size(); summary_.last_record_index = f.record_index;
    data_hash_.update(header); data_hash_.update(body); file_hash_.update(header); file_hash_.update(body);
    frame = std::move(f);
    return true;
}
}
