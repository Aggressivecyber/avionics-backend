#include "core/protocol_impl.hpp"
#include <algorithm>
#include <iterator>
#include <set>
#include <stdexcept>
#include <utility>

namespace avionics {
namespace {
bool sameInputDescriptor(const ProtocolInputDescriptor& left, const ProtocolInputDescriptor& right) {
    return left.source_hint == right.source_hint && left.hint_is_authoritative == right.hint_is_authoritative &&
        left.capture_representation == right.capture_representation;
}
bool listsRepresentation(const ProtocolDescriptor& descriptor, const std::string& representation) {
    return std::find(descriptor.capture_representations.begin(), descriptor.capture_representations.end(),
        representation) != descriptor.capture_representations.end();
}
}

ProtocolRouter::ProtocolRouter(std::shared_ptr<const IProtocolFactory> factory) : factory_(std::move(factory)) {
    if (!factory_) throw std::invalid_argument("protocol router requires a factory");
}

ProtocolRouter::AddressKey ProtocolRouter::addressKey(const StreamAddress& address) {
    return {address.source, address.channel, address.origin_source};
}

ProtocolRouter::StreamKeyTuple ProtocolRouter::streamKey(const ProtocolStreamKey& key) {
    return {key.address.source, key.address.channel, key.address.origin_source, key.generation, key.origin_generation};
}

bool ProtocolRouter::supported(ProtocolId protocol) const {
    for (const auto& descriptor : factory_->descriptors())
        if (descriptor.id == protocol) return true;
    return false;
}

ProtocolDescriptor ProtocolRouter::descriptorFor(ProtocolId protocol) const {
    for (const auto& descriptor : factory_->descriptors())
        if (descriptor.id == protocol) return descriptor;
    return {};
}

const ProtocolRouter::AddressState& ProtocolRouter::addressOr(const StreamAddress& address) const {
    static const AddressState fallback{};
    const auto found = addresses_.find(addressKey(address));
    return found == addresses_.end() ? fallback : found->second;
}

void ProtocolRouter::clearAddress(const StreamAddress& address) {
    const auto target = addressKey(address);
    for (auto it = streams_.begin(); it != streams_.end();) {
        const auto& key = it->first;
        const bool matches = std::get<0>(key) == std::get<0>(target) && std::get<1>(key) == std::get<1>(target) &&
            std::get<2>(key) == std::get<2>(target);
        it = matches ? streams_.erase(it) : std::next(it);
    }
}

void ProtocolRouter::setSelection(const StreamAddress& address, ProtocolSelection selection) {
    if (selection.mode() == ProtocolSelectionMode::Manual && !supported(*selection.protocol()))
        throw std::invalid_argument("manual selection requires a supported protocol id");
    auto& state = addresses_[addressKey(address)];
    if (state.selection.mode() == selection.mode() && state.selection.protocol() == selection.protocol()) return;
    state.selection = selection;
    clearAddress(address);
}

ProtocolSelection ProtocolRouter::selection(const StreamAddress& address) const {
    const auto found = addresses_.find(addressKey(address));
    return found == addresses_.end() ? ProtocolSelection::automatic() : found->second.selection;
}

void ProtocolRouter::setInputDescriptor(const StreamAddress& address, const ProtocolInputDescriptor& descriptor) {
    auto& state = addresses_[addressKey(address)];
    if (sameInputDescriptor(state.descriptor, descriptor)) return;
    state.descriptor = descriptor;
    clearAddress(address);
}

ProtocolInputDescriptor ProtocolRouter::inputDescriptor(const StreamAddress& address) const {
    const auto found = addresses_.find(addressKey(address));
    return found == addresses_.end() ? ProtocolInputDescriptor{} : found->second.descriptor;
}

void ProtocolRouter::setLimits(const ProtocolRoutingLimits& limits) {
    if (!limits.max_streams || !limits.max_observation_frames_per_stream ||
        !limits.max_observation_bytes_per_stream || !limits.max_candidates_per_stream)
        throw std::invalid_argument("protocol routing limits must be positive");
    const bool shrinks = limits.max_streams < limits_.max_streams ||
        limits.max_observation_frames_per_stream < limits_.max_observation_frames_per_stream ||
        limits.max_observation_bytes_per_stream < limits_.max_observation_bytes_per_stream ||
        limits.max_candidates_per_stream < limits_.max_candidates_per_stream;
    if (shrinks) streams_.clear();
    limits_ = limits;
}

ProtocolDetectionReport ProtocolRouter::probeStream(
    StreamState& state, const ProtocolInputDescriptor& descriptor, const RawFrame& frame) {
    ProtocolDetectionReport report;
    if (state.observations.size() >= limits_.max_observation_frames_per_stream ||
        state.observation_bytes + frame.payload.size() > limits_.max_observation_bytes_per_stream) {
        report.status = ProtocolDetectionStatus::Rejected;
        report.reason = "observation budget exhausted before identification";
        return report;
    }
    state.observations.push_back(frame);
    state.observation_bytes += frame.payload.size();

    const ProtocolDetectionContext context{state.key, descriptor};
    std::vector<ProtocolCandidate> candidates;
    std::vector<ProtocolId> strong;
    bool need_more = false;
    bool rejected = false;
    std::string reject_reason;
    bool representation_matched = descriptor.capture_representation.empty();
    for (const auto& candidate_descriptor : factory_->descriptors()) {
        if (!descriptor.capture_representation.empty() &&
            !listsRepresentation(candidate_descriptor, descriptor.capture_representation)) continue;
        representation_matched = true;
        auto detector = factory_->createDetector(candidate_descriptor.id);
        if (!detector) continue;
        auto probe = detector->probe(context, state.observations);
        if (probe.status == ProtocolDetectionStatus::Identified && probe.selected) strong.push_back(*probe.selected);
        if (probe.status == ProtocolDetectionStatus::NeedMoreData) need_more = true;
        if (probe.status == ProtocolDetectionStatus::Rejected) {
            rejected = true;
            if (reject_reason.empty()) reject_reason = probe.reason;
        }
        for (auto& candidate : probe.candidates) candidates.push_back(std::move(candidate));
    }
    if (!representation_matched) {
        report.status = ProtocolDetectionStatus::Rejected;
        report.reason = "capture representation does not match any supported protocol";
        return report;
    }
    if (descriptor.source_hint && !descriptor.hint_is_authoritative && supported(*descriptor.source_hint)) {
        ProtocolCandidate hint;
        hint.protocol = *descriptor.source_hint;
        hint.strength = CandidateStrength::Tentative;
        hint.evidence.push_back({ProtocolEvidenceKind::SourceDescriptor, "non-authoritative source hint"});
        candidates.push_back(std::move(hint));
    }
    if (candidates.size() > limits_.max_candidates_per_stream) {
        report.status = ProtocolDetectionStatus::Rejected;
        report.reason = "candidate budget exceeded";
        return report;
    }
    report.candidates = std::move(candidates);

    std::sort(strong.begin(), strong.end());
    strong.erase(std::unique(strong.begin(), strong.end()), strong.end());
    if (strong.size() == 1) {
        report.status = ProtocolDetectionStatus::Identified;
        report.basis = ProtocolSelectionBasis::ContentEvidence;
        report.selected = strong.front();
        report.reason = "single protocol satisfied content evidence";
        return report;
    }
    if (strong.size() > 1) {
        report.status = ProtocolDetectionStatus::Ambiguous;
        report.reason = "multiple protocols satisfied content evidence";
        return report;
    }
    std::set<ProtocolId> tentative;
    for (const auto& candidate : report.candidates)
        if (candidate.strength == CandidateStrength::Tentative) tentative.insert(candidate.protocol);
    if (tentative.size() > 1) {
        report.status = ProtocolDetectionStatus::Ambiguous;
        report.reason = "multiple tentative candidates cannot be excluded";
        return report;
    }
    if (rejected) {
        report.status = ProtocolDetectionStatus::Rejected;
        report.reason = reject_reason;
        return report;
    }
    if (state.observations.size() >= limits_.max_observation_frames_per_stream ||
        state.observation_bytes >= limits_.max_observation_bytes_per_stream) {
        report.status = ProtocolDetectionStatus::Rejected;
        report.reason = "observation budget exhausted before identification";
        return report;
    }
    if (need_more || tentative.size() == 1) {
        report.status = ProtocolDetectionStatus::NeedMoreData;
        report.reason = "insufficient evidence within the observation window";
        return report;
    }
    report.status = ProtocolDetectionStatus::Unknown;
    report.reason = "no protocol evidence in the observation window";
    return report;
}

ProtocolParseResult ProtocolRouter::replayObservations(StreamState& state) {
    ProtocolParseResult combined;
    bool complete = false;
    for (const auto& observation : state.observations) {
        auto parsed = state.handler->parse(observation);
        if (parsed.status == ProtocolParseStatus::Malformed || parsed.status == ProtocolParseStatus::Unsupported)
            return parsed;
        if (parsed.status == ProtocolParseStatus::Complete) complete = true;
        for (auto& message : parsed.messages) combined.messages.push_back(std::move(message));
        for (auto& diagnostic : parsed.diagnostics) combined.diagnostics.push_back(std::move(diagnostic));
    }
    state.observations.clear();
    state.observation_bytes = 0;
    combined.status = complete ? ProtocolParseStatus::Complete : ProtocolParseStatus::NeedMoreData;
    return combined;
}

ProtocolRouteResult ProtocolRouter::route(const RawFrame& frame) {
    ProtocolRouteResult result;
    result.stream = protocolStreamOf(frame);
    const auto key = result.stream;
    auto found = streams_.find(streamKey(key));
    StreamState* state = nullptr;
    if (found == streams_.end()) {
        if (streams_.size() >= limits_.max_streams) {
            result.detection.status = ProtocolDetectionStatus::Rejected;
            result.detection.reason = "active stream budget exhausted";
            result.parsing.status = ProtocolParseStatus::Unsupported;
            return result;
        }
        auto inserted = streams_.emplace(streamKey(key), StreamState{});
        inserted.first->second.key = key;
        state = &inserted.first->second;
    } else {
        state = &found->second;
    }
    const auto& address = addressOr(key.address);
    bool replayed = false;

    if (!state->selected) {
        if (address.selection.mode() == ProtocolSelectionMode::Manual) {
            state->selected = *address.selection.protocol();
            state->basis = ProtocolSelectionBasis::Manual;
            state->handler = factory_->createHandler(*state->selected, key);
        } else if (address.descriptor.source_hint && address.descriptor.hint_is_authoritative) {
            if (!supported(*address.descriptor.source_hint)) {
                result.detection.status = ProtocolDetectionStatus::Rejected;
                result.detection.reason = "authoritative source hint is not a supported protocol";
                result.parsing.status = ProtocolParseStatus::Unsupported;
                return result;
            }
            state->selected = *address.descriptor.source_hint;
            state->basis = ProtocolSelectionBasis::SourceDescriptor;
            state->handler = factory_->createHandler(*state->selected, key);
        } else {
            auto report = probeStream(*state, address.descriptor, frame);
            if (report.status != ProtocolDetectionStatus::Identified || !report.selected) {
                result.detection = std::move(report);
                result.parsing.status = report.status == ProtocolDetectionStatus::NeedMoreData
                    ? ProtocolParseStatus::NeedMoreData
                    : (report.status == ProtocolDetectionStatus::Rejected ? ProtocolParseStatus::Unsupported
                                                                          : ProtocolParseStatus::NotAttempted);
                return result;
            }
            state->selected = *report.selected;
            state->basis = ProtocolSelectionBasis::ContentEvidence;
            state->handler = factory_->createHandler(*state->selected, key);
            result.detection = std::move(report);
            replayed = true;
        }
        if (!state->handler) {
            state->selected.reset();
            state->basis = ProtocolSelectionBasis::None;
            result.detection.status = ProtocolDetectionStatus::Rejected;
            result.detection.selected.reset();
            result.detection.reason = "factory could not create a handler for the selected protocol";
            result.parsing.status = ProtocolParseStatus::Unsupported;
            return result;
        }
    }
    if (!result.detection.selected) {
        result.detection.status = ProtocolDetectionStatus::Identified;
        result.detection.basis = state->basis;
        result.detection.selected = state->selected;
        result.detection.reason = state->basis == ProtocolSelectionBasis::Manual ? "manual selection" : "source descriptor";
    }
    result.parsing = replayed ? replayObservations(*state) : state->handler->parse(frame);
    return result;
}

void ProtocolRouter::resetStream(const StreamAddress& address, ProtocolResetReason) { clearAddress(address); }

void ProtocolRouter::removeStream(const StreamAddress& address) {
    clearAddress(address);
    addresses_.erase(addressKey(address));
}
}
