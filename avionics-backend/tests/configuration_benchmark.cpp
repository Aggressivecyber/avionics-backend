#include "core/processing.hpp"
#include <chrono>
#include <iostream>
#include <stdexcept>

using namespace avionics;
struct CountSink final : IAnalysisSink {
    void sample(const AlignedSample&) override {}
    void result(const CorrelationResult&) override {}
    void flush() override {}
};

int main() {
    for (int fields : {1, 1000, 10000}) {
        BackendConfiguration config; config.version = "benchmark";
        FieldDefinition field; field.source = "bench"; field.protocol = 2; field.bit_width = 64;
        field.type = FieldType::Unsigned; field.unit = "raw"; field.max_age_ns = 1000000;
        config.fields.assign(static_cast<std::size_t>(fields), field);
        for (int i = 0; i < fields; ++i) config.fields[static_cast<std::size_t>(i)].id = "p" + std::to_string(i);
        config.fields.back().protocol = 1;
        DictionaryDecoder decoder(std::move(config));
        RawFrame frame; frame.source = "bench"; frame.protocol = 1; frame.payload.resize(8);
        for (int run = 0; run < 3; ++run) {
            std::uint64_t samples{};
            const auto begin = std::chrono::steady_clock::now();
            for (int i = 0; i < 20000; ++i) samples += decoder.decode(frame).size();
            const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - begin).count();
            if (samples != 20000) throw std::runtime_error("dictionary benchmark output mismatch");
            std::cout << "{\"component\":\"dictionary\",\"size\":" << fields << ",\"run\":" << run
                      << ",\"items\":20000,\"elapsed_ns\":" << elapsed << "}\n";
        }
    }
    for (const std::string family : {"consistency_rules", "response_rules"}) {
      for (int rules : {0, 100, 1000, 5000}) {
        BackendConfiguration config; config.version = "benchmark";
        config.clocks.push_back({"bench", "host", 1, 0, 0});
        for (int i = 0; i < rules; ++i) {
            if (family == "consistency_rules") config.consistency.push_back({"rule" + std::to_string(i), {"left", "p"}, {"right", "p"}, 1, 1000000});
            else config.responses.push_back({"response" + std::to_string(i), {"left", "p"}, {"right", "p"}, 0.5, 0.5, 1000000});
        }
        for (int run = 0; run < 3; ++run) {
            ProcessingService service(config, std::make_shared<CountSink>());
            ParameterSample sample; sample.source = "bench"; sample.parameter_id = "p"; sample.clock_domain = 1;
            sample.generation = 1; sample.valid = true; sample.value = 1.0; sample.max_age_ns = 1000000;
            const auto begin = std::chrono::steady_clock::now();
            for (std::uint64_t i = 0; i < 10000; ++i) {
                sample.sequence = i; sample.capture_time_ns = 1000000000 + i;
                service.consume(sample);
            }
            const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - begin).count();
            if (service.stats().usable != 10000) throw std::runtime_error("rule benchmark output mismatch");
            std::cout << "{\"component\":\"" << family << "\",\"size\":" << rules << ",\"run\":" << run
                      << ",\"items\":10000,\"elapsed_ns\":" << elapsed << "}\n";
        }
      }
    }
}
