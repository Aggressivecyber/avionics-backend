#include "core/processing.hpp"
#include "core/demo_fixture.hpp"
#include <cmath>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>

using namespace avionics;
namespace {
void check(bool value, const std::string& message) { if (!value) throw std::runtime_error(message); }
template<class F> void throws(F&& function, const std::string& message) {
    bool caught = false;
    try { function(); } catch (const std::exception&) { caught = true; }
    check(caught, message);
}
class MemorySink final : public IAnalysisSink {
public:
    void sample(const AlignedSample& item) override { samples.push_back(item); }
    void result(const CorrelationResult& item) override { results.push_back(item); }
    void flush() override {}
    std::vector<AlignedSample> samples;
    std::vector<CorrelationResult> results;
};
ParameterSample sample(const std::string& source, std::uint64_t ms, double value, std::uint64_t sequence) {
    ParameterSample result;
    result.source = source; result.parameter_id = source == "control" ? "command" : "position";
    result.capture_time_ns = 1000000000 + ms * 1000000; result.ingest_time_ns = result.capture_time_ns + 1;
    result.generation = 1; result.clock_domain = 1; result.sequence = sequence;
    result.max_age_ns = 50000000; result.valid = true; result.value = value;
    result.decoder_version = "test-v1"; result.unit = "ratio"; result.raw_record_index = ms + 1;
    return result;
}
void baseline(ProcessingService& service) { service.consume(sample("feedback", 0, 0, 0)); service.consume(sample("control", 0, 0, 0)); }
std::uint64_t count(const ProcessingService& service, const std::string& outcome) {
    const auto stats = service.stats(); const auto found = stats.outcomes.find(outcome);
    return found == stats.outcomes.end() ? 0 : found->second;
}
void testConfiguration(const std::filesystem::path& path, const std::filesystem::path& output) {
    std::ifstream stream(path); std::ostringstream text; text << stream.rdbuf(); const auto original = text.str();
    auto reject = [&](std::string modified) {
        const auto file = output / "invalid.ini";
        { std::ofstream out(file); out << modified; }
        throws([&] { BackendConfiguration::load(file); }, "bad configuration was accepted");
    };
    auto replace = [&](const std::string& from, const std::string& to) {
        auto changed = original; const auto at = changed.find(from); check(at != std::string::npos, "test fixture key missing");
        changed.replace(at, from.size(), to); reject(changed);
    };
    replace("bit_width=64", "bit_width=0");
    replace("scale=0.001", "scale=nan");
    replace("max_age_ms=50", "max_age_ms=-1");
    replace("type=signed", "type=mystery");
    replace("command_parameter=command", "command_parameter=missing");
    replace("schema=1", "schema=2");
    replace("window_ms=5", "window_ms=5\nwindow_ms=5");
    reject(original + "\n[unknown section]\nx=1\n");
    const auto valid = BackendConfiguration::load(path);
    check(valid.fields.size() == 4 && valid.responses.size() == 1 && valid.consistency.size() == 1, "configuration lost rules");
}
void testDictionary() {
    BackendConfiguration cfg; cfg.version = "dict-test";
    FieldDefinition field; field.id = "x"; field.source = "a"; field.protocol = 1; field.unit = "unit";
    field.bit_offset = 4; field.bit_width = 12; field.type = FieldType::Signed; field.max_age_ns = 1000;
    RawFrame raw; raw.source = "a"; raw.protocol = 1; raw.generation = 2; raw.record_index = 7;
    auto decode = [&] { cfg.fields = {field}; return DictionaryDecoder(cfg).decode(raw).at(0); };
    raw.payload = {0xb0, 0xfa};
    check(std::get<std::int64_t>(decode().value) == -85, "cross-byte little-endian sign extension failed");
    field.big_endian = true; field.type = FieldType::Unsigned; raw.payload = {0xab, 0xcd};
    check(std::get<std::uint64_t>(decode().value) == 3021, "cross-byte big-endian bit numbering failed");
    field.bit_offset = 0; field.bit_width = 64; raw.payload.assign(8, 0xff);
    check(std::get<std::uint64_t>(decode().value) == UINT64_MAX, "64-bit unsigned precision lost");
    field.type = FieldType::Signed;
    check(std::get<std::int64_t>(decode().value) == -1, "64-bit negative value failed");
    field.type = FieldType::Float64; raw.payload = {0x3f,0xf8,0,0,0,0,0,0};
    check(std::get<double>(decode().value) == 1.5, "IEEE float64 decode failed");
    field.scale = 2; field.offset = -1;
    check(std::get<double>(decode().value) == 2, "scale and offset failed");
    field.scale = 1; field.offset = 0; raw.payload = {0x7f,0xf8,0,0,0,0,0,0};
    check(!decode().valid, "NaN must be invalid");
    field.bit_width = 1; field.type = FieldType::Boolean; raw.payload = {0x80};
    check(std::get<bool>(decode().value), "boolean bit decode failed");
    field.valid_bit = 1; check(!decode().valid, "invalid status bit ignored"); field.valid_bit.reset();
    field.bit_width = 2; field.type = FieldType::Enumeration; field.enum_values = {{1, "READY"}};
    raw.payload = {0x40}; check(std::get<EnumValue>(decode().value).label == "READY", "enum label failed");
    raw.payload = {0xc0}; check(!decode().valid, "unmapped enum must be invalid");
    raw.payload.clear(); check(!decode().valid, "truncated payload must emit invalid sample, not disappear");
}
void testTime(const BackendConfiguration& cfg) {
    TimeQualityProcessor quality(cfg.clocks);
    auto s = sample("control", 10, 1, 10);
    check(quality.process(s).usable(), "mapped sample should be usable");
    const auto duplicate = quality.process(s); check(!duplicate.usable(), "duplicate sequence not rejected");
    s.sequence = 11; s.capture_time_ns -= 1;
    check(!quality.process(s).usable(), "out-of-order sample not rejected");
    s.generation = 2; check(quality.process(s).usable(), "new generation should reset sequence/time state");
    s.clock_domain = 999; check(!quality.process(s).time_ns, "unknown clock silently mapped");
    TimeQualityProcessor shifted({ClockDefinition{"control", "g", 1, 100, 3}});
    s.clock_domain = 1; s.capture_time_ns = 20;
    const auto aligned = shifted.process(s);
    check(aligned.time_ns == 120 && aligned.uncertainty_ns == 3, "explicit clock offset not applied");
    s.capture_time_ns = UINT64_MAX; check(!shifted.process(s).time_ns, "timestamp overflow accepted");
    TimeQualityProcessor negative({ClockDefinition{"control", "g", 1, INT64_MIN, 0}});
    s.capture_time_ns = 0; check(!negative.process(s).time_ns, "timestamp underflow accepted");
}
void testResponses(BackendConfiguration cfg) {
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1)); service.consume(sample("feedback", 40, 1, 1)); service.finish();
        check(count(service, "response_observed") == 1 && count(service, "response_timeout") == 0, "response at deadline mishandled");
        check(sink->results[0].metric == 30.0 && sink->results[0].evidence.size() == 2, "latency/evidence mismatch");
        throws([&] { service.consume(sample("control", 50, 0, 2)); }, "finished analysis accepted new data");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1));
        auto changed = sample("feedback", 12, 0, 1); changed.generation = 2; service.consume(changed);
        changed = sample("feedback", 22, 1, 2); changed.generation = 2; service.consume(changed); service.finish();
        check(count(service, "response_cancelled") == 1 && count(service, "response_observed") == 0, "cross-generation false match");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1));
        auto invalid = sample("feedback", 12, 0, 1); invalid.valid = false; service.consume(invalid); service.finish();
        check(count(service, "response_cancelled") == 1 && service.stats().rejected == 1, "invalid data did not cancel pending association");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1)); service.consume(sample("feedback", 9, 1, 1)); service.finish();
        check(service.stats().rejected == 1 && count(service, "response_observed") == 0, "late response created false match");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1));
        auto invalid = sample("sensor_a", 1000, 10, 0); invalid.parameter_id = "temperature_a"; invalid.valid = false;
        service.consume(invalid); service.consume(sample("feedback", 22, 1, 1)); service.finish();
        check(count(service, "response_observed") == 1 && count(service, "response_timeout") == 0,
            "invalid unrelated timestamp advanced the watermark and created a false timeout");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink);
        service.consume(sample("feedback", 0, 1, 0)); service.consume(sample("control", 0, 0, 0));
        service.consume(sample("control", 10, 1, 1)); service.finish();
        check(count(service, "response_already_high") == 1, "preexisting response incorrectly treated as reaction");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink);
        auto response = sample("feedback", 0, 0, 0); response.max_age_ns = 15000000;
        service.consume(response); service.consume(sample("control", 0, 0, 0)); service.consume(sample("control", 10, 1, 1));
        service.advance("host", 1050000000); service.finish();
        check(count(service, "response_observation_gap") == 1 && count(service, "response_timeout") == 0, "stale observation falsely diagnosed as timeout");
    }
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1)); service.finish();
        check(count(service, "response_unresolved") == 1 && count(service, "response_timeout") == 0, "EOF fabricated a timeout");
    }
    for (auto& clock : cfg.clocks) clock.uncertainty_ns = 1000000;
    {
        auto sink = std::make_shared<MemorySink>(); ProcessingService service(cfg, sink); baseline(service);
        service.consume(sample("control", 10, 1, 1)); service.consume(sample("feedback", 41, 1, 1)); service.finish();
        check(count(service, "response_time_ambiguous") == 1 && count(service, "response_timeout") == 0, "uncertainty ignored at deadline");
        check(sink->results[0].latency_low_ns == 29000000 && sink->results[0].latency_high_ns == 33000000, "latency bounds incorrect");
    }
}
void testConsistencyAndFixture(const BackendConfiguration& cfg, const std::filesystem::path& output) {
    auto sink = std::make_shared<JsonlAnalysisSink>(output / "fixture_analysis");
    ProcessingService service(cfg, sink); DictionaryDecoder decoder(cfg);
    for (const auto& frame : correlationFixture()) for (const auto& decoded : decoder.decode(frame)) service.consume(decoded);
    service.finish();
    check(service.stats().samples == 17 && service.stats().usable == 17, "fixture samples lost/rejected");
    check(count(service, "consistent") == 1 && count(service, "inconsistent") == 1 && count(service, "response_observed") == 1 &&
        count(service, "response_timeout") == 1 && count(service, "response_unresolved") == 1, "fixture outcomes changed");
    for (const auto& result : service.results()) if (result.outcome == "response_observed") check(result.metric == 12, "known 12ms delay not recovered");
    auto memory = std::make_shared<MemorySink>(); ProcessingService separate(cfg, memory);
    auto a = sample("sensor_a", 0, 10, 0); a.parameter_id = "temperature_a"; a.unit = "degC"; a.max_age_ns = 20000000;
    auto b = a; b.source = "sensor_b"; b.parameter_id = "temperature_b"; b.unit = "K";
    separate.consume(a); separate.consume(b);
    check(count(separate, "unit_mismatch") == 1 && count(separate, "inconsistent") == 0, "units silently mixed");
    b.sequence = 1; b.unit = "degC"; b.capture_time_ns += 30000000; separate.consume(b);
    check(count(separate, "consistent") == 0, "stale pair was compared");
    auto split_cfg = cfg;
    for (auto& clock : split_cfg.clocks) if (clock.source == "sensor_b") clock.group = "independent";
    ProcessingService split(split_cfg, memory); b.capture_time_ns = a.capture_time_ns; split.consume(a); split.consume(b);
    check(split.stats().results == 0, "independent clock groups were compared");
    auto json_sink = std::make_shared<JsonlAnalysisSink>(output / "typed_json");
    AlignedSample typed; typed.sample = a; typed.sample.value = UINT64_MAX; typed.sample.unit = "quote\"\nunit";
    typed.time_ns = a.capture_time_ns; json_sink->sample(typed);
    typed.sample.value = EnumValue{7, "READY\n状态"}; json_sink->sample(typed);
    typed.sample.value = std::numeric_limits<double>::quiet_NaN(); typed.sample.valid = false;
    typed.quality = {"invalid_payload"}; json_sink->sample(typed); json_sink->flush();
    throws([&] { JsonlAnalysisSink same(output / "typed_json"); }, "analysis output should not overwrite existing evidence");
}
}
int main(int argc, char** argv) {
    try {
        if (argc != 3) throw std::invalid_argument("expected config and output directory");
        const auto output = std::filesystem::path(argv[2]) / std::to_string(monotonicNowNs());
        std::filesystem::create_directories(output);
        const auto config = BackendConfiguration::load(argv[1]);
        testConfiguration(argv[1], output); testDictionary(); std::cout << "PASS strict dictionary, bit fields, types, precision and invalid data\n";
        testTime(config); testResponses(config); std::cout << "PASS clock mappings, sequence/time quality, response boundaries, restart, stale data, EOF and uncertainty\n";
        testConsistencyAndFixture(config, output); std::cout << "PASS consistency, unit/group boundaries, known 12ms fixture and JSONL output\n";
        std::cout << "ALL PROCESSING TESTS PASSED\nartifacts=" << output.string() << '\n'; return 0;
    } catch (const std::exception& e) { std::cerr << "PROCESSING TEST FAILURE: " << e.what() << '\n'; return 1; }
}
