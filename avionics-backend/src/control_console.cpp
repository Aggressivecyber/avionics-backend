#include "core/control_console.hpp"
#include "core/analysis_json.hpp"
#include "core/build_info.hpp"
#include "core/json.hpp"
#include "core/path.hpp"
#include <iomanip>
#include <iostream>
#include <locale>
#include <set>
#include <sstream>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace avionics {
void MonitorSink::sample(const AlignedSample& sample) {
    file_->sample(sample);
    std::lock_guard lock(mutex_);
    samples_.emplace_back(++sample_index_, sample);
    if (samples_.size() > 512) samples_.pop_front();
}
void MonitorSink::result(const CorrelationResult& event) {
    file_->result(event);
    std::lock_guard lock(mutex_);
    events_.emplace_back(++event_index_, event);
    if (events_.size() > 128) events_.pop_front();
}
void MonitorSink::writeSnapshot(std::ostream& out, std::uint64_t after_sample, std::uint64_t after_event) const {
    std::deque<std::pair<std::uint64_t, AlignedSample>> samples;
    std::deque<std::pair<std::uint64_t, CorrelationResult>> events;
    std::uint64_t sample_cursor{}, event_cursor{};
    {
        std::lock_guard lock(mutex_);
        samples = samples_; events = events_;
        sample_cursor = sample_index_; event_cursor = event_index_;
    }
    if (after_sample > sample_cursor || after_event > event_cursor) throw std::invalid_argument("display cursor is ahead of session");
    out << ",\"sample_cursor\":" << sample_cursor << ",\"event_cursor\":" << event_cursor
        << ",\"display_samples_omitted\":" << (sample_cursor - after_sample > samples.size() ? sample_cursor - after_sample - samples.size() : 0)
        << ",\"display_events_omitted\":" << (event_cursor - after_event > events.size() ? event_cursor - after_event - events.size() : 0)
        << ",\"parameters\":[";
    bool first = true;
    for (const auto& [index, sample] : samples) {
        if (index <= after_sample) continue;
        if (!first) out << ',';
        first = false; writeAlignedSample(out, sample);
    }
    out << "],\"events\":["; first = true;
    for (const auto& [index, event] : events) {
        if (index <= after_event) continue;
        if (!first) out << ',';
        first = false; writeCorrelationResult(out, event);
    }
    out << ']';
}
namespace {
const char* stateName(AdapterState state) {
    switch (state) {
    case AdapterState::Loaded: return "loaded";
    case AdapterState::Configured: return "configured";
    case AdapterState::Running: return "running";
    case AdapterState::Stopped: return "stopped";
    case AdapterState::Faulted: return "faulted";
    }
    throw std::logic_error("unknown adapter state");
}
void writeStatus(std::ostream& out, const AdapterStatus& s) {
    out << "{\"state\":" << jsonString(stateName(s.state)) << ",\"generation\":" << s.generation
        << ",\"emitted\":" << s.emitted << ",\"rejected\":" << s.rejected << ",\"errors\":" << s.errors
        << ",\"malformed\":" << s.malformed << ",\"failure\":" << jsonString(s.failure) << ",\"plugin_abi\":" << s.plugin_abi << '}';
}
}
void runControlConsole(BusManager& buses, const FramePipeline& pipeline, const ProcessingService& service,
    const MonitorSink& monitor, const SessionJournal& journal, const std::filesystem::path& archive,
    const BackendConfiguration& configuration, const ArchiveWriter& recording) {
    const auto utf8 = absolutePath(archive).u8string();
    std::cout << "{\"schema\":1,\"type\":\"ready\",\"archive\":" << jsonString(std::string(utf8.begin(), utf8.end()))
        << ",\"configuration_sha256\":" << jsonString(hexIdentity(configuration.document->sha256)) << ",\"build\":";
    writeBuildInfo(std::cout); std::cout << "}\n" << std::flush;
    std::set<std::string> sources;
    auto space_checked = std::chrono::steady_clock::time_point::min();
    std::uintmax_t available_bytes{};
    std::string line;
    while (std::getline(std::cin, line)) {
        std::uint64_t request{};
        try {
            if (line.size() > 65536) throw std::invalid_argument("control command exceeds 64 KiB");
            std::istringstream input(line);
            std::string command, id, path, config;
            if (!(input >> request >> command) || request == 0) throw std::invalid_argument("expected positive request id and command");
            auto end = [&] { input >> std::ws; if (!input.eof()) throw std::invalid_argument("unexpected command arguments"); };
            std::ostringstream data; data.imbue(std::locale::classic()); data << std::setprecision(17);
            bool quit = false;
            if (command == "snapshot") {
                std::uint64_t after_sample{}, after_event{};
                if (!(input >> after_sample >> after_event)) throw std::invalid_argument("snapshot requires two cursors");
                end();
                const auto s = pipeline.stats(); const auto p = service.stats();
                const auto now = std::chrono::steady_clock::now();
                if (space_checked == std::chrono::steady_clock::time_point::min() || now - space_checked >= std::chrono::seconds(1)) {
                    const auto directory = absolutePath(archive).parent_path();
#if defined(_WIN32)
                    ULARGE_INTEGER available{};
                    if (!GetDiskFreeSpaceExW(directory.c_str(), &available, nullptr, nullptr))
                        throw std::runtime_error("disk capacity query failed; Windows error " + std::to_string(GetLastError()));
                    available_bytes = available.QuadPart;
#else
                    available_bytes = std::filesystem::space(directory).available;
#endif
                    space_checked = now;
                }
                data << "{\"accepted\":" << s.accepted << ",\"recorded\":" << s.recorded << ",\"dropped\":" << s.dropped
                    << ",\"storage\":{\"volumes\":" << recording.currentVolume() << ",\"volume_bytes\":" << recording.volumeBytes()
                    << ",\"available_bytes\":" << available_bytes << ",\"warning_free_bytes\":" << configuration.warning_free_bytes
                    << ",\"low_space\":" << (configuration.warning_free_bytes && available_bytes < configuration.warning_free_bytes ? "true" : "false") << '}'
                    << ",\"healthy\":" << (s.healthy ? "true" : "false") << ",\"failure\":" << jsonString(s.failure)
                    << ",\"analysis_healthy\":" << (s.analysis_healthy ? "true" : "false")
                    << ",\"analysis_failure\":" << jsonString(s.analysis_failure)
                    << ",\"capture_degraded\":" << (journal.hadFailures() || s.dropped ? "true" : "false")
                    << ",\"analysis_cursor\":" << s.analysis_cursor << ",\"analysis_skipped\":" << s.analysis_skipped
                    << ",\"decode_errors\":" << s.decode_errors << ",\"unsupported\":" << s.unsupported
                    << ",\"queued_bytes\":" << s.queued_bytes << ",\"analysis_queued_bytes\":" << s.analysis_queued_bytes
                    << ",\"samples\":" << p.samples << ",\"usable\":" << p.usable << ",\"quality_rejected\":" << p.rejected
                    << ",\"results\":" << p.results << ",\"sources\":[";
                bool first = true;
                for (const auto& source : sources) {
                    if (!first) data << ',';
                    first = false;
                    data << "{\"id\":" << jsonString(source) << ",\"status\":";
                    writeStatus(data, buses.status(source));
                    const auto& counters = s.sources.at(source); // add() reserves pipeline counters before publishing the source.
                    data << ",\"recorded\":" << counters.recorded << ",\"dropped\":" << counters.dropped;
                    data << '}';
                }
                data << ']'; monitor.writeSnapshot(data, after_sample, after_event); data << '}';
            } else if (command == "quit") { end(); quit = true; data << "{}"; }
            else {
                if (!(input >> std::quoted(id))) throw std::invalid_argument("missing source id");
                if (command == "add" || command == "replace") {
                    if (!(input >> std::quoted(path) >> std::quoted(config))) throw std::invalid_argument("missing plugin path or configuration");
                    end();
                    if (command == "add") { buses.add(id, pathFromUtf8(path), config); sources.insert(id); }
                    else buses.replace(id, pathFromUtf8(path), config);
                } else {
                    end();
                    if (command == "start") buses.start(id);
                    else if (command == "stop") buses.stop(id);
                    else if (command == "remove") { buses.remove(id); sources.erase(id); }
                    else throw std::invalid_argument("unknown control command");
                }
                data << "{}";
            }
            std::cout << "{\"schema\":1,\"request\":" << request << ",\"ok\":true,\"data\":" << data.str() << "}\n" << std::flush;
            if (quit) break;
        } catch (const std::exception& error) {
            std::cout << "{\"schema\":1,\"request\":" << request << ",\"ok\":false,\"error\":" << jsonString(error.what()) << "}\n" << std::flush;
        }
    }
}
}
