#include "core/archive.hpp"
#include "plugin_api/bus_plugin.h"
#include <array>
#include <limits>
#include <stdexcept>

namespace avionics {
namespace {
constexpr std::array<char, 8> magic{'A','V','B','U','S','0','1','\n'};
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
std::uint32_t crc32(const std::vector<std::uint8_t>& bytes) {
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
ArchiveWriter::ArchiveWriter(const std::filesystem::path& path) {
    if (std::filesystem::exists(path)) throw std::runtime_error("refusing to overwrite archive: " + path.string());
    stream_.open(path, std::ios::binary | std::ios::out);
    if (!stream_) throw std::runtime_error("cannot create archive: " + path.string());
    stream_.write(magic.data(), static_cast<std::streamsize>(magic.size()));
    flush();
}
void ArchiveWriter::write(const RawFrame& f) {
    if (f.source.empty() || f.source.size() > 128 || f.origin_source.size() > 128 || f.payload.size() > BUS_MAX_PAYLOAD)
        throw std::invalid_argument("archive frame exceeds bounds");
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
    std::vector<std::uint8_t> header;
    put(header, static_cast<std::uint32_t>(body.size())); put(header, crc32(body));
    stream_.write(reinterpret_cast<const char*>(header.data()), static_cast<std::streamsize>(header.size()));
    stream_.write(reinterpret_cast<const char*>(body.data()), static_cast<std::streamsize>(body.size()));
    if (!stream_) throw std::runtime_error("archive write failed");
}
void ArchiveWriter::flush() {
    stream_.flush();
    if (!stream_) throw std::runtime_error("archive flush failed");
}
ArchiveReader::ArchiveReader(const std::filesystem::path& path) : stream_(path, std::ios::binary) {
    if (!stream_) throw std::runtime_error("cannot open archive: " + path.string());
    std::array<char, 8> header{};
    exact(stream_, header.data(), static_cast<std::streamsize>(header.size()));
    if (header != magic) throw std::runtime_error("unsupported archive format");
}
bool ArchiveReader::next(RawFrame& frame) {
    if (stream_.peek() == std::char_traits<char>::eof()) {
        if (stream_.bad()) throw std::runtime_error("archive read failed");
        return false;
    }
    std::vector<std::uint8_t> header(8);
    exact(stream_, reinterpret_cast<char*>(header.data()), 8);
    std::size_t at{};
    const auto size = get<std::uint32_t>(header, at);
    const auto expected_crc = get<std::uint32_t>(header, at);
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
    if (source_size == 0 || source_size > 128 || origin_size > 128 || payload_size > BUS_MAX_PAYLOAD ||
        at + source_size + origin_size + payload_size != body.size()) throw std::runtime_error("invalid archive field sizes");
    f.source.assign(reinterpret_cast<const char*>(body.data() + at), source_size); at += source_size;
    f.origin_source.assign(reinterpret_cast<const char*>(body.data() + at), origin_size); at += origin_size;
    f.payload.assign(body.begin() + static_cast<std::ptrdiff_t>(at), body.end());
    frame = std::move(f);
    return true;
}
}
