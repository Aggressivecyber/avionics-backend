#include "core/processing.hpp"
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
        if (argc != 5 || std::string(argv[1]) != "--analyze") {
            std::cout << "Usage: bus_analyze --fixture NEW_ARCHIVE\n"
                << "       bus_analyze --inspect INPUT_ARCHIVE\n"
                << "       bus_analyze --analyze CONFIG_INI INPUT_ARCHIVE NEW_OUTPUT_DIRECTORY\n";
            return argc == 1 ? 0 : 2;
        }
        const auto config_path = pathFromUtf8(argv[2]), input_path = pathFromUtf8(argv[3]), output = pathFromUtf8(argv[4]);
        auto config = BackendConfiguration::load(config_path);
        ArchiveReader reader(input_path);
        config.recording_session_id = reader.summary().session_id;
        DictionaryDecoder decoder(config);
        auto sink = std::make_shared<JsonlAnalysisSink>(output);
        config.document->save(output / "configuration.ini");
        {
            std::ofstream provenance(output / "input_archive.txt", std::ios::binary);
            const auto utf8 = std::filesystem::absolute(input_path).u8string();
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
