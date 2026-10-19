#pragma once
#include "core/types.hpp"
#include "core/plugin_manager.hpp"
#include <cstring>
#include <span>
#include <stdexcept>

namespace avionics::wire {
constexpr std::uint32_t max_packet = BUS_MAX_PAYLOAD + 8192;
constexpr std::uint32_t protocol_version = 1;
enum class Kind : std::uint8_t { Load = 1, Configure, Start, Stop, Shutdown, Reply = 16, Status, Frame };
template<class T> void put(std::vector<std::uint8_t>& bytes, T value) {
    static_assert(std::is_unsigned_v<T>);
    for (std::size_t i = 0; i < sizeof(T); ++i) bytes.push_back(static_cast<std::uint8_t>(value >> (8 * i)));
}
template<class T> T take(std::span<const std::uint8_t>& bytes) {
    static_assert(std::is_unsigned_v<T>);
    if (bytes.size() < sizeof(T)) throw std::runtime_error("truncated worker packet");
    std::uint64_t value{}; for (std::size_t i = 0; i < sizeof(T); ++i) value |= std::uint64_t(bytes[i]) << (8 * i);
    bytes = bytes.subspan(sizeof(T)); return static_cast<T>(value);
}
inline void text(std::vector<std::uint8_t>& bytes, const std::string& value) {
    put(bytes, static_cast<std::uint32_t>(value.size())); bytes.insert(bytes.end(), value.begin(), value.end());
}
inline std::string text(std::span<const std::uint8_t>& bytes, std::size_t maximum = 4096) {
    const auto size = take<std::uint32_t>(bytes);
    if (size > maximum || size > bytes.size()) throw std::runtime_error("invalid worker string length");
    std::string result(reinterpret_cast<const char*>(bytes.data()), size); bytes = bytes.subspan(size); return result;
}
inline void end(std::span<const std::uint8_t> bytes) { if (!bytes.empty()) throw std::runtime_error("unexpected worker packet fields"); }
inline std::vector<std::uint8_t> packet(Kind kind) { return {static_cast<std::uint8_t>(kind)}; }
inline std::vector<std::uint8_t> envelope(std::span<const std::uint8_t> payload) {
    if (payload.empty() || payload.size() > max_packet) throw std::runtime_error("worker packet exceeds limit");
    std::vector<std::uint8_t> bytes; bytes.reserve(payload.size() + 4); put(bytes, static_cast<std::uint32_t>(payload.size()));
    bytes.insert(bytes.end(), payload.begin(), payload.end()); return bytes;
}
template<class Reader> bool receive(Reader& reader, std::vector<std::uint8_t>& payload) {
    std::array<std::uint8_t, 4> header{}; if (!reader.read(header)) return false;
    std::span<const std::uint8_t> cursor(header); const auto length = take<std::uint32_t>(cursor);
    if (!length || length > max_packet) throw std::runtime_error("invalid worker packet length");
    payload.resize(length); if (!reader.read(payload)) throw std::runtime_error("truncated worker packet body"); return true;
}
inline void status(std::vector<std::uint8_t>& out, const AdapterStatus& s) {
    put(out, static_cast<std::uint32_t>(s.state)); put(out, s.generation); put(out, s.emitted); put(out, s.rejected);
    put(out, s.errors); put(out, s.malformed); put(out, s.plugin_abi); text(out, s.failure);
}
inline AdapterStatus status(std::span<const std::uint8_t>& in) {
    AdapterStatus s; const auto state = take<std::uint32_t>(in);
    if (state > static_cast<unsigned>(AdapterState::Faulted)) throw std::runtime_error("invalid worker state");
    s.state = static_cast<AdapterState>(state); s.generation = take<std::uint64_t>(in); s.emitted = take<std::uint64_t>(in);
    s.rejected = take<std::uint64_t>(in); s.errors = take<std::uint64_t>(in); s.malformed = take<std::uint64_t>(in);
    s.plugin_abi = take<std::uint32_t>(in); s.failure = text(in); return s;
}
inline void frame(std::vector<std::uint8_t>& out, const RawFrame& f) {
    put(out, f.generation); put(out, f.protocol); put(out, f.channel); put(out, f.flags); put(out, f.clock_domain);
    put(out, f.capture_time_ns); put(out, f.sequence); text(out, f.origin_source); put(out, f.origin_generation); put(out, f.origin_record_index);
    put(out, std::uint8_t(f.origin_session_id ? 1 : 0));
    if (f.origin_session_id) out.insert(out.end(), f.origin_session_id->begin(), f.origin_session_id->end());
    put(out, static_cast<std::uint32_t>(f.payload.size())); out.insert(out.end(), f.payload.begin(), f.payload.end());
}
inline RawFrame frame(std::span<const std::uint8_t>& in) {
    RawFrame f; f.generation = take<std::uint64_t>(in); f.protocol = take<std::uint32_t>(in); f.channel = take<std::uint32_t>(in);
    f.flags = take<std::uint32_t>(in); f.clock_domain = take<std::uint32_t>(in); f.capture_time_ns = take<std::uint64_t>(in); f.sequence = take<std::uint64_t>(in);
    f.origin_source = text(in, 128); f.origin_generation = take<std::uint64_t>(in); f.origin_record_index = take<std::uint64_t>(in);
    const auto has_identity = take<std::uint8_t>(in);
    if (has_identity > 1) throw std::runtime_error("invalid worker identity flag");
    if (has_identity) {
        if (in.size() < 16) throw std::runtime_error("truncated worker identity");
        SessionId identity{}; std::memcpy(identity.data(), in.data(), 16); in = in.subspan(16); f.origin_session_id = identity;
    }
    const auto length = take<std::uint32_t>(in);
    if (length > BUS_MAX_PAYLOAD || length != in.size()) throw std::runtime_error("invalid worker payload length");
    f.payload.assign(in.begin(), in.end()); in = {}; return f;
}
}
