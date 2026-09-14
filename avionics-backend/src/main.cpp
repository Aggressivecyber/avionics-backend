#include "core/analysis.hpp"
#include "core/bus_manager.hpp"
#include "core/path.hpp"
#include "core/decoder_registry.hpp"
#include "core/processing.hpp"
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
        if ((argc != 3 && argc != 4) || (std::string(argv[1]) != "--demo" && std::string(argv[1]) != "--interactive") ||
            (argc == 4 && std::string(argv[1]) != "--interactive")) {
            std::cout << "Usage: bus_backend --demo SIMULATED_PLUGIN\n"
                << "       bus_backend --interactive NEW_ARCHIVE_PATH [CONFIG_INI]\n";
            return argc == 1 ? 0 : 2;
        }
        const bool demo = std::string(argv[1]) == "--demo";
        std::filesystem::path archive;
        if (demo) {
            std::filesystem::create_directories("runs");
            archive = std::filesystem::path("runs") / ("demo_" + std::to_string(monotonicNowNs()) + ".avbus");
        } else archive = pathFromUtf8(argv[2]);
        auto analyzer = std::make_shared<RuleAnalyzer>(22.0);
        std::shared_ptr<ProcessingService> service;
        std::shared_ptr<IParameterConsumer> consumer = analyzer;
        std::unique_ptr<IParameterDecoder> decoder;
        if (argc == 4) {
            const auto config_path = pathFromUtf8(argv[3]);
            const auto config = BackendConfiguration::load(config_path);
            if (std::filesystem::exists(archive)) throw std::runtime_error("raw archive already exists");
            auto output = archive; output += ".analysis";
            auto sink = std::make_shared<JsonlAnalysisSink>(output);
            std::filesystem::copy_file(config_path, output / "configuration.ini");
            service = std::make_shared<ProcessingService>(config, sink);
            consumer = service; decoder = std::make_unique<DictionaryDecoder>(config);
            std::cout << "analysis_output=" << output.string() << '\n';
        } else {
            auto decoders = std::make_unique<DecoderRegistry>();
            decoders->registerDecoder(BUS_PROTOCOL_SIMULATED, std::make_shared<SimulatedDecoder>());
            decoder = std::move(decoders);
        }
        auto pipeline = std::make_shared<FramePipeline>(4096, std::make_unique<ArchiveWriter>(archive),
            std::move(decoder), consumer);
        std::cout << "archive=" << std::filesystem::absolute(archive).string() << '\n';
        {
            BusManager buses(pipeline);
            if (demo) {
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
                std::cout << "loaded_plugins_after_remove=" << buses.loadedPluginCount() << '\n';
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
                                << " rejected=" << s.rejected << " errors=" << s.errors << " malformed=" << s.malformed << '\n';
                        } else throw std::invalid_argument("unknown command");
                    } catch (const std::exception& e) { std::cerr << "command failed: " << e.what() << '\n'; }
                }
            }
        } // all plugin workers joined before draining/closing the pipeline.
        pipeline->shutdown();
        if (service && pipeline->stats().healthy) service->finish();
        printStats(*pipeline, *analyzer, service.get());
        return pipeline->stats().healthy ? 0 : 1;
    } catch (const std::exception& e) { std::cerr << "fatal: " << e.what() << '\n'; return 1; }
}
