#pragma once
#include "core/frame_pipeline.hpp"
#include "core/protocol_types.hpp"
#include <memory>
#include <span>

namespace avionics {
// Pure contracts only. No built-in detector, router, or real protocol is supplied.
class IProtocolDetector {
public:
    virtual ~IProtocolDetector() = default;
    virtual ProtocolId protocolId() const noexcept = 0;
    // Stateless probe over a bounded, same-stream observation window. No parsing
    // side effects; observations are borrowed only for the duration of this call.
    virtual ProtocolDetectionReport probe(const ProtocolDetectionContext&,
        std::span<const RawFrame> observations) const = 0;
};

class IProtocolHandler {
public:
    virtual ~IProtocolHandler() = default;
    virtual ProtocolId protocolId() const noexcept = 0;
    virtual const ProtocolStreamKey& stream() const noexcept = 0;
    // Exactly one stream/epoch owns this stateful handler. Must validate format
    // even after manual selection; malformed input must never switch protocols.
    virtual ProtocolParseResult parse(const RawFrame&) = 0;
    virtual void reset(ProtocolResetReason) noexcept = 0;
};

class IProtocolFactory {
public:
    virtual ~IProtocolFactory() = default;
    virtual std::vector<ProtocolDescriptor> descriptors() const = 0;
    virtual std::unique_ptr<IProtocolDetector> createDetector(ProtocolId) const = 0;
    virtual std::unique_ptr<IProtocolHandler> createHandler(ProtocolId, const ProtocolStreamKey&) const = 0;
};

class IProtocolRouter {
public:
    virtual ~IProtocolRouter() = default;
    // Policy is scoped to an address, not shared by all input channels. Changing
    // it invalidates existing detection and parser state for that address only.
    virtual void setSelection(const StreamAddress&, ProtocolSelection) = 0;
    virtual ProtocolSelection selection(const StreamAddress&) const = 0;
    // Source metadata and capture representation are explicit configuration.
    // A changed descriptor invalidates runtime state for this address only.
    virtual void setInputDescriptor(const StreamAddress&, const ProtocolInputDescriptor&) = 0;
    virtual ProtocolInputDescriptor inputDescriptor(const StreamAddress&) const = 0;
    virtual void setLimits(const ProtocolRoutingLimits&) = 0;
    virtual ProtocolRouteResult route(const RawFrame&) = 0;
    // Reset retains policy and input descriptor; removal also erases both.
    virtual void resetStream(const StreamAddress&, ProtocolResetReason) = 0;
    virtual void removeStream(const StreamAddress&) = 0;
};

class IProtocolMessageDecoder {
public:
    virtual ~IProtocolMessageDecoder() = default;
    // Maps validated protocol messages to engineering parameters using a device
    // dictionary. Protocol recognition does not establish parameter semantics.
    virtual std::vector<ParameterSample> decode(const ProtocolMessage&) = 0;
};

// Direct extension point for the EXISTING FramePipeline and DecoderRegistry.
// Future derived classes compose router + message decoder in their decode().
// This declaration intentionally provides no routing/detection implementation.
class IProtocolPipelineDecoder : public IParameterDecoder {
public:
    ~IProtocolPipelineDecoder() override = default;
    virtual IProtocolRouter& router() noexcept = 0;
    virtual IProtocolMessageDecoder& messageDecoder() noexcept = 0;
    std::vector<ParameterSample> decode(const RawFrame&) override = 0;
};
}
