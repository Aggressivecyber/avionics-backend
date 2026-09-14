#include "core/analysis.hpp"
#include "core/bus_manager.hpp"
#include "core/decoder_registry.hpp"
#include <bit>
#include <cmath>
#include <functional>
#include <iostream>
#include <stdexcept>

using namespace avionics;
using namespace std::chrono_literals;
namespace {
void check(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
template<class F> void throws(F&& function, const std::string& message) {
    bool caught = false;
    try { function(); } catch (const std::exception&) { caught = true; }
    check(caught, message);
}
template<class F> void eventually(F&& condition, const std::string& message) {
    const auto deadline = std::chrono::steady_clock::now() + 3s;
    while (!condition()) {
        if (std::chrono::steady_clock::now() >= deadline) throw std::runtime_error(message);
        std::this_thread::sleep_for(2ms);
    }
}
RawFrame frame(std::string source, std::int64_t milli, std::uint64_t sequence = 0) {
    RawFrame f;
    f.source = std::move(source); f.generation = 1; f.protocol = BUS_PROTOCOL_SIMULATED;
    f.clock_domain = BUS_CLOCK_HOST_MONOTONIC;
    f.capture_time_ns = 1000000000 + sequence * 1000000;
    f.ingest_time_ns = f.capture_time_ns + 123; f.sequence = sequence;
    const auto bits = std::bit_cast<std::uint64_t>(milli);
    for (std::size_t i = 0; i < 8; ++i) f.payload.push_back(static_cast<std::uint8_t>(bits >> (i * 8)));
    return f;
}
class MemoryRecorder final : public IFrameRecorder {
public:
    void write(const RawFrame& f) override { frames.push_back(f); }
    void flush() override {}
    std::vector<RawFrame> frames; // inspected only after shutdown.
};
class BrokenRecorder final : public IFrameRecorder {
public:
    void write(const RawFrame&) override { throw std::runtime_error("injected storage failure"); }
    void flush() override {}
};
class GateConsumer final : public IParameterConsumer {
public:
    void consume(const ParameterSample&) override {
        std::unique_lock lock(mutex);
        entered = true; changed.notify_all();
        changed.wait(lock, [&] { return released; });
    }
    bool awaitEntry() {
        std::unique_lock lock(mutex);
        return changed.wait_for(lock, 3s, [&] { return entered; });
    }
    void release() { std::lock_guard lock(mutex); released = true; changed.notify_all(); }
    std::mutex mutex;
    std::condition_variable changed;
    bool entered{}, released{};
};
void testDecodeAndRules() {
    DecoderRegistry registry;
    check(registry.decode(frame("a", 1)).empty(), "unregistered protocol must remain undecoded");
    registry.registerDecoder(BUS_PROTOCOL_SIMULATED, std::make_shared<SimulatedDecoder>());
    check(registry.decode(frame("a", 1)).size() == 1, "registered decoder route failed");
    SimulatedDecoder decoder;
    auto sample = decoder.decode(frame("a", -1234)).at(0);
    check(std::abs(std::get<double>(sample.value) + 1.234) < 1e-12, "signed LE payload must decode correctly");
    check(sample.unit == "degC" && sample.valid, "sample quality/unit lost");
    auto bad = frame("a", 0); bad.payload.resize(2);
    throws([&] { decoder.decode(bad); }, "short payload must be rejected");
    RuleAnalyzer analyzer(22, 10);
    sample.value = 21.0; analyzer.consume(sample);
    sample.value = 23.0; sample.raw_record_index = 7; analyzer.consume(sample);
    check(analyzer.events().size() == 1 && analyzer.events()[0].raw_record_index == 7, "threshold evidence missing");
    sample.value = 21.0; analyzer.consume(sample);
    sample.generation = 2; sample.value = 23.0; analyzer.consume(sample);
    check(analyzer.events().size() == 1, "replacement must not create cross-generation event");
    sample.value = 21.0; analyzer.consume(sample);
    sample.origin_generation = 99; sample.value = 23.0; analyzer.consume(sample);
    check(analyzer.events().size() == 1, "replay must preserve original generation boundaries");
    sample.value = 21.0; analyzer.consume(sample);
    sample.valid = false; analyzer.consume(sample);
    sample.valid = true; sample.value = 23.0; analyzer.consume(sample);
    check(analyzer.events().size() == 1, "invalid data must reset threshold history");
}
void testQueueAndFailures() {
    auto gate = std::make_shared<GateConsumer>();
    auto recorder = std::make_unique<MemoryRecorder>();
    auto* inspect = recorder.get();
    FramePipeline pipeline(2, std::move(recorder), std::make_unique<SimulatedDecoder>(), gate);
    // Always release the consumer before raising an assertion, avoiding teardown deadlock.
    const bool first = pipeline.submit(frame("a", 1));
    const bool entered = gate->awaitEntry();
    const bool second = pipeline.submit(frame("a", 2));
    const bool third = pipeline.submit(frame("a", 3));
    const bool fourth = pipeline.submit(frame("a", 4));
    gate->release(); pipeline.shutdown();
    check(first && entered && second && third && !fourth, "bounded queue must reject newest frame when full");
    check(pipeline.stats().dropped == 1 && inspect->frames.size() == 3, "overflow must be visible and accepted frames retained");
    check(!pipeline.submit(frame("a", 5)), "submission after shutdown must fail");
    auto analyzer = std::make_shared<RuleAnalyzer>(22);
    FramePipeline broken(4, std::make_unique<BrokenRecorder>(), std::make_unique<SimulatedDecoder>(), analyzer);
    broken.submit(frame("a", 1));
    eventually([&] { return !broken.stats().healthy; }, "storage failure not exposed");
    check(!broken.drainSource("a", 100ms) && !broken.submit(frame("a", 2)), "faulted pipeline must reject work and fail drain");
    broken.shutdown();
    FramePipeline malformed(4, std::make_unique<MemoryRecorder>(), std::make_unique<SimulatedDecoder>(), analyzer);
    auto bad = frame("a", 1); bad.payload.clear(); malformed.submit(bad);
    auto unknown = frame("a", 1); unknown.protocol = 42; malformed.submit(unknown);
    malformed.shutdown();
    check(malformed.stats().decode_errors == 1 && malformed.stats().unsupported == 1 && malformed.stats().healthy,
        "bad/unknown payloads must be counted separately without killing raw capture");
}
void testArchive(const std::filesystem::path& dir) {
    const auto path = dir / "roundtrip.avbus";
    auto original = frame("test", -12000, 3);
    original.record_index = 17; original.origin_source = "original";
    original.origin_generation = 7; original.origin_record_index = 9;
    { ArchiveWriter writer(path); writer.write(original); writer.flush(); }
    ArchiveReader reader(path);
    RawFrame restored;
    check(reader.next(restored) && !reader.next(restored), "archive record count mismatch");
    check(restored.payload == original.payload && restored.source == original.source &&
        restored.origin_source == original.origin_source && restored.origin_generation == 7 &&
        restored.origin_record_index == 9 && restored.record_index == 17 &&
        restored.capture_time_ns == original.capture_time_ns, "archive metadata roundtrip failed");
    throws([&] { ArchiveWriter duplicate(path); }, "archive must not overwrite existing evidence");
    auto corrupt = dir / "corrupt.avbus";
    std::filesystem::copy_file(path, corrupt);
    { std::fstream file(corrupt, std::ios::in | std::ios::out | std::ios::binary);
      file.seekg(-1, std::ios::end); char byte{}; file.read(&byte, 1); byte ^= 0x01;
      file.seekp(-1, std::ios::end); file.write(&byte, 1); }
    throws([&] { ArchiveReader bad(corrupt); bad.next(restored); }, "CRC corruption must be detected");
    auto truncated = dir / "truncated.avbus";
    std::filesystem::copy_file(path, truncated);
    std::filesystem::resize_file(truncated, std::filesystem::file_size(truncated) - 1);
    throws([&] { ArchiveReader bad(truncated); bad.next(restored); }, "truncated body must be detected");
    auto short_header = dir / "short_header.avbus";
    std::filesystem::copy_file(path, short_header);
    std::filesystem::resize_file(short_header, 10);
    throws([&] { ArchiveReader bad(short_header); bad.next(restored); }, "truncated record header must be detected");
}
void testLifecycle(const std::filesystem::path& simulated, const std::filesystem::path& replay,
    const std::filesystem::path& incompatible, const std::filesystem::path& dir) {
    const auto archive = dir / u8"capture_中文.avbus";
    auto analyzer = std::make_shared<RuleAnalyzer>(22, 10000);
    auto pipeline = std::make_shared<FramePipeline>(8192, std::make_unique<ArchiveWriter>(archive),
        std::make_unique<SimulatedDecoder>(), analyzer);
    {
        BusManager buses(pipeline);
        buses.add("a", simulated, "period_ms=1;step_milli=1000");
        buses.add("b", simulated, "period_ms=1;channel=1;step_milli=0");
        buses.start("a"); buses.start("b");
        eventually([&] { return buses.status("a").emitted >= 5 && buses.status("b").emitted >= 5; }, "simulated sources not running");
        check(buses.loadedPluginCount() == 1, "two instances must share module ownership");
        const auto a_before = buses.status("a");
        const auto b_before = buses.status("b");
        throws([&] { buses.add("a", simulated, ""); }, "duplicate source must be rejected");
        throws([&] { buses.replace("a", incompatible, ""); }, "incompatible ABI must be rejected");
        throws([&] { buses.replace("a", simulated, "period_ms=0"); }, "invalid replacement config must be rejected");
        throws([&] { buses.replace("a", simulated, "unknown=1"); }, "unknown config must be rejected");
        check(buses.status("a").generation == a_before.generation && buses.status("a").state == AdapterState::Running,
            "preflight failure must leave original source running");
        throws([&] { buses.replace("a", simulated, "fail_start=1"); }, "injected start failure must be reported");
        check(buses.status("a").state == AdapterState::Running && buses.status("a").generation > a_before.generation,
            "failed replacement must restart the previous adapter with a new generation");
        eventually([&] { return buses.status("a").emitted >= 2; }, "rollback source not producing frames");
        for (int i = 0; i < 20; ++i) {
            buses.replace("a", simulated, "period_ms=1;initial_milli=21000;step_milli=500");
            eventually([&] { return buses.status("a").emitted >= 2; }, "replacement stopped producing frames");
        }
        const auto b_after = buses.status("b");
        check(b_after.generation == b_before.generation && b_after.emitted > b_before.emitted, "replacement disrupted unrelated source");
        buses.stop("a");
        const auto stopped = pipeline->stats().sources.at("a").accepted;
        const auto b_count = buses.status("b").emitted;
        eventually([&] { return buses.status("b").emitted > b_count + 2; }, "second source stalled");
        check(pipeline->stats().sources.at("a").accepted == stopped, "callback arrived after stop returned");
        buses.start("a"); eventually([&] { return buses.status("a").emitted >= 2; }, "restart failed");
        buses.remove("a"); check(buses.loadedPluginCount() == 1, "module unloaded while second instance still owns it");
        buses.remove("b"); check(buses.loadedPluginCount() == 0, "module handle retained after last adapter removed");
    }
    pipeline->shutdown();
    auto stats = pipeline->stats();
    check(stats.healthy && stats.dropped == 0 && stats.accepted == stats.completed, "normal capture must drain without loss");
    check(!analyzer->events().empty(), "end-to-end threshold events missing");
    std::size_t records{};
    { ArchiveReader reader(archive); RawFrame f; while (reader.next(f)) {
        ++records; check(f.record_index == records && f.ingest_time_ns >= f.capture_time_ns, "capture identity/time lost");
      } }
    check(records == stats.accepted, "archive does not contain all accepted frames");

    // Switch a live simulated adapter to a DIFFERENT plugin library, then replay
    // captured bytes through the same decoder, maintaining source provenance.
    const auto replay_archive = dir / "replayed.avbus";
    auto replay_analyzer = std::make_shared<RuleAnalyzer>(22, 10000);
    auto replay_pipeline = std::make_shared<FramePipeline>(8192, std::make_unique<ArchiveWriter>(replay_archive),
        std::make_unique<SimulatedDecoder>(), replay_analyzer);
    {
        BusManager buses(replay_pipeline);
        buses.add("replay", simulated, "period_ms=1"); buses.start("replay");
        eventually([&] { return buses.status("replay").emitted >= 2; }, "source for cross-plugin swap not ready");
        const auto text = archive.u8string();
        const auto config = "path=" + std::string(text.begin(), text.end()) + ";speed=0";
        buses.replace("replay", replay, config);
        eventually([&] { return buses.status("replay").state != AdapterState::Running; }, "replay did not reach EOF");
        const auto status = buses.status("replay");
        check(status.errors == 0 && status.emitted == records && status.rejected == 0, "replay count/fault mismatch");
        check(buses.loadedPluginCount() == 1, "old library remained loaded after cross-plugin swap");
        buses.remove("replay"); check(buses.loadedPluginCount() == 0, "replay module did not release");
    }
    replay_pipeline->shutdown();
    std::size_t replay_records{};
    ArchiveReader reader(replay_archive); RawFrame f;
    ArchiveReader originals(archive); RawFrame original;
    while (reader.next(f)) if (!f.origin_source.empty()) {
        ++replay_records;
        check(originals.next(original), "replay produced more records than its input");
        check((f.origin_source == "a" || f.origin_source == "b") && f.origin_record_index > 0 &&
            f.origin_generation > 0 && f.clock_domain == BUS_CLOCK_RECORDED, "replay provenance lost");
        check(f.payload == original.payload && f.protocol == original.protocol && f.channel == original.channel &&
            f.flags == original.flags && f.sequence == original.sequence && f.capture_time_ns == original.capture_time_ns &&
            f.origin_source == original.source && f.origin_generation == original.generation &&
            f.origin_record_index == original.record_index, "replay changed original payload, timestamp or provenance");
    }
    check(replay_records == records && !originals.next(original), "replay archive missing original frames");
    check(replay_pipeline->stats().healthy, "replay pipeline failed");
}
}
int main(int argc, char** argv) {
    try {
        if (argc != 5) throw std::invalid_argument("expected simulated, replay, incompatible, output paths");
        const auto dir = std::filesystem::path(argv[4]) / std::to_string(monotonicNowNs());
        std::filesystem::create_directories(dir);
        testDecodeAndRules(); std::cout << "PASS signed decoding, rule quality and generation boundaries\n";
        testQueueAndFailures(); std::cout << "PASS bounded queue, storage failure, malformed/unsupported frames\n";
        testArchive(dir); std::cout << "PASS archive roundtrip, overwrite protection, CRC and truncation\n";
        testLifecycle(argv[1], argv[2], argv[3], dir);
        std::cout << "PASS lifecycle, 20 replacements, ABI/config rejection, rollback, isolation, cross-plugin swap and replay\n";
        std::cout << "ALL TESTS PASSED\nartifacts=" << dir.string() << '\n';
        return 0;
    } catch (const std::exception& e) { std::cerr << "TEST FAILURE: " << e.what() << '\n'; return 1; }
}
