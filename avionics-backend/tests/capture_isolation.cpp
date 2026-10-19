#include "core/bus_manager.hpp"
#include "core/analysis.hpp"
#include "core/decoder_registry.hpp"
#include <iostream>

using namespace avionics;
int main(int argc, char** argv) {
    try {
        if (argc != 5) throw std::runtime_error("expected simulated, fault, load-hang and output paths");
        auto check = [](bool condition, const std::string& reason) { if (!condition) throw std::runtime_error(reason); };
        auto eventually = [&](auto&& predicate, const std::string& reason) {
            const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
            while (!predicate()) { check(std::chrono::steady_clock::now() < deadline, reason); std::this_thread::sleep_for(std::chrono::milliseconds(5)); }
        };
        const auto output = std::filesystem::path(argv[4]) / ("capture_faults_" + std::to_string(monotonicNowNs()));
        std::filesystem::create_directories(output);
        auto recorder = std::make_unique<ArchiveWriter>(output / "capture.avbus");
        auto pipeline = std::make_shared<FramePipeline>(4096, std::move(recorder), std::make_unique<SimulatedDecoder>(), std::make_shared<RuleAnalyzer>(22));
        auto journal = std::make_shared<SessionJournal>(output / "lifecycle.jsonl");
        CaptureOptions options; options.command_timeout = std::chrono::milliseconds(500); options.stop_timeout = std::chrono::milliseconds(100);
        BusManager buses(pipeline, journal, options); buses.add("good", argv[1], "period_ms=1"); buses.start("good");
        eventually([&] { return buses.status("good").emitted > 5; }, "healthy source did not start");
        const auto good_generation = buses.status("good").generation;
        for (const std::string mode : {"exception", "crash", "malformed"}) {
            const auto before = buses.status("good").emitted;
            buses.add("bad", argv[2], "mode=" + mode);
            try { buses.start("bad"); }
            catch (const std::exception& error) { check(mode == "crash" && std::string(error.what()).find("77") != std::string::npos, "unexpected start failure"); }
            eventually([&] { return buses.status("bad").state == AdapterState::Faulted; }, "fault was not detected: " + mode);
            const auto failure = buses.status("bad"); check(!failure.failure.empty(), "asynchronous reason was lost");
            if (mode == "exception") check(failure.failure.find("injected asynchronous SDK failure") != std::string::npos, "SDK error text changed");
            if (mode == "crash") check(failure.failure.find("77") != std::string::npos, "process exit code was lost");
            eventually([&] { return buses.status("good").emitted > before + 5; }, "fault disrupted healthy source");
            buses.replace("bad", argv[1], "period_ms=1"); buses.start("bad");
            eventually([&] { return buses.status("bad").emitted > 1; }, "manual recovery failed"); buses.remove("bad");
            std::cout << "PASS " << mode << " isolation and manual recovery\n";
        }
        for (const std::string mode : {"hang_configure", "hang_start", "hang_stop", "hang_load"}) {
            const auto begin = std::chrono::steady_clock::now(); bool failed{};
            try {
                buses.add("hanging", mode == "hang_load" ? argv[3] : argv[2], "mode=" + mode);
                buses.start("hanging");
                if (mode == "hang_stop") {
                    eventually([&] { return buses.status("hanging").emitted == 1; }, "hanging source emitted nothing");
                    buses.stop("hanging"); failed = buses.status("hanging").state == AdapterState::Faulted;
                }
            } catch (const std::exception& error) { failed = std::string(error.what()).find("deadline") != std::string::npos; }
            check(failed, "hanging SDK call did not produce a deadline failure: " + mode);
            check(std::chrono::steady_clock::now() - begin < std::chrono::seconds(2), "worker teardown was unbounded");
            if (mode == "hang_start" || mode == "hang_stop") buses.remove("hanging");
            const auto before = buses.status("good").emitted;
            eventually([&] { return buses.status("good").emitted > before + 5; }, "deadline handling stalled another source");
            std::cout << "PASS " << mode << " deadline\n";
        }
        buses.add("large", argv[2], "mode=large_frame"); buses.start("large");
        eventually([&] { return buses.status("large").state == AdapterState::Stopped; }, "maximum frame did not pass IPC");
        check(buses.status("large").emitted == 1 && buses.status("large").malformed == 0, "maximum payload was corrupted");
        buses.remove("large");
        check(buses.status("good").generation == good_generation, "unrelated source generation changed");
        buses.shutdown(); pipeline->shutdown(); const auto stats = pipeline->stats();
        check(stats.healthy && stats.recorded == stats.accepted && !stats.dropped, "faults damaged raw recording");
        check(journal->hadFailures(), "removed source faults vanished from session status");
        ArchiveReader reader(output / "capture.avbus"); RawFrame frame; bool maximum{};
        while (reader.next(frame)) if (frame.source == "large") maximum = frame.payload.size() == BUS_MAX_PAYLOAD;
        check(maximum && reader.summary().integrity == ArchiveIntegrity::Sealed, "maximum frame or archive seal missing");
        std::cout << "PASS persistent fault journal, maximum payload and sealed raw archive\nartifacts=" << output.string() << '\n'; return 0;
    } catch (const std::exception& error) { std::cerr << "isolation test failed: " << error.what() << '\n'; return 1; }
}
