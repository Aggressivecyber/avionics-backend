#include "core/bitfield.hpp"
#include "core/protocol_impl.hpp"
#include "plugin_api/bus_plugin.h"
#include <bit>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <utility>

namespace avionics {
namespace {
constexpr std::size_t word_bytes = 4;
constexpr std::size_t min_observed_words = 4;
constexpr std::uint8_t frame_sync0 = 0xB5;
constexpr std::uint8_t frame_sync1 = 0x00;
constexpr std::size_t frame_header_bytes = 4;
constexpr std::size_t frame_crc_bytes = 2;
constexpr std::size_t max_frame_payload = 4096;

ProtocolFrameReference referenceOf(const ProtocolStreamKey& key, const RawFrame& frame) {
    return {key, frame.record_index, frame.sequence, frame.capture_time_ns, frame.clock_domain};
}

std::uint32_t readWordBigEndian(const std::uint8_t* bytes) {
    return (std::uint32_t(bytes[0]) << 24) | (std::uint32_t(bytes[1]) << 16) |
        (std::uint32_t(bytes[2]) << 8) | std::uint32_t(bytes[3]);
}

bool wordIsStructurallyValid(std::uint32_t word) {
    if (std::popcount(word) % 2 == 0) return false;
    return ((word >> 24) & 0xffu) != 0;
}

std::uint16_t readLittle16(const std::uint8_t* bytes) {
    return std::uint16_t(std::uint16_t(bytes[0]) | (std::uint16_t(bytes[1]) << 8));
}

std::uint16_t crc16Ccitt(const std::uint8_t* data, std::size_t size) {
    std::uint16_t crc = 0xFFFF;
    for (std::size_t i = 0; i < size; ++i) {
        crc ^= std::uint16_t(std::uint16_t(data[i]) << 8);
        for (int bit = 0; bit < 8; ++bit)
            crc = (crc & 0x8000) ? std::uint16_t((crc << 1) ^ 0x1021) : std::uint16_t(crc << 1);
    }
    return crc;
}

class WordDetector final : public IProtocolDetector {
public:
    ProtocolId protocolId() const noexcept override { return protocol_word_stream; }
    ProtocolDetectionReport probe(const ProtocolDetectionContext&, std::span<const RawFrame> observations) const override {
        ProtocolDetectionReport report;
        if (observations.empty()) return report;
        std::uint64_t words = 0;
        for (const auto& frame : observations) {
            if (frame.payload.empty() || frame.payload.size() % word_bytes != 0) return report;
            for (std::size_t offset = 0; offset < frame.payload.size(); offset += word_bytes) {
                const auto word = readWordBigEndian(frame.payload.data() + offset);
                if (!wordIsStructurallyValid(word)) return report;
                ++words;
            }
        }
        ProtocolCandidate candidate;
        candidate.protocol = protocol_word_stream;
        candidate.strength = words >= min_observed_words ? CandidateStrength::Strong : CandidateStrength::Tentative;
        candidate.evidence = {
            {ProtocolEvidenceKind::Structure, "32-bit big-endian words"},
            {ProtocolEvidenceKind::Checksum, "odd parity per word"},
            {ProtocolEvidenceKind::Sequence, "contiguous same-stream window"}};
        report.candidates.push_back(std::move(candidate));
        if (words >= min_observed_words) {
            report.status = ProtocolDetectionStatus::Identified;
            report.basis = ProtocolSelectionBasis::ContentEvidence;
            report.selected = protocol_word_stream;
            report.reason = "parity and label valid across " + std::to_string(words) + " words";
        } else {
            report.status = ProtocolDetectionStatus::NeedMoreData;
            report.reason = "only " + std::to_string(words) + " words observed";
        }
        return report;
    }
};

class FramedDetector final : public IProtocolDetector {
public:
    ProtocolId protocolId() const noexcept override { return protocol_framed_stream; }
    ProtocolDetectionReport probe(const ProtocolDetectionContext&, std::span<const RawFrame> observations) const override {
        ProtocolDetectionReport report;
        std::vector<std::uint8_t> buffer;
        for (const auto& frame : observations) buffer.insert(buffer.end(), frame.payload.begin(), frame.payload.end());
        if (buffer.empty()) return report;
        std::size_t offset = 0;
        std::uint64_t frames = 0;
        bool need_more = false;
        while (offset + frame_header_bytes <= buffer.size()) {
            if (buffer[offset] != frame_sync0 || buffer[offset + 1] != frame_sync1) return report;
            const auto length = readLittle16(buffer.data() + offset + 2);
            if (length > max_frame_payload) return report;
            const auto total = frame_header_bytes + length + frame_crc_bytes;
            if (offset + total > buffer.size()) { need_more = true; break; }
            const auto declared = readLittle16(buffer.data() + offset + frame_header_bytes + length);
            if (declared != crc16Ccitt(buffer.data() + offset + 2, 2 + length)) return report;
            ++frames;
            offset += total;
        }
        if (frames == 0) {
            if (need_more) { report.status = ProtocolDetectionStatus::NeedMoreData; report.reason = "sync matched, frame incomplete"; }
            return report;
        }
        ProtocolCandidate candidate;
        candidate.protocol = protocol_framed_stream;
        candidate.strength = CandidateStrength::Strong;
        candidate.evidence = {
            {ProtocolEvidenceKind::Structure, "sync B5 00 plus little-endian length"},
            {ProtocolEvidenceKind::Checksum, "CRC-16/CCITT-FALSE"},
            {ProtocolEvidenceKind::Sequence, "length-delimited frame order"}};
        report.candidates.push_back(std::move(candidate));
        report.status = ProtocolDetectionStatus::Identified;
        report.basis = ProtocolSelectionBasis::ContentEvidence;
        report.selected = protocol_framed_stream;
        report.reason = "validated " + std::to_string(frames) + " framed record(s)";
        return report;
    }
};

class WordHandler final : public IProtocolHandler {
public:
    explicit WordHandler(ProtocolStreamKey key) : key_(std::move(key)) {}
    ProtocolId protocolId() const noexcept override { return protocol_word_stream; }
    const ProtocolStreamKey& stream() const noexcept override { return key_; }
    ProtocolParseResult parse(const RawFrame& frame) override {
        ProtocolParseResult result;
        if (frame.payload.empty() || frame.payload.size() % word_bytes != 0) {
            result.status = ProtocolParseStatus::Malformed;
            result.diagnostics.push_back("word payload is not a whole number of 32-bit words");
            return result;
        }
        std::uint64_t words = 0;
        std::uint64_t label = 0;
        for (std::size_t offset = 0; offset < frame.payload.size(); offset += word_bytes) {
            const auto word = readWordBigEndian(frame.payload.data() + offset);
            if (!wordIsStructurallyValid(word)) {
                result.status = ProtocolParseStatus::Malformed;
                result.diagnostics.push_back("word parity or label invalid");
                return result;
            }
            if (words == 0) label = (word >> 24) & 0xffu;
            ++words;
        }
        ProtocolMessage message;
        message.stream = key_;
        message.protocol = protocol_word_stream;
        message.message_kind = "word";
        message.payload = frame.payload;
        message.metadata.push_back({"word_count", words});
        message.metadata.push_back({"label", label});
        message.evidence.push_back(referenceOf(key_, frame));
        message.flags = frame.flags;
        result.status = ProtocolParseStatus::Complete;
        result.messages.push_back(std::move(message));
        return result;
    }
    void reset(ProtocolResetReason) noexcept override {}
private:
    ProtocolStreamKey key_;
};

class FramedHandler final : public IProtocolHandler {
public:
    explicit FramedHandler(ProtocolStreamKey key) : key_(std::move(key)) {}
    ProtocolId protocolId() const noexcept override { return protocol_framed_stream; }
    const ProtocolStreamKey& stream() const noexcept override { return key_; }
    ProtocolParseResult parse(const RawFrame& frame) override {
        buffer_.insert(buffer_.end(), frame.payload.begin(), frame.payload.end());
        references_.push_back(referenceOf(key_, frame));
        ProtocolParseResult result;
        std::size_t offset = 0;
        while (offset + frame_header_bytes <= buffer_.size()) {
            if (buffer_[offset] != frame_sync0 || buffer_[offset + 1] != frame_sync1) {
                discard();
                result.status = ProtocolParseStatus::Malformed;
                result.diagnostics.push_back("framed sync mismatch");
                return result;
            }
            const auto length = readLittle16(buffer_.data() + offset + 2);
            if (length > max_frame_payload) {
                discard();
                result.status = ProtocolParseStatus::Malformed;
                result.diagnostics.push_back("framed length exceeds limit");
                return result;
            }
            const auto total = frame_header_bytes + length + frame_crc_bytes;
            if (offset + total > buffer_.size()) break;
            const auto declared = readLittle16(buffer_.data() + offset + frame_header_bytes + length);
            const auto actual = crc16Ccitt(buffer_.data() + offset + 2, 2 + length);
            if (declared != actual) {
                discard();
                result.status = ProtocolParseStatus::Malformed;
                result.diagnostics.push_back("framed CRC mismatch");
                return result;
            }
            ProtocolMessage message;
            message.stream = key_;
            message.protocol = protocol_framed_stream;
            message.message_kind = "frame";
            message.payload.assign(buffer_.begin() + static_cast<std::ptrdiff_t>(offset + frame_header_bytes),
                buffer_.begin() + static_cast<std::ptrdiff_t>(offset + frame_header_bytes + length));
            message.metadata.push_back({"length", std::uint64_t(length)});
            message.metadata.push_back({"crc16", std::uint64_t(actual)});
            message.evidence = references_;
            message.flags = frame.flags;
            result.messages.push_back(std::move(message));
            offset += total;
        }
        if (offset > 0) {
            buffer_.erase(buffer_.begin(), buffer_.begin() + static_cast<std::ptrdiff_t>(offset));
            references_.clear();
        }
        if (!result.messages.empty()) result.status = ProtocolParseStatus::Complete;
        else result.status = ProtocolParseStatus::NeedMoreData;
        return result;
    }
    void reset(ProtocolResetReason) noexcept override { discard(); }
private:
    void discard() { buffer_.clear(); references_.clear(); }
    ProtocolStreamKey key_;
    std::vector<std::uint8_t> buffer_;
    std::vector<ProtocolFrameReference> references_;
};
}

bool BuiltinProtocolFactory::supports(ProtocolId protocol) const noexcept {
    return protocol == protocol_word_stream || protocol == protocol_framed_stream;
}

std::vector<ProtocolDescriptor> BuiltinProtocolFactory::descriptors() const {
    return {
        {protocol_word_stream, "word32-parity", "1", {representation_captured_words}},
        {protocol_framed_stream, "framed-crc16", "1", {representation_wire_bytes}}};
}

std::unique_ptr<IProtocolDetector> BuiltinProtocolFactory::createDetector(ProtocolId protocol) const {
    if (protocol == protocol_word_stream) return std::make_unique<WordDetector>();
    if (protocol == protocol_framed_stream) return std::make_unique<FramedDetector>();
    return nullptr;
}

std::unique_ptr<IProtocolHandler> BuiltinProtocolFactory::createHandler(ProtocolId protocol,
    const ProtocolStreamKey& key) const {
    if (protocol == protocol_word_stream) return std::make_unique<WordHandler>(key);
    if (protocol == protocol_framed_stream) return std::make_unique<FramedHandler>(key);
    return nullptr;
}
}
