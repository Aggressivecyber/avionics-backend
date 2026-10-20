#include "core/analysis.hpp"
#include "core/bus_manager.hpp"
#include "core/path.hpp"
#include "core/decoder_registry.hpp"
#include "core/processing.hpp"
#include "core/mil1553b.hpp"
#include "core/json.hpp"
#include "core/build_info.hpp"
#include "core/arguments.hpp"
#include "core/control_console.hpp"
#include <iostream>
#include <sstream>
#include <thread>

namespace {
using namespace avionics;
const char* stateName(AdapterState state) {
    switch (state) {
    case AdapterState::Loaded: return "loaded";
    case AdapterState::Configured: return "configured";
    case AdapterState::Running: return "running";
    case AdapterState::Stopped: return "stopped";
    case AdapterState::Faulted: return "faulted";
    }
    return "unknown";
}
void printStats(const FramePipeline& pipeline, const RuleAnalyzer& analyzer, const ProcessingService* service = nullptr) {
    const auto s = pipeline.stats();
    std::cout << "accepted=" << s.accepted << " completed=" << s.completed << " dropped=" << s.dropped
        << " samples=" << s.samples << " decode_errors=" << s.decode_errors << " unsupported=" << s.unsupported
        << " healthy=" << s.healthy << " retained_events=" << analyzer.events().size() << '\n';
    std::cout << "recorded=" << s.recorded << " analysis_healthy=" << s.analysis_healthy
        << " analysis_cursor=" << s.analysis_cursor << " analysis_skipped=" << s.analysis_skipped << '\n';
    if (!s.analysis_failure.empty()) std::cout << "analysis_failure=" << s.analysis_failure << '\n';
    if (s.analysis_first_missing) std::cout << "analysis_first_missing=" << *s.analysis_first_missing << '\n';
    std::cout << "reject_queue_frames=" << s.rejections.queue_frames << " reject_queue_bytes=" << s.rejections.queue_bytes
        << " reject_source_frames=" << s.rejections.source_frames << " reject_source_bytes=" << s.rejections.source_bytes
        << " reject_source_capacity=" << s.rejections.source_capacity << '\n';
    if (!s.failure.empty()) std::cout << "failure=" << s.failure << '\n';
    for (const auto& [id, counters] : s.sources)
        std::cout << "  " << id << ": accepted=" << counters.accepted << " completed=" << counters.completed
            << " dropped=" << counters.dropped << '\n';
    if (service) {
        const auto processed = service->stats();
        std::cout << "analysis_samples=" << processed.samples << " usable=" << processed.usable
            << " rejected=" << processed.rejected << " results=" << processed.results << '\n';
    }
}
void help() {
    std::cout << "Commands (plugin paths cannot contain spaces in this initial CLI):\n"
        << "  add ID PLUGIN [key=value;key=value]\n  start ID\n  stop ID\n  remove ID\n"
        << "  replace ID PLUGIN [key=value;key=value]\n  status ID\n  stats\n  events\n  help\n  quit\n";
}
}
int main(int argc, char** argv) {
    using namespace avionics;
    try {
        auto arguments = utf8Arguments(argc, argv);
        std::vector<char*> argument_pointers;
        for (auto& argument : arguments) argument_pointers.push_back(argument.data());
        argc = static_cast<int>(argument_pointers.size()); argv = argument_pointers.data();
        if (argc == 2 && std::string(argv[1]) == "--version") { writeBuildInfo(std::cout); std::cout << '\n'; return 0; }
        const bool control = argc > 1 && std::string(argv[1]) == "--control";
        if ((argc != 3 && argc != 4) || (std::string(argv[1]) != "--demo" && std::string(argv[1]) != "--interactive" && !control) ||
            (argc == 4 && std::string(argv[1]) == "--demo") || (control && argc != 4)) {
            std::cout << "Usage: bus_backend --demo SIMULATED_PLUGIN\n"
                << "       bus_backend --interactive NEW_ARCHIVE_PATH [CONFIG_INI]\n"
                << "       bus_backend --control NEW_ARCHIVE_PATH CONFIG_INI\n";
            return argc == 1 ? 0 : 2;
        }
        const bool demo = std::string(argv[1]) == "--demo";
        std::filesystem::path archive;
        if (demo) {
            std::filesystem::create_directories("runs");
            archive = std::filesystem::path("runs") / ("demo_" + std::to_string(monotonicNowNs()) + ".avbus");
        } else archive = pathFromUtf8(argv[2]);
        auto session_path = archive; session_path += ".session.json";
        if (std::filesystem::exists(session_path)) throw std::runtime_error("session metadata already exists");
        std::optional<BackendConfiguration> configuration;
        if (argc == 4) configuration = BackendConfiguration::load(pathFromUtf8(argv[3]));
        auto recorder = std::make_unique<ArchiveWriter>(archive);
        const auto* recording = recorder.get();
        auto analyzer = std::make_shared<RuleAnalyzer>(22.0);
        std::shared_ptr<ProcessingService> service;
        std::shared_ptr<MonitorSink> monitor;
        PipelineLimits pipeline_limits;
        std::shared_ptr<IParameterConsumer> consumer = analyzer;
        std::unique_ptr<IParameterDecoder> decoder;
        if (argc == 4) {
            auto& config = *configuration;
            config.recording_session_id = recording->summary().session_id;
            pipeline_limits = config.pipeline;
            auto output = archive; output += ".analysis";
            auto sink = std::make_shared<JsonlAnalysisSink>(output);
            config.document->save(output / "configuration.ini");
            std::shared_ptr<IAnalysisSink> analysis_sink = sink;
            if (control) { monitor = std::make_shared<MonitorSink>(sink); analysis_sink = monitor; }
            service = std::make_shared<ProcessingService>(config, analysis_sink);
            consumer = service; decoder = std::make_unique<ProtocolPipelineDecoder>(config);
            if (!control) std::cout << "analysis_output=" << output.string() << '\n';
        } else {
            auto decoders = std::make_unique<DecoderRegistry>();
            decoders->registerDecoder(BUS_PROTOCOL_SIMULATED, std::make_shared<SimulatedDecoder>());
            decoder = std::move(decoders);
        }
        auto pipeline = std::make_shared<FramePipeline>(pipeline_limits, std::move(recorder),
            std::move(decoder), consumer);
        auto journal_path = archive; journal_path += ".lifecycle.jsonl";
        auto journal = std::make_shared<SessionJournal>(journal_path);
        CaptureOptions capture;
        if (configuration) {
            capture.command_timeout = std::chrono::milliseconds(configuration->capture_command_timeout_ms);
            capture.stop_timeout = std::chrono::milliseconds(configuration->capture_stop_timeout_ms);
        }
        if (!control) std::cout << "archive=" << absolutePath(archive).string() << '\n';
        {
            BusManager buses(pipeline, journal, capture);
            if (control) runControlConsole(buses, *pipeline, *service, *monitor, *journal, archive, *configuration);
            else if (demo) {
                const auto plugin = pathFromUtf8(argv[2]);
                buses.add("primary", plugin, "period_ms=5;initial_milli=20000;step_milli=500");
                buses.add("secondary", plugin, "period_ms=5;channel=1");
                buses.start("primary"); buses.start("secondary");
                std::this_thread::sleep_for(std::chrono::milliseconds(150));
                const auto before = buses.status("secondary");
                buses.replace("primary", plugin, "period_ms=5;initial_milli=21000;step_milli=250");
                std::this_thread::sleep_for(std::chrono::milliseconds(150));
                const auto after = buses.status("secondary");
                std::cout << "primary replaced; secondary generation=" << before.generation << " -> " << after.generation
                    << ", emitted=" << before.emitted << " -> " << after.emitted << '\n';
                buses.remove("primary"); buses.remove("secondary");
                std::cout << "sources_after_remove=" << buses.sourceCount() << '\n';
            } else {
                help();
                std::string line;
                while (std::cout << "> " && std::getline(std::cin, line)) {
                    try {
                        std::istringstream input(line);
                        std::string command, id, path, config;
                        input >> command;
                        if (command.empty()) continue;
                        if (command == "quit") break;
                        if (command == "help") { help(); continue; }
                        if (command == "stats") { printStats(*pipeline, *analyzer, service.get()); continue; }
                        if (command == "events") {
                            if (service) {
                                for (const auto& result : service->results()) {
                                    std::cout << result.rule << ' ' << result.outcome << " time_ns=" << result.time_ns;
                                    if (result.metric) std::cout << " metric=" << *result.metric;
                                    std::cout << '\n';
                                }
                                continue;
                            }
                            for (const auto& e : analyzer->events()) std::cout << e.rule_id << " source=" << e.source
                                << " generation=" << e.generation << " record=" << e.raw_record_index << " value=" << e.value << '\n';
                            continue;
                        }
                        if (!(input >> id)) throw std::invalid_argument("missing instance id");
                        if (command == "add" || command == "replace") {
                            if (!(input >> path)) throw std::invalid_argument("missing plugin path");
                            std::getline(input >> std::ws, config);
                            if (command == "add") buses.add(id, pathFromUtf8(path), config);
                            else buses.replace(id, pathFromUtf8(path), config);
                        } else if (command == "start") buses.start(id);
                        else if (command == "stop") buses.stop(id);
                        else if (command == "remove") buses.remove(id);
                        else if (command == "status") {
                            const auto s = buses.status(id);
                            std::cout << stateName(s.state) << " generation=" << s.generation << " emitted=" << s.emitted
                                << " rejected=" << s.rejected << " errors=" << s.errors << " malformed=" << s.malformed
                                << " plugin_abi=" << s.plugin_abi << '\n';
                            if (!s.failure.empty()) std::cout << "source_failure=" << s.failure << '\n';
                        } else throw std::invalid_argument("unknown command");
                    } catch (const std::exception& e) { std::cerr << "command failed: " << e.what() << '\n'; }
                }
            }
            buses.shutdown();
        } // all plugin workers joined before draining/closing the pipeline.
        pipeline->shutdown();
        if (!control) printStats(*pipeline, *analyzer, service.get());
        const auto final = pipeline->stats();
        std::ofstream metadata(session_path, std::ios::binary);
        metadata << "{\"schema\":2,\"build\":"; writeBuildInfo(metadata);
        metadata << ",\"archive\":"; writeArchiveSummary(metadata, recording->summary());
        metadata << ",\"recording_healthy\":" << (final.healthy ? "true" : "false")
            << ",\"configuration_sha256\":" << (configuration ? jsonString(hexIdentity(configuration->document->sha256)) : "null")
            << ",\"capture_status\":" << jsonString(journal->hadFailures() || final.dropped ? "degraded" : "complete")
            << ",\"analysis_status\":" << jsonString(final.analysis_healthy ? "complete" : "incomplete")
            << ",\"analysis_failure\":" << jsonString(final.analysis_failure)
            << ",\"analysis_cursor\":" << final.analysis_cursor << ",\"analysis_skipped\":" << final.analysis_skipped;
        if (service) {
            const auto processed = service->stats();
            metadata << ",\"samples\":" << processed.samples << ",\"usable\":" << processed.usable
                << ",\"quality_rejected\":" << processed.rejected << ",\"results\":" << processed.results;
        }
        metadata << ",\"analysis_first_missing\":";
        if (final.analysis_first_missing) metadata << *final.analysis_first_missing; else metadata << "null";
        metadata
            << ",\"recording_failure\":" << jsonString(final.failure)
            << ",\"accepted\":" << final.accepted << ",\"recorded\":" << final.recorded << ",\"dropped\":" << final.dropped
            << ",\"rejections\":{\"closed\":" << final.rejections.closed << ",\"storage_fault\":" << final.rejections.storage_fault
            << ",\"queue_frames\":" << final.rejections.queue_frames << ",\"queue_bytes\":" << final.rejections.queue_bytes
            << ",\"source_frames\":" << final.rejections.source_frames << ",\"source_bytes\":" << final.rejections.source_bytes
            << ",\"source_capacity\":" << final.rejections.source_capacity << "},\"sources\":{";
        bool first = true;
        for (const auto& [id, counters] : final.sources) {
            if (!first) metadata << ',';
            first = false;
            metadata << jsonString(id) << ":{\"accepted\":" << counters.accepted << ",\"recorded\":" << counters.recorded
                << ",\"analysis_consumed\":" << counters.completed << ",\"dropped\":" << counters.dropped << '}';
        }
        metadata << "}}\n";
        metadata.close(); if (!metadata) throw std::runtime_error("cannot write session metadata");
        if (control) {
            std::ifstream completed(session_path, std::ios::binary);
            std::string finalized; std::getline(completed, finalized);
            std::cout << "{\"schema\":1,\"type\":\"closed\",\"data\":" << finalized << "}\n" << std::flush;
        }
        return final.healthy ? (final.analysis_healthy ? (journal->hadFailures() || final.dropped ? 4 : 0) : 3) : 1;
    } catch (const std::exception& e) { std::cerr << "fatal: " << e.what() << '\n'; return 1; }
}
