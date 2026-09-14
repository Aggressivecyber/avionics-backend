#include "core/processing.hpp"
#include "core/demo_fixture.hpp"
#include "core/path.hpp"
#include <iostream>

int main(int argc, char** argv) {
    using namespace avionics;
    try {
        if (argc == 3 && std::string(argv[1]) == "--fixture") {
            ArchiveWriter writer(pathFromUtf8(argv[2]));
            const auto frames = correlationFixture();
            for (const auto& frame : frames) writer.write(frame);
            writer.flush(); std::cout << "synthetic_records=" << frames.size() << '\n'; return 0;
        }
        if (argc != 5 || std::string(argv[1]) != "--analyze") {
            std::cout << "Usage: bus_analyze --fixture NEW_ARCHIVE\n"
                << "       bus_analyze --analyze CONFIG_INI INPUT_ARCHIVE NEW_OUTPUT_DIRECTORY\n";
            return argc == 1 ? 0 : 2;
        }
        const auto config_path = pathFromUtf8(argv[2]), input_path = pathFromUtf8(argv[3]), output = pathFromUtf8(argv[4]);
        const auto config = BackendConfiguration::load(config_path);
        ArchiveReader reader(input_path);
        DictionaryDecoder decoder(config);
        auto sink = std::make_shared<JsonlAnalysisSink>(output);
        std::filesystem::copy_file(config_path, output / "configuration.ini");
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
        summary << "{\"schema\":1,\"status\":\"complete\",\"records\":" << records
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
