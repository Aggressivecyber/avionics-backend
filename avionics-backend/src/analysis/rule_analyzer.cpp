#include "core/analysis.hpp"
#include <cmath>
#include <stdexcept>

namespace avionics {
RuleAnalyzer::RuleAnalyzer(double threshold, std::size_t limit) : threshold_(threshold), limit_(limit) {
    if (!std::isfinite(threshold) || !limit) throw std::invalid_argument("invalid analyzer settings");
}
void RuleAnalyzer::consume(const ParameterSample& sample) {
    std::lock_guard lock(mutex_);
    samples_.push_back(sample);
    if (samples_.size() > limit_) samples_.pop_front();
    const auto key = std::make_pair(sample.source, sample.parameter_id);
    const auto* value = std::get_if<double>(&sample.value);
    if (!sample.valid || !value || !std::isfinite(*value)) { previous_.erase(key); return; }
    const auto found = previous_.find(key);
    if (found != previous_.end() && found->second.generation == sample.generation &&
        found->second.origin_generation == sample.origin_generation &&
        found->second.value < threshold_ && *value >= threshold_) {
        events_.push_back({"demo.threshold.rising.v1", sample.source, sample.parameter_id,
            "Simulated temperature crossed the configured threshold", sample.generation,
            sample.capture_time_ns, sample.raw_record_index, *value});
        if (events_.size() > limit_) events_.pop_front();
    }
    previous_.insert_or_assign(key, Previous{sample.generation, sample.origin_generation, *value});
}
std::vector<ParameterSample> RuleAnalyzer::samples() const {
    std::lock_guard lock(mutex_); return {samples_.begin(), samples_.end()};
}
std::vector<AnalysisEvent> RuleAnalyzer::events() const {
    std::lock_guard lock(mutex_); return {events_.begin(), events_.end()};
}
}
