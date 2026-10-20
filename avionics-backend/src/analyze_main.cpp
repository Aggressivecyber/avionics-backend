#include "core/processing.hpp"
#include "core/mil1553b.hpp"
#include "core/archive.hpp"
#include "core/demo_fixture.hpp"
#include "core/path.hpp"
#include "core/json.hpp"
#include "core/build_info.hpp"
#include "core/arguments.hpp"
#include <iostream>

int main(int argc, char** argv) {
    using namespace avionics;
    try {
        auto arguments = utf8Arguments(argc, argv);
        std::vector<char*> argument_pointers;
        for (auto& argument : arguments) argument_pointers.push_back(argument.data());
        argc = static_cast<int>(argument_pointers.size()); argv = argument_pointers.data();
        if (argc == 2 && std::string(argv[1]) == "--version") { writeBuildInfo(std::cout); std::cout << '\n'; return 0; }
        if (argc == 3 && std::string(argv[1]) == "--validate-config") {
            const auto config = BackendConfiguration::load(pathFromUtf8(argv[2]));
            std::cout << "{\"status\":\"valid\",\"configuration_sha256\":"
                << jsonString(hexIdentity(config.document->sha256)) << "}\n";
            return 0;
        }
        if (argc == 3 && std::string(argv[1]) == "--fixture") {
            const SessionId fixture_id{0x46,0x49,0x58,0x54,0x55,0x52,0x45,0x01,0x80,0,0,0,0,0,0,1};
            ArchiveWriter writer(pathFromUtf8(argv[2]), fixture_id);
            const auto frames = correlationFixture();
            for (const auto& frame : frames) writer.write(frame);
            writer.finish(); std::cout << "synthetic_records=" << frames.size() << '\n'; return 0;
        }
        if (argc == 3 && std::string(argv[1]) == "--inspect") {
            ArchiveReader reader(pathFromUtf8(argv[2])); RawFrame frame;
            while (reader.next(frame)) {}
            writeArchiveSummary(std::cout, reader.summary()); std::cout << '\n'; return 0;
        }
        if (argc == 4 && std::string(argv[1]) == "--record") {
            std::size_t consumed{};
            const auto index = std::stoull(argv[3], &consumed);
            if (!index || consumed != std::string(argv[3]).size()) throw std::invalid_argument("record index must be a positive integer");
            ArchiveReader reader(pathFromUtf8(argv[2])); RawFrame frame;
            std::optional<RawFrame> selected;
            while (reader.next(frame)) if (frame.record_index == index) selected = frame;
            if (!selected) throw std::invalid_argument("record index not present in archive");
            const auto& f = *selected;
            std::string payload; payload.reserve(f.payload.size() * 2);
            constexpr char digits[] = "0123456789abcdef";
            for (const auto captured_byte : f.payload) { payload += digits[captured_byte >> 4]; payload += digits[captured_byte & 15]; }
            std::cout << "{\"schema\":1,\"record_index\":" << f.record_index << ",\"source\":" << jsonString(f.source)
                << ",\"protocol\":" << f.protocol << ",\"channel\":" << f.channel << ",\"generation\":" << f.generation
                << ",\"capture_time_ns\":" << f.capture_time_ns << ",\"ingest_time_ns\":" << f.ingest_time_ns
                << ",\"sequence\":" << f.sequence << ",\"flags\":" << f.flags << ",\"clock_domain\":" << f.clock_domain
                << ",\"origin_source\":" << jsonString(f.origin_source) << ",\"origin_generation\":" << f.origin_generation
                << ",\"origin_record_index\":" << f.origin_record_index << ",\"payload_hex\":" << jsonString(payload)
                << ",\"recording_session_id\":" << (f.recording_session_id ? jsonString(hexIdentity(*f.recording_session_id)) : "null")
                << ",\"origin_session_id\":" << (f.origin_session_id ? jsonString(hexIdentity(*f.origin_session_id)) : "null");
            std::cout << ",\"protocol_messages\":[";
            ProtocolParseResult parsing;
            if (f.protocol == BUS_PROTOCOL_MIL1553B) {
                Mil1553BHandler handler(protocolStreamOf(f)); parsing = handler.parse(f);
            }
            bool first_message = true;
            for (const auto& message : parsing.messages) {
                if (!first_message) std::cout << ',';
                first_message = false;
                std::cout << "{\"kind\":" << jsonString(message.message_kind) << ",\"metadata\":{";
                bool first_field = true;
                for (const auto& field : message.metadata) {
                    if (!first_field) std::cout << ',';
                    first_field = false;
                    std::cout << jsonString(field.name) << ':';
                    std::visit([](const auto& value) {
                        using T = std::decay_t<decltype(value)>;
                        if constexpr (std::is_same_v<T, std::string>) std::cout << jsonString(value);
                        else if constexpr (std::is_same_v<T, bool>) std::cout << (value ? "true" : "false");
                        else std::cout << value;
                    }, field.value);
                }
                std::cout << "}}";
            }
            std::cout << "],\"diagnostics\":[";
            for (std::size_t i = 0; i < parsing.diagnostics.size(); ++i) {
                if (i) std::cout << ',';
                std::cout << jsonString(parsing.diagnostics[i]);
            }
            std::cout << "]}\n";
            return 0;
        }
        if (argc != 5 || std::string(argv[1]) != "--analyze") {
            std::cout << "Usage: bus_analyze --fixture NEW_ARCHIVE\n"
                << "       bus_analyze --validate-config CONFIG_INI\n"
                << "       bus_analyze --inspect INPUT_ARCHIVE\n"
                << "       bus_analyze --record INPUT_ARCHIVE RECORD_INDEX\n"
                << "       bus_analyze --analyze CONFIG_INI INPUT_ARCHIVE NEW_OUTPUT_DIRECTORY\n";
            return argc == 1 ? 0 : 2;
        }
        const auto config_path = pathFromUtf8(argv[2]), input_path = pathFromUtf8(argv[3]), output = pathFromUtf8(argv[4]);
        auto config = BackendConfiguration::load(config_path);
        ArchiveReader reader(input_path);
        config.recording_session_id = reader.summary().session_id;
        ProtocolPipelineDecoder decoder(config);
        auto sink = std::make_shared<JsonlAnalysisSink>(output);
        config.document->save(output / "configuration.ini");
        {
            std::ofstream provenance(output / "input_archive.txt", std::ios::binary);
            const auto utf8 = absolutePath(input_path).u8string();
            provenance << std::string(utf8.begin(), utf8.end()) << '\n';
            if (!provenance) throw std::runtime_error("cannot write input provenance");
        }
        ProcessingService service(config, sink);
        RawFrame frame; std::uint64_t records{}, unmatched{};
        while (reader.next(frame)) {
            ++records; const auto samples = decoder.decode(frame);
            if (samples.empty()) ++unmatched;
            for (const auto& sample : samples) service.consume(sample);
        }
        service.finish(); const auto stats = service.stats();
        std::ofstream summary(output / "summary.json", std::ios::binary);
        summary << "{\"schema\":2,\"status\":\"complete\",\"build\":"; writeBuildInfo(summary);
        summary << ",\"archive\":"; writeArchiveSummary(summary, reader.summary());
        summary << ",\"configuration_sha256\":" << jsonString(hexIdentity(config.document->sha256)) << ",\"records\":" << records
            << ",\"unmatched_records\":" << unmatched << ",\"samples\":" << stats.samples << ",\"usable\":" << stats.usable
            << ",\"rejected\":" << stats.rejected << ",\"results\":" << stats.results << ",\"outcomes\":{";
        bool first = true;
        for (const auto& [outcome, count] : stats.outcomes) {
            if (!first) summary << ',';
            first = false; summary << '"' << outcome << "\":" << count;
            std::cout << outcome << '=' << count << '\n';
        }
        summary << "}}\n"; summary.flush();
        if (!summary) throw std::runtime_error("cannot complete summary");
        std::cout << "records=" << records << " samples=" << stats.samples << " usable=" << stats.usable
            << " rejected=" << stats.rejected << " results=" << stats.results << '\n';
        return 0;
    } catch (const std::exception& e) { std::cerr << "analysis failed: " << e.what() << '\n'; return 1; }
}
