#pragma once
#include "core/frame_pipeline.hpp"
#include <tuple>

namespace avionics {
class SimulatedDecoder final : public IParameterDecoder {
public:
    std::vector<ParameterSample> decode(const RawFrame&) override;
};
// An intentionally small demonstrator, not an aircraft diagnosis algorithm.
class RuleAnalyzer final : public IParameterConsumer {
public:
    explicit RuleAnalyzer(double threshold, std::size_t history_limit = 1024);
    void consume(const ParameterSample&) override;
    std::vector<ParameterSample> samples() const;
    std::vector<AnalysisEvent> events() const;
private:
    double threshold_;
    std::size_t limit_;
    mutable std::mutex mutex_;
    std::deque<ParameterSample> samples_;
    std::deque<AnalysisEvent> events_;
    // One current generation per source/parameter: replacement resets the rule.
    struct Previous { std::uint64_t generation, origin_generation; double value; };
    std::map<std::pair<std::string, std::string>, Previous> previous_;
};
}
