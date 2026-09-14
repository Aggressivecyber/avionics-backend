#include "core/processing.hpp"
#include <cmath>
#include <iomanip>
#include <locale>
#include <sstream>
#include <stdexcept>

namespace avionics {
namespace {
std::string json(const std::string& text) {
    std::ostringstream output; output << '"';
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') output << '\\' << c;
        else if (c < 32) output << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c) << std::dec;
        else output << c;
    }
    output << '"'; return output.str();
}
void value(std::ostream& out, const ParameterValue& item) {
    std::visit([&](const auto& data) {
        using T = std::decay_t<decltype(data)>;
        if constexpr (std::is_same_v<T, EnumValue>) out << "{\"code\":" << data.code << ",\"label\":" << json(data.label) << '}';
        else if constexpr (std::is_same_v<T, bool>) out << (data ? "true" : "false");
        else if constexpr (std::is_floating_point_v<T>) { if (std::isfinite(data)) out << data; else out << "null"; }
        else out << data;
    }, item);
}
}
JsonlAnalysisSink::JsonlAnalysisSink(const std::filesystem::path& directory) {
    if (!std::filesystem::create_directory(directory)) throw std::runtime_error("analysis output directory must be new");
    parameters_.open(directory / "parameters.jsonl", std::ios::binary);
    events_.open(directory / "events.jsonl", std::ios::binary);
    if (!parameters_ || !events_) throw std::runtime_error("cannot create analysis outputs");
    parameters_.imbue(std::locale::classic()); events_.imbue(std::locale::classic());
    parameters_ << std::setprecision(17); events_ << std::setprecision(17);
}
void JsonlAnalysisSink::sample(const AlignedSample& aligned) {
    const auto& s = aligned.sample;
    parameters_ << "{\"schema\":1,\"source\":" << json(s.source) << ",\"parameter\":" << json(s.parameter_id)
        << ",\"generation\":" << s.generation << ",\"origin_generation\":" << s.origin_generation
        << ",\"record_index\":" << s.raw_record_index << ",\"sequence\":" << s.sequence
        << ",\"capture_time_ns\":" << s.capture_time_ns << ",\"ingest_time_ns\":" << s.ingest_time_ns
        << ",\"clock_domain\":" << s.clock_domain << ",\"time_group\":" << json(aligned.time_group) << ",\"time_ns\":";
    if (aligned.time_ns) parameters_ << *aligned.time_ns; else parameters_ << "null";
    parameters_ << ",\"uncertainty_ns\":" << aligned.uncertainty_ns << ",\"max_age_ns\":" << s.max_age_ns
        << ",\"unit\":" << json(s.unit) << ",\"dictionary_version\":" << json(s.decoder_version)
        << ",\"value\":"; value(parameters_, s.value);
    parameters_ << ",\"payload_valid\":" << (s.valid ? "true" : "false")
        << ",\"usable\":" << (aligned.usable() ? "true" : "false") << ",\"quality\":[";
    for (std::size_t i = 0; i < aligned.quality.size(); ++i) { if (i) parameters_ << ','; parameters_ << json(aligned.quality[i]); }
    parameters_ << "]}\n";
    if (!parameters_) throw std::runtime_error("parameter output write failed");
}
void JsonlAnalysisSink::result(const CorrelationResult& r) {
    events_ << "{\"schema\":1,\"rule\":" << json(r.rule) << ",\"outcome\":" << json(r.outcome)
        << ",\"time_group\":" << json(r.time_group) << ",\"time_ns\":" << r.time_ns << ",\"metric\":";
    if (r.metric && std::isfinite(*r.metric)) events_ << *r.metric; else events_ << "null";
    events_ << ",\"latency_low_ns\":"; if (r.latency_low_ns) events_ << *r.latency_low_ns; else events_ << "null";
    events_ << ",\"latency_high_ns\":"; if (r.latency_high_ns) events_ << *r.latency_high_ns; else events_ << "null";
    events_ << ",\"evidence\":[";
    for (std::size_t i = 0; i < r.evidence.size(); ++i) {
        if (i) events_ << ',';
        const auto& e = r.evidence[i];
        events_ << "{\"source\":" << json(e.source) << ",\"parameter\":" << json(e.parameter)
            << ",\"generation\":" << e.generation << ",\"origin_generation\":" << e.origin_generation
            << ",\"record_index\":" << e.record_index << ",\"time_ns\":" << e.time_ns
            << ",\"dictionary_version\":" << json(e.decoder_version) << '}';
    }
    events_ << "]}\n";
    if (!events_) throw std::runtime_error("event output write failed");
}
void JsonlAnalysisSink::flush() {
    parameters_.flush(); events_.flush();
    if (!parameters_ || !events_) throw std::runtime_error("analysis output flush failed");
}
}
