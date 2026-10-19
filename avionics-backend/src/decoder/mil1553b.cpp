#include "core/mil1553b.hpp"
#include "plugin_api/bus_plugin.h"
#include "plugin_api/mil1553b_capture.hpp"
#include <algorithm>
#include <iterator>

namespace avionics {
namespace {
auto addressKey(const StreamAddress& address) { return std::tuple{address.source, address.channel, address.origin_source}; }
ProtocolParseResult malformed(std::string reason) { return {ProtocolParseStatus::Malformed, {}, {std::move(reason)}}; }
ProtocolParseResult unsupported(std::string reason) { return {ProtocolParseStatus::Unsupported, {}, {std::move(reason)}}; }
// MIL-STD-1553B figure 3: Message Error, Busy, Subsystem Flag, Terminal Flag.
constexpr std::uint16_t status_no_data = 0x0400 | 0x0008;
constexpr std::uint16_t status_bad_data = status_no_data | 0x0004 | 0x0001;
}
Mil1553BCommand decode1553BCommand(std::uint16_t word) noexcept {
    return {std::uint32_t(word >> 11), std::uint32_t((word >> 5) & 31), std::uint32_t(word & 31), (word & 0x0400) != 0};
}
ProtocolId Mil1553BDetector::protocolId() const noexcept { return BUS_PROTOCOL_MIL1553B; }
ProtocolId Mil1553BHandler::protocolId() const noexcept { return BUS_PROTOCOL_MIL1553B; }
ProtocolDetectionReport Mil1553BDetector::probe(const ProtocolDetectionContext& context, std::span<const RawFrame> frames) const {
    ProtocolDetectionReport report;
    if (context.input.capture_representation != mil1553b::capture_representation) {
        report.status = ProtocolDetectionStatus::Rejected; report.reason = "unknown 1553B capture representation"; return report;
    }
    if (frames.empty()) { report.status = ProtocolDetectionStatus::NeedMoreData; return report; }
    for (const auto& frame : frames) {
        if (!(protocolStreamOf(frame) == context.stream)) {
            report.status = ProtocolDetectionStatus::Rejected; report.reason = "mixed protocol streams"; return report;
        }
    }
    const auto& payload = frames.back().payload;
    if (payload.size() < mil1553b::capture_magic.size() ||
        !std::equal(mil1553b::capture_magic.begin(), mil1553b::capture_magic.end(), payload.begin())) {
        report.reason = "1553B monitor envelope signature absent"; return report;
    }
    if (context.input.hint_is_authoritative && context.input.source_hint != BUS_PROTOCOL_MIL1553B) {
        report.status = ProtocolDetectionStatus::Rejected; report.reason = "authoritative source hint conflicts with 1553B"; return report;
    }
    report.status = ProtocolDetectionStatus::Identified;
    report.basis = context.input.hint_is_authoritative ? ProtocolSelectionBasis::SourceDescriptor : ProtocolSelectionBasis::ContentEvidence;
    report.selected = BUS_PROTOCOL_MIL1553B;
    report.candidates.push_back({BUS_PROTOCOL_MIL1553B, CandidateStrength::Strong,
        {{ProtocolEvidenceKind::CaptureFormat, mil1553b::capture_representation},
         {ProtocolEvidenceKind::Structure, "project monitor envelope signature; not identification from wire bytes"}}});
    return report;
}
ProtocolParseResult Mil1553BHandler::parse(const RawFrame& frame) {
    using namespace mil1553b;
    if (!(protocolStreamOf(frame) == key_)) return malformed("frame belongs to another source/channel/generation");
    const auto& bytes = frame.payload;
    if (bytes.size() < 12 || !std::equal(capture_magic.begin(), capture_magic.end(), bytes.begin()))
        return malformed("invalid 1553B monitor envelope header");
    const std::size_t count = bytes[9];
    if (bytes[8] > 1 || count == 0 || count > 36 || bytes[10] || bytes[11] || bytes.size() != 12 + 4 * count)
        return malformed("invalid bus, word count, reserved bytes or envelope length");
    std::vector<CapturedWord> words;
    words.reserve(count);
    for (std::size_t i = 0; i < count; ++i) {
        const auto at = 12 + i * 4;
        const auto value = static_cast<std::uint16_t>(std::uint32_t(bytes[at]) | (std::uint32_t(bytes[at + 1]) << 8));
        if (bytes[at + 2] > 1) return malformed("invalid sync kind at word " + std::to_string(i));
        if (bytes[at + 3] > 1 || ((std::popcount(value) + bytes[at + 3]) & 1) != 1)
            return malformed("odd parity failure at word " + std::to_string(i));
        words.push_back({value, static_cast<Sync>(bytes[at + 2]), bytes[at + 3]});
    }
    if (words[0].sync != Sync::CommandStatus) return malformed("first word must have command/status sync");
    const auto first = decode1553BCommand(words[0].value);
    if (first.isMode()) return unsupported("mode commands are outside the initial software subset");
    if (first.transmit && first.rt_address == 31) return malformed("broadcast address cannot transmit data");
    const bool broadcast = first.rt_address == 31;
    const bool rt_to_rt = !first.transmit && count > 1 && words[1].sync == Sync::CommandStatus;
    std::size_t data_begin{}, data_count = first.wordCount(), expected{};
    std::vector<std::pair<std::size_t, std::uint32_t>> statuses;
    ProtocolMessage message; message.stream = key_; message.protocol = BUS_PROTOCOL_MIL1553B; message.flags = frame.flags;
    message.metadata.push_back({"bus", std::uint64_t(bytes[8])});
    if (first.transmit) {
        message.message_kind = "rt_to_bc"; message.metadata.push_back({"transmit_command", std::uint64_t(words[0].value)});
        if (count < 2) return malformed("missing RT status");
        statuses.emplace_back(1, first.rt_address);
        if (words[1].value & status_no_data) data_count = 0; // Busy / illegal-command rejection returns status only.
        data_begin = 2; expected = data_count + 2;
    } else if (rt_to_rt) {
        const auto second = decode1553BCommand(words[1].value);
        if (second.isMode()) return malformed("mode command cannot be an RT-to-RT transmit command");
        if (!second.transmit || second.rt_address == 31 || second.rt_address == first.rt_address ||
            second.wordCount() != first.wordCount()) return malformed("invalid RT-to-RT transmit command or mismatched count");
        if (count < 3) return malformed("missing transmitting RT status");
        if (words[2].sync != Sync::CommandStatus || std::uint32_t(words[2].value >> 11) != second.rt_address || (words[2].value & 0x02e0))
            return malformed("invalid transmitting RT status word");
        if (words[2].value & status_no_data) return unsupported("RT-to-RT busy/message-error response is outside the initial software subset");
        message.message_kind = broadcast ? "rt_broadcast" : "rt_to_rt";
        message.metadata.push_back({"receive_command", std::uint64_t(words[0].value)});
        message.metadata.push_back({"transmit_command", std::uint64_t(words[1].value)});
        statuses.emplace_back(2, second.rt_address);
        data_begin = 3; expected = data_count + (broadcast ? 3 : 4);
        if (!broadcast) statuses.emplace_back(data_count + 3, first.rt_address);
    } else {
        message.message_kind = broadcast ? "bc_broadcast" : "bc_to_rt";
        message.metadata.push_back({"receive_command", std::uint64_t(words[0].value)});
        data_begin = 1; expected = data_count + (broadcast ? 1 : 2);
        if (!broadcast) statuses.emplace_back(data_count + 1, first.rt_address);
    }
    if (count != expected) return malformed("message word count mismatch: expected " + std::to_string(expected) + ", received " + std::to_string(count));
    for (const auto& [index, rt] : statuses) {
        const auto& status = words[index];
        if (status.sync != Sync::CommandStatus || std::uint32_t(status.value >> 11) != rt || (status.value & 0x02e0))
            return malformed("invalid status word/address/reserved bits at word " + std::to_string(index));
        message.metadata.push_back({"status_word_" + std::to_string(index), std::uint64_t(status.value)});
        if (status.value & status_bad_data) message.flags |= BUS_FRAME_INVALID;
    }
    for (std::size_t i = 0; i < data_count; ++i) {
        const auto& word = words[data_begin + i];
        if (word.sync != Sync::Data) return malformed("data word has command/status sync at word " + std::to_string(data_begin + i));
        message.payload.push_back(static_cast<std::uint8_t>(word.value >> 8));
        message.payload.push_back(static_cast<std::uint8_t>(word.value));
    }
    message.metadata.push_back({"word_count", std::uint64_t(data_count)});
    message.evidence.push_back({key_, frame.record_index, frame.sequence, frame.capture_time_ns, frame.clock_domain,
        frame.ingest_time_ns, frame.origin_record_index, frame.recording_session_id, frame.origin_session_id});
    return {ProtocolParseStatus::Complete, {std::move(message)}, {}};
}
std::vector<ProtocolDescriptor> Mil1553BFactory::descriptors() const {
    return {{BUS_PROTOCOL_MIL1553B, "MIL-STD-1553B monitor software subset", "1553b-monitor-v1", {mil1553b::capture_representation}}};
}
std::unique_ptr<IProtocolDetector> Mil1553BFactory::createDetector(ProtocolId protocol) const {
    if (protocol == BUS_PROTOCOL_MIL1553B) return std::make_unique<Mil1553BDetector>();
    return {};
}
std::unique_ptr<IProtocolHandler> Mil1553BFactory::createHandler(ProtocolId protocol, const ProtocolStreamKey& key) const {
    if (protocol == BUS_PROTOCOL_MIL1553B) return std::make_unique<Mil1553BHandler>(key);
    return {};
}
void Mil1553BRouter::setSelection(const StreamAddress& address, ProtocolSelection next) {
    if (next.protocol() && next.protocol() != BUS_PROTOCOL_MIL1553B) throw std::invalid_argument("unregistered manual protocol");
    if (!selections_.contains(addressKey(address)) && selections_.size() >= limits_.max_streams)
        throw std::invalid_argument("protocol selection budget exceeded");
    const auto previous = selection(address);
    if (previous.mode() != next.mode() || previous.protocol() != next.protocol()) resetStream(address, ProtocolResetReason::SelectionChanged);
    selections_.insert_or_assign(addressKey(address), next);
}
ProtocolSelection Mil1553BRouter::selection(const StreamAddress& address) const {
    auto found = selections_.find(addressKey(address));
    if (found == selections_.end() && !address.origin_source.empty()) found = selections_.find({address.source, address.channel, ""});
    return found == selections_.end() ? ProtocolSelection{} : found->second;
}
void Mil1553BRouter::setInputDescriptor(const StreamAddress& address, const ProtocolInputDescriptor& input) {
    if (!inputs_.contains(addressKey(address)) && inputs_.size() >= limits_.max_streams)
        throw std::invalid_argument("protocol input descriptor budget exceeded");
    const auto previous = inputDescriptor(address);
    if (previous.source_hint != input.source_hint || previous.hint_is_authoritative != input.hint_is_authoritative ||
        previous.capture_representation != input.capture_representation) resetStream(address, ProtocolResetReason::InputChanged);
    inputs_.insert_or_assign(addressKey(address), input);
}
ProtocolInputDescriptor Mil1553BRouter::inputDescriptor(const StreamAddress& address) const {
    auto found = inputs_.find(addressKey(address));
    if (found == inputs_.end() && !address.origin_source.empty()) found = inputs_.find({address.source, address.channel, ""});
    return found == inputs_.end() ? ProtocolInputDescriptor{} : found->second;
}
void Mil1553BRouter::setLimits(const ProtocolRoutingLimits& limits) {
    if (!limits.max_streams || !limits.max_observation_frames_per_stream || !limits.max_observation_bytes_per_stream || !limits.max_candidates_per_stream)
        throw std::invalid_argument("protocol routing budgets must be positive");
    if (limits.max_streams < limits_.max_streams || limits.max_observation_frames_per_stream < limits_.max_observation_frames_per_stream ||
        limits.max_observation_bytes_per_stream < limits_.max_observation_bytes_per_stream || limits.max_candidates_per_stream < limits_.max_candidates_per_stream)
        handlers_.clear();
    limits_ = limits;
}
void Mil1553BRouter::resetStream(const StreamAddress& address, ProtocolResetReason) {
    for (auto found = handlers_.begin(); found != handlers_.end();) {
        if (std::get<0>(found->first) == address.source && std::get<1>(found->first) == address.channel &&
            (address.origin_source.empty() || std::get<2>(found->first) == address.origin_source)) found = handlers_.erase(found);
        else ++found;
    }
}
void Mil1553BRouter::removeStream(const StreamAddress& address) {
    resetStream(address, ProtocolResetReason::ExplicitReset); selections_.erase(addressKey(address)); inputs_.erase(addressKey(address));
}
ProtocolRouteResult Mil1553BRouter::route(const RawFrame& frame) {
    ProtocolRouteResult result; result.stream = protocolStreamOf(frame);
    const auto& address = result.stream.address; const auto key = addressKey(address);
    const auto selected = selection(address); const auto input = inputDescriptor(address);
    if (frame.payload.size() > limits_.max_observation_bytes_per_stream) {
        result.detection.status = ProtocolDetectionStatus::Rejected; result.detection.reason = "protocol byte budget exceeded"; return result;
    }
    if (selected.mode() == ProtocolSelectionMode::Manual) {
        result.detection.status = ProtocolDetectionStatus::Identified; result.detection.selected = selected.protocol();
        result.detection.basis = ProtocolSelectionBasis::Manual;
    } else {
        result.detection = detector_.probe({result.stream, input}, {&frame, 1});
    }
    if (result.detection.status != ProtocolDetectionStatus::Identified) return result;
    if (input.capture_representation != mil1553b::capture_representation) {
        result.parsing = unsupported("unknown 1553B capture representation"); return result;
    }
    auto found = handlers_.find(key);
    if (found == handlers_.end()) {
        if (handlers_.size() >= limits_.max_streams) {
            result.detection = {}; result.detection.status = ProtocolDetectionStatus::Rejected;
            result.detection.reason = "protocol stream budget exceeded"; return result;
        }
        found = handlers_.emplace(key, factory_.createHandler(*result.detection.selected, result.stream)).first;
    } else if (!(found->second->stream() == result.stream)) found->second = factory_.createHandler(*result.detection.selected, result.stream);
    result.parsing = found->second->parse(frame);
    return result;
}
Mil1553BMessageDecoder::Mil1553BMessageDecoder(const BackendConfiguration& config) {
    std::map<SelectorKey, BackendConfiguration> grouped;
    for (const auto& field : config.fields) {
        if (!field.mil1553b) continue;
        const auto& selector = *field.mil1553b;
        const auto key = SelectorKey{field.channel, selector.rt_address, selector.subaddress, selector.transmit};
        auto [found, inserted] = grouped.try_emplace(key);
        if (inserted) {
            found->second.version = config.version; found->second.document = config.document;
            found->second.recording_session_id = config.recording_session_id;
        }
        found->second.fields.push_back(field);
    }
    for (auto& [key, selected] : grouped) dictionaries_.emplace(key, std::make_unique<DictionaryDecoder>(std::move(selected)));
}
std::vector<ParameterSample> Mil1553BMessageDecoder::decode(const ProtocolMessage& message) {
    const auto& reference = message.evidence.front();
    RawFrame frame;
    frame.source = message.stream.address.source; frame.channel = message.stream.address.channel;
    frame.generation = message.stream.generation; frame.origin_source = message.stream.address.origin_source;
    frame.origin_generation = message.stream.origin_generation; frame.protocol = message.protocol; frame.flags = message.flags;
    frame.payload = message.payload; frame.record_index = reference.record_index; frame.sequence = reference.sequence;
    frame.capture_time_ns = reference.capture_time_ns; frame.clock_domain = reference.clock_domain;
    frame.ingest_time_ns = reference.ingest_time_ns; frame.origin_record_index = reference.origin_record_index;
    frame.recording_session_id = reference.recording_session_id; frame.origin_session_id = reference.origin_session_id;
    std::vector<ParameterSample> samples;
    // Both commands in an RT-to-RT transfer refer to the same captured data words.
    for (const auto& metadata : message.metadata) {
        if (metadata.name != "receive_command" && metadata.name != "transmit_command") continue;
        const auto command = decode1553BCommand(static_cast<std::uint16_t>(std::get<std::uint64_t>(metadata.value)));
        const auto found = dictionaries_.find({frame.channel, command.rt_address, command.subaddress, command.transmit});
        if (found == dictionaries_.end()) continue;
        auto decoded = found->second->decode(frame);
        samples.insert(samples.end(), std::make_move_iterator(decoded.begin()), std::make_move_iterator(decoded.end()));
    }
    return samples;
}
ProtocolPipelineDecoder::ProtocolPipelineDecoder(const BackendConfiguration& config)
    : config_(config), ordinary_(config), messages_(config) {
    for (const auto& input : config.protocol_inputs) {
        router_.setSelection(input.address, input.selection); router_.setInputDescriptor(input.address, input.input);
        inputs_.emplace(std::pair{input.address.source, input.address.channel}, input);
    }
}
std::vector<ParameterSample> ProtocolPipelineDecoder::decode(const RawFrame& frame) {
    const auto address = protocolStreamOf(frame).address;
    const auto configured = inputs_.find({address.source, address.channel});
    if (configured == inputs_.end() && frame.protocol != BUS_PROTOCOL_MIL1553B) return ordinary_.decode(frame);
    const auto result = router_.route(frame);
    ParameterSample diagnostic;
    diagnostic.parameter_id = "mil1553b.protocol_status.ch" + std::to_string(frame.channel);
    diagnostic.source = frame.source; diagnostic.generation = frame.generation; diagnostic.origin_generation = frame.origin_generation;
    diagnostic.capture_time_ns = frame.capture_time_ns; diagnostic.ingest_time_ns = frame.ingest_time_ns;
    diagnostic.raw_record_index = frame.record_index; diagnostic.sequence = frame.sequence; diagnostic.clock_domain = frame.clock_domain;
    diagnostic.max_age_ns = 1000000000; diagnostic.unit = "status"; diagnostic.decoder_version = config_.version;
    diagnostic.recording_session_id = frame.recording_session_id ? frame.recording_session_id : config_.recording_session_id;
    diagnostic.origin_session_id = frame.origin_session_id; diagnostic.origin_source = frame.origin_source; diagnostic.origin_record_index = frame.origin_record_index;
    if (config_.document) diagnostic.configuration_sha256 = config_.document->sha256;
    std::int64_t code{}; std::string label;
    if (result.detection.status != ProtocolDetectionStatus::Identified) { code = 3; label = "unresolved: " + result.detection.reason; }
    else if (result.parsing.status != ProtocolParseStatus::Complete) {
        code = result.parsing.status == ProtocolParseStatus::Unsupported ? 2 : 1;
        label = (code == 2 ? "unsupported: " : "malformed: ") + result.parsing.diagnostics.front();
    } else {
        const auto& message = result.parsing.messages.front();
        code = (message.flags & BUS_FRAME_INVALID) ? 4 : 0;
        label = (code == 4 ? "invalid_capture_or_rt_status: " : "complete: ") + message.message_kind;
        for (const auto& metadata : message.metadata) {
            if (metadata.name == "bus") label += std::get<std::uint64_t>(metadata.value) == 0 ? ":bus_a" : ":bus_b";
            if (metadata.name.starts_with("status_word_")) label += ":" + metadata.name + "=" + std::to_string(std::get<std::uint64_t>(metadata.value));
        }
    }
    diagnostic.valid = code == 0; // Invalid input diagnostics cannot advance business event-time watermarks.
    diagnostic.value = EnumValue{code, std::move(label)};
    std::vector<ParameterSample> samples{std::move(diagnostic)};
    if (result.parsing.status != ProtocolParseStatus::Complete) {
        // With an untrusted command word the affected endpoint is unknown. Mark
        // this input's configured parameters invalid so no rule bridges the gap.
        auto invalid = frame; invalid.protocol = BUS_PROTOCOL_MIL1553B; invalid.flags |= BUS_FRAME_INVALID; invalid.payload.clear();
        auto decoded = ordinary_.decode(invalid);
        samples.insert(samples.end(), std::make_move_iterator(decoded.begin()), std::make_move_iterator(decoded.end()));
    }
    for (const auto& message : result.parsing.messages) {
        auto decoded = messages_.decode(message);
        samples.insert(samples.end(), std::make_move_iterator(decoded.begin()), std::make_move_iterator(decoded.end()));
    }
    return samples;
}
}
