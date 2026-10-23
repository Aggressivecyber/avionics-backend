#include "core/processing.hpp"
#include <cmath>
#include <limits>
#include <stdexcept>

namespace avionics {
namespace {
bool sameEpoch(const ParameterSample& a, const ParameterSample& b) {
    return a.generation == b.generation && a.origin_generation == b.origin_generation && a.decoder_version == b.decoder_version &&
        a.recording_session_id == b.recording_session_id && a.origin_session_id == b.origin_session_id &&
        a.configuration_sha256 == b.configuration_sha256;
}
std::optional<double> numeric(const ParameterValue& value) {
    return std::visit([](const auto& item) -> std::optional<double> {
        using T = std::decay_t<decltype(item)>;
        if constexpr (std::is_same_v<T, EnumValue>) return {};
        else {
            // Keep exact integers in storage; only safely representable values enter double-valued rules.
            if constexpr (std::is_integral_v<T>) {
                const auto extended = static_cast<long double>(item);
                if (extended > 9007199254740992.0L || extended < -9007199254740992.0L) return {};
            }
            const auto converted = static_cast<double>(item);
            return std::isfinite(converted) ? std::optional<double>(converted) : std::nullopt;
        }
    }, value);
}
EvidenceRef evidence(const AlignedSample& value) {
    return {value.sample.source, value.sample.parameter_id, value.sample.decoder_version, value.sample.generation,
        value.sample.origin_generation, value.sample.raw_record_index, value.time_ns.value_or(0),
        value.sample.recording_session_id, value.sample.origin_session_id, value.sample.configuration_sha256,
        value.sample.origin_source, value.sample.origin_record_index};
}
bool fresh(const AlignedSample& sample, std::uint64_t now) {
    if (!sample.usable() || !sample.time_ns || now < *sample.time_ns) return false;
    const auto age = now - *sample.time_ns;
    return sample.uncertainty_ns <= sample.sample.max_age_ns && age <= sample.sample.max_age_ns - sample.uncertainty_ns;
}
std::uint64_t distance(std::uint64_t a, std::uint64_t b) { return a > b ? a - b : b - a; }
CorrelationResult event(const std::string& id, const std::string& outcome, const AlignedSample& first,
    const AlignedSample* second = nullptr) {
    CorrelationResult result;
    result.rule = id; result.outcome = outcome; result.time_group = first.time_group;
    result.time_ns = first.time_ns.value_or(0); result.evidence.push_back(evidence(first));
    if (second) { result.evidence.push_back(evidence(*second)); result.time_ns = second->time_ns.value_or(result.time_ns); }
    return result;
}
}
TimeQualityProcessor::TimeQualityProcessor(std::vector<ClockDefinition> clocks, std::size_t capacity) : capacity_(capacity) {
    for (auto& clock : clocks) {
        const auto key = std::make_pair(clock.source, clock.domain);
        clocks_.emplace(key, std::move(clock));
    }
}
AlignedSample TimeQualityProcessor::process(const ParameterSample& sample) {
    AlignedSample out; out.sample = sample;
    if (!sample.valid) out.quality.push_back("invalid_payload");
    const auto found_clock = clocks_.find({sample.source, sample.clock_domain});
    if (found_clock == clocks_.end()) { out.quality.push_back("unmapped_clock"); return out; }
    const auto* clock = &found_clock->second;
    out.time_group = clock->group; out.uncertainty_ns = clock->uncertainty_ns;
    if (clock->offset_ns >= 0) {
        const auto offset = static_cast<std::uint64_t>(clock->offset_ns);
        if (sample.capture_time_ns > UINT64_MAX - offset) { out.quality.push_back("time_overflow"); return out; }
        out.time_ns = sample.capture_time_ns + offset;
    } else {
        const auto offset = static_cast<std::uint64_t>(-(clock->offset_ns + 1)) + 1;
        if (sample.capture_time_ns < offset) { out.quality.push_back("time_underflow"); return out; }
        out.time_ns = sample.capture_time_ns - offset;
    }
    // Mapping errors and invalid payloads do not advance ordering state.
    if (!sample.valid) return out;
    const auto key = std::make_pair(sample.source, sample.parameter_id);
    auto found = previous_.find(key);
    if (found != previous_.end() && sameEpoch(found->second.sample, sample)) {
        const auto& last = found->second;
        if (*out.time_ns < last.time_ns) out.quality.push_back("out_of_order");
        if (sample.sequence == last.sample.sequence) out.quality.push_back("duplicate_sequence");
        else if (sample.sequence_step && (sample.sequence < last.sample.sequence ||
            sample.sequence - last.sample.sequence != sample.sequence_step)) out.quality.push_back("sequence_gap");
        if (*out.time_ns < last.time_ns || sample.sequence == last.sample.sequence) return out;
    }
    if (!previous_.contains(key) && previous_.size() >= capacity_) throw std::runtime_error("time quality state capacity exceeded");
    previous_.insert_or_assign(key, Last{sample, *out.time_ns});
    return out;
}
ProcessingService::ProcessingService(BackendConfiguration config, std::shared_ptr<IAnalysisSink> sink)
    : config_(std::move(config)), quality_(config_.clocks, config_.max_parameter_states), sink_(std::move(sink)),
      pairs_(config_.consistency.size()), responses_(config_.responses.size()), limit_previous_(config_.limits.size()) {
    if (!sink_) throw std::invalid_argument("analysis sink is required");
    for (const auto& clock : config_.clocks) {
        known_groups_.insert(clock.group);
        response_clock_uncertainty_[{clock.source, clock.group}] += clock.uncertainty_ns;
    }
    for (std::size_t i = 0; i < config_.consistency.size(); ++i) {
        const auto& rule = config_.consistency[i];
        consistency_by_input_[{rule.left.source, rule.left.parameter}].push_back(i);
        consistency_by_input_[{rule.right.source, rule.right.parameter}].push_back(i);
    }
    for (std::size_t i = 0; i < config_.responses.size(); ++i) {
        const auto& rule = config_.responses[i];
        responses_by_input_[{rule.command.source, rule.command.parameter}].push_back(i);
        responses_by_input_[{rule.response.source, rule.response.parameter}].push_back(i);
    }
    for (std::size_t i = 0; i < config_.limits.size(); ++i) {
        const auto& input = config_.limits[i].input;
        limits_by_input_[{input.source, input.parameter}].push_back(i);
    }
}
void ProcessingService::emit(CorrelationResult result) {
    sink_->result(result);
    ++stats_.results; ++stats_.outcomes[result.outcome];
    history_.push_back(std::move(result));
    if (history_.size() > 1024) history_.pop_front();
}
void ProcessingService::consume(const ParameterSample& sample) {
    std::lock_guard lock(mutex_);
    if (finished_) throw std::logic_error("analysis is already finished");
    if (!config_.allowed_lateness_ns) { processAligned(quality_.process(sample)); return; }
    auto aligned = quality_.process(sample);
    if (!aligned.time_ns) {
        // An unplaceable clock boundary invalidates this source's buffered
        // observations, so they cannot later restore a pre-boundary baseline.
        for (auto& [group_name, group] : reorder_) {
            (void)group_name;
            for (auto item = group.samples.begin(); item != group.samples.end();) {
                if (item->second.sample.source != sample.source) { ++item; continue; }
                auto cancelled = std::move(item->second);
                cancelled.quality.push_back("unmapped_clock_boundary");
                item = group.samples.erase(item); --buffered_;
                processAligned(std::move(cancelled));
            }
        }
        processAligned(std::move(aligned)); return;
    }
    auto& group = reorder_[aligned.time_group];
    const auto time = *aligned.time_ns;
    if (time < group.closed_before || (group.closed_through && time <= *group.closed_through)) {
        aligned.quality.push_back("late_for_group"); processAligned(std::move(aligned)); return;
    }
    if (aligned.usable()) {
        group.max_seen = std::max(group.max_seen, time);
        const auto limit = group.max_seen > config_.allowed_lateness_ns ? group.max_seen - config_.allowed_lateness_ns : 0;
        // Equality remains open, allowing same-time observations to be ordered
        // by stable stream identity rather than callback scheduling.
        drain(group, limit, false); group.closed_before = std::max(group.closed_before, limit);
    }
    if (buffered_ >= config_.max_reorder_samples) throw std::runtime_error("reorder sample capacity exceeded");
    group.samples.emplace(PendingKey{time, sample.source, sample.parameter_id, sample.generation,
        sample.origin_generation, sample.sequence, sample.raw_record_index}, std::move(aligned));
    ++buffered_;
}
void ProcessingService::drain(ReorderGroup& group, std::uint64_t limit, bool inclusive) {
    while (!group.samples.empty()) {
        auto item = group.samples.begin(); const auto time = std::get<0>(item->first);
        if (time > limit || (!inclusive && time == limit)) break;
        auto sample = std::move(item->second); group.samples.erase(item); --buffered_;
        processAligned(std::move(sample));
    }
}
void ProcessingService::processAligned(AlignedSample aligned) {
    if (aligned.usable()) {
        auto& watermark = watermarks_[aligned.time_group];
        if (*aligned.time_ns < watermark) aligned.quality.push_back("late_for_group");
        else {
            // A response arriving EXACTLY at its deadline is processed before expiry.
            expire(aligned.time_group, *aligned.time_ns, false);
            watermark = *aligned.time_ns;
        }
    }
    sink_->sample(aligned);
    ++stats_.samples;
    if (aligned.usable()) ++stats_.usable; else ++stats_.rejected;
    processConsistency(aligned); processResponse(aligned); processLimit(aligned);
}
void ProcessingService::processLimit(const AlignedSample& sample) {
    const auto found = limits_by_input_.find({sample.sample.source, sample.sample.parameter_id});
    if (found == limits_by_input_.end()) return;
    for (const auto i : found->second) {
        auto& previous = limit_previous_[i];
        const auto value = numeric(sample.sample.value);
        if (!sample.usable() || !value) { previous.reset(); continue; }
        const auto& rule = config_.limits[i];
        const bool above = *value > rule.upper;
        const bool continuous = previous && sameEpoch(previous->sample, sample.sample) &&
            previous->time_group == sample.time_group && fresh(*previous, *sample.time_ns);
        const bool was_above = continuous && *numeric(previous->sample.value) > rule.upper;
        // A first valid high value is an alarm; recovery requires a continuous high baseline.
        if ((above && !was_above) || (!above && was_above)) {
            auto result = event(rule.id, above ? "parameter_above_limit" : "parameter_within_limit", sample);
            result.metric = *value; emit(std::move(result));
        }
        previous = sample;
    }
}
void ProcessingService::processConsistency(const AlignedSample& sample) {
    const auto found = consistency_by_input_.find({sample.sample.source, sample.sample.parameter_id});
    if (found == consistency_by_input_.end()) return;
    for (std::size_t i : found->second) {
        const auto& rule = config_.consistency[i]; auto& pair = pairs_[i];
        const bool left = rule.left.matches(sample.sample);
        auto& side = left ? pair.left : pair.right;
        const bool changed = side && !sameEpoch(side->sample, sample.sample);
        if (changed || !sample.usable()) { pair.left.reset(); pair.right.reset(); }
        if (!sample.usable()) continue;
        side = sample;
        if (!pair.left || !pair.right) continue;
        const auto& a = *pair.left; const auto& b = *pair.right;
        if (a.time_group != b.time_group) continue;
        const auto now = std::max(*a.time_ns, *b.time_ns);
        if (!fresh(a, now) || !fresh(b, now)) continue;
        const auto difference = distance(*a.time_ns, *b.time_ns);
        if (difference > rule.window_ns || a.uncertainty_ns > rule.window_ns - difference ||
            b.uncertainty_ns > rule.window_ns - difference - a.uncertainty_ns) continue;
        if (a.sample.unit != b.sample.unit) { emit(event(rule.id, "unit_mismatch", a, &b)); continue; }
        const auto av = numeric(a.sample.value), bv = numeric(b.sample.value);
        if (!av || !bv) { emit(event(rule.id, "non_numeric_pair", a, &b)); continue; }
        const auto residual = *av - *bv;
        auto result = event(rule.id, !std::isfinite(residual) ? "numeric_overflow" :
            (std::abs(residual) <= rule.tolerance ? "consistent" : "inconsistent"), a, &b);
        if (std::isfinite(residual)) result.metric = residual;
        result.time_ns = now; emit(std::move(result));
    }
}
void ProcessingService::processResponse(const AlignedSample& sample) {
    const auto found = responses_by_input_.find({sample.sample.source, sample.sample.parameter_id});
    if (found == responses_by_input_.end()) return;
    for (std::size_t i : found->second) {
        const auto& rule = config_.responses[i]; auto& state = responses_[i];
        const bool command = rule.command.matches(sample.sample), response = rule.response.matches(sample.sample);
        auto& previous = command ? state.command : state.response;
        const bool boundary = previous && !sameEpoch(previous->sample, sample.sample);
        const auto value = numeric(sample.sample.value);
        if (boundary || !sample.usable() || !value) {
            if (state.pending) emit(event(rule.id, "response_cancelled", *state.pending, &sample));
            clearPending(i);
            state = {};
            if (!sample.usable() || !value) continue;
        }
        auto& old = command ? state.command : state.response;
        const auto old_value = old ? numeric(old->sample.value) : std::nullopt;
        const auto threshold = command ? rule.command_threshold : rule.response_threshold;
        const bool rising = old && old_value && old->time_group == sample.time_group && fresh(*old, *sample.time_ns) &&
            *old_value < threshold && *value >= threshold;
        if (command && rising) {
            if (state.pending) emit(event(rule.id, "response_superseded", *state.pending, &sample));
            clearPending(i);
            const auto baseline = state.response ? numeric(state.response->sample.value) : std::nullopt;
            if (!state.response || state.response->time_group != sample.time_group || !fresh(*state.response, *sample.time_ns) || !baseline)
                emit(event(rule.id, "response_missing_baseline", sample));
            else if (*baseline >= rule.response_threshold) emit(event(rule.id, "response_already_high", sample, &*state.response));
            else if (*sample.time_ns > UINT64_MAX - rule.max_delay_ns) emit(event(rule.id, "response_time_overflow", sample));
            else schedulePending(i, sample);
        }
        if (response && rising && state.pending && state.pending->time_group == sample.time_group &&
            *sample.time_ns >= *state.pending->time_ns) {
            const auto delta = *sample.time_ns - *state.pending->time_ns;
            const auto uncertainty = state.pending->uncertainty_ns + sample.uncertainty_ns;
            auto result = event(rule.id, "response_observed", *state.pending, &sample);
            result.metric = static_cast<double>(delta) / 1000000.0;
            result.latency_low_ns = delta > uncertainty ? delta - uncertainty : 0;
            result.latency_high_ns = delta > UINT64_MAX - uncertainty ? UINT64_MAX : delta + uncertainty;
            if (*result.latency_high_ns > rule.max_delay_ns || delta < uncertainty) result.outcome = "response_time_ambiguous";
            emit(std::move(result)); clearPending(i);
        }
        old = sample;
    }
}
void ProcessingService::clearPending(std::size_t index) {
    auto& state = responses_[index];
    if (state.pending) deadlines_[state.pending->time_group].erase({*state.final_deadline, index});
    state.pending.reset(); state.final_deadline.reset();
}
void ProcessingService::schedulePending(std::size_t index, const AlignedSample& sample) {
    auto& state = responses_[index]; const auto& rule = config_.responses[index]; state.pending = sample;
    const auto deadline = *sample.time_ns + rule.max_delay_ns;
    const auto uncertainty = sample.uncertainty_ns + response_clock_uncertainty_[{rule.response.source, sample.time_group}];
    state.final_deadline = deadline > UINT64_MAX - uncertainty ? UINT64_MAX : deadline + uncertainty;
    deadlines_[sample.time_group].emplace(*state.final_deadline, index);
}
void ProcessingService::expire(const std::string& group, std::uint64_t time, bool inclusive) {
    const auto found = deadlines_.find(group);
    if (found == deadlines_.end()) return;
    auto& due = found->second;
    while (!due.empty()) {
        const auto [final_deadline, i] = *due.begin();
        if (time < final_deadline || (!inclusive && time == final_deadline)) break;
        auto& state = responses_[i]; const auto& rule = config_.responses[i];
        const auto deadline = *state.pending->time_ns + rule.max_delay_ns;
        const bool observed = state.response && state.response->time_group == group && fresh(*state.response, deadline);
        auto result = event(rule.id, observed ? "response_timeout" : "response_observation_gap", *state.pending,
            state.response ? &*state.response : nullptr);
        result.time_ns = deadline; emit(std::move(result)); clearPending(i);
    }
}
void ProcessingService::advance(const std::string& group, std::uint64_t watermark) {
    std::lock_guard lock(mutex_);
    if (finished_) throw std::logic_error("analysis is already finished");
    if (!known_groups_.contains(group) || watermark < watermarks_[group]) throw std::invalid_argument("unknown group or backward watermark");
    if (config_.allowed_lateness_ns) {
        auto& pending = reorder_[group];
        if (watermark < pending.closed_before || (pending.closed_through && watermark < *pending.closed_through))
            throw std::invalid_argument("backward reorder watermark");
        drain(pending, watermark, true); pending.closed_through = watermark;
    }
    expire(group, watermark, true); watermarks_[group] = watermark;
}
void ProcessingService::finish() {
    std::lock_guard lock(mutex_);
    if (finished_) return;
    for (auto& [group_name, group] : reorder_) { (void)group_name; drain(group, UINT64_MAX, true); }
    for (std::size_t i = 0; i < responses_.size(); ++i) if (responses_[i].pending) {
        emit(event(config_.responses[i].id, "response_unresolved", *responses_[i].pending)); clearPending(i);
    }
    sink_->flush(); finished_ = true;
}
ProcessingStats ProcessingService::stats() const {
    std::lock_guard lock(mutex_); auto result = stats_; result.buffered = buffered_; return result;
}
std::vector<CorrelationResult> ProcessingService::results() const {
    std::lock_guard lock(mutex_); return {history_.begin(), history_.end()};
}
}
