#pragma once
#include "core/types.hpp"
#include <cstddef>
#include <optional>
#include <stdexcept>

namespace avionics {
using ProtocolId = std::uint32_t;
inline constexpr ProtocolId unknown_protocol = 0;

// A selection belongs to a logical source/channel and survives a new generation.
// origin_source distinguishes multiplexed original streams during replay.
struct StreamAddress {
    std::string source;
    std::uint32_t channel{};
    std::string origin_source;
    bool operator==(const StreamAddress&) const = default;
};
// Parser/detector state belongs to an epoch, never to a global "current protocol".
struct ProtocolStreamKey {
    StreamAddress address;
    std::uint64_t generation{}, origin_generation{};
    bool operator==(const ProtocolStreamKey&) const = default;
};
inline ProtocolStreamKey protocolStreamOf(const RawFrame& frame) {
    return {{frame.source, frame.channel, frame.origin_source}, frame.generation, frame.origin_generation};
}

enum class ProtocolSelectionMode { Automatic, Manual };
class ProtocolSelection {
public:
    ProtocolSelection() = default; // Automatic is the default for unconfigured streams.
    static ProtocolSelection automatic() { return {}; }
    static ProtocolSelection manual(ProtocolId protocol) {
        if (protocol == unknown_protocol) throw std::invalid_argument("manual selection requires a known protocol id");
        return ProtocolSelection(protocol);
    }
    ProtocolSelectionMode mode() const noexcept { return mode_; }
    std::optional<ProtocolId> protocol() const noexcept { return protocol_; }
private:
    explicit ProtocolSelection(ProtocolId id) : mode_(ProtocolSelectionMode::Manual), protocol_(id) {}
    ProtocolSelectionMode mode_{ProtocolSelectionMode::Automatic};
    std::optional<ProtocolId> protocol_;
};

enum class ProtocolEvidenceKind { SourceDescriptor, CaptureFormat, Structure, Checksum, Sequence, Timing };
struct ProtocolEvidence {
    ProtocolEvidenceKind kind{};
    std::string detail;
};
// Ordinal evidence strength only: this is not a calibrated probability.
enum class CandidateStrength { Tentative, Strong };
struct ProtocolCandidate {
    ProtocolId protocol{};
    CandidateStrength strength{CandidateStrength::Tentative};
    std::vector<ProtocolEvidence> evidence;
};
enum class ProtocolDetectionStatus { Unknown, NeedMoreData, Identified, Ambiguous, Rejected };
enum class ProtocolSelectionBasis { None, Manual, SourceDescriptor, ContentEvidence };
struct ProtocolDetectionReport {
    ProtocolDetectionStatus status{ProtocolDetectionStatus::Unknown};
    ProtocolSelectionBasis basis{ProtocolSelectionBasis::None};
    std::optional<ProtocolId> selected;
    std::vector<ProtocolCandidate> candidates;
    std::string reason;
};
struct ProtocolInputDescriptor {
    std::optional<ProtocolId> source_hint;
    bool hint_is_authoritative{};
    // Identifies the input representation, e.g. captured words versus wire bytes.
    // An empty value does not grant permission to assume a representation.
    std::string capture_representation;
};
struct ProtocolDetectionContext {
    ProtocolStreamKey stream;
    ProtocolInputDescriptor input;
};
struct ProtocolRoutingLimits {
    std::size_t max_streams{256};
    std::size_t max_observation_frames_per_stream{64};
    std::size_t max_observation_bytes_per_stream{4 * 1024 * 1024};
    std::size_t max_candidates_per_stream{16};
};
enum class ProtocolResetReason { NewGeneration, SelectionChanged, InputChanged, Discontinuity, ExplicitReset, Shutdown };

struct ProtocolFrameReference {
    ProtocolStreamKey stream;
    std::uint64_t record_index{}, sequence{}, capture_time_ns{};
    std::uint32_t clock_domain{};
};
using ProtocolMetadataValue = std::variant<std::int64_t, std::uint64_t, bool, std::string>;
struct ProtocolMetadataField {
    std::string name;
    ProtocolMetadataValue value;
};
struct ProtocolMessage {
    ProtocolStreamKey stream;
    ProtocolId protocol{};
    std::string message_kind;
    std::vector<ProtocolMetadataField> metadata;
    // Owned application payload. The original frame remains in the raw archive.
    std::vector<std::uint8_t> payload;
    std::vector<ProtocolFrameReference> evidence;
    std::uint32_t flags{};
};
enum class ProtocolParseStatus { NotAttempted, Complete, NeedMoreData, Malformed, Unsupported };
struct ProtocolParseResult {
    ProtocolParseStatus status{ProtocolParseStatus::NotAttempted};
    std::vector<ProtocolMessage> messages;
    std::vector<std::string> diagnostics;
};
struct ProtocolRouteResult {
    ProtocolStreamKey stream;
    ProtocolDetectionReport detection;
    ProtocolParseResult parsing;
};
struct ProtocolDescriptor {
    ProtocolId id{};
    std::string name, implementation_version;
    std::vector<std::string> capture_representations;
};
}
