#pragma once
#include "core/configuration.hpp"
#include "core/protocol_interfaces.hpp"
#include <tuple>

namespace avionics {
struct Mil1553BCommand {
    std::uint32_t rt_address{}, subaddress{}, count_or_mode{};
    bool transmit{};
    bool isMode() const noexcept { return subaddress == 0 || subaddress == 31; }
    std::uint32_t wordCount() const noexcept { return count_or_mode == 0 ? 32 : count_or_mode; }
};
Mil1553BCommand decode1553BCommand(std::uint16_t) noexcept;

class Mil1553BDetector final : public IProtocolDetector {
public:
    ProtocolId protocolId() const noexcept override;
    ProtocolDetectionReport probe(const ProtocolDetectionContext&, std::span<const RawFrame>) const override;
};
class Mil1553BHandler final : public IProtocolHandler {
public:
    explicit Mil1553BHandler(ProtocolStreamKey key) : key_(std::move(key)) {}
    ProtocolId protocolId() const noexcept override;
    const ProtocolStreamKey& stream() const noexcept override { return key_; }
    ProtocolParseResult parse(const RawFrame&) override;
    void reset(ProtocolResetReason) noexcept override {} // Each envelope is a complete captured transaction.
private:
    ProtocolStreamKey key_;
};
class Mil1553BFactory final : public IProtocolFactory {
public:
    std::vector<ProtocolDescriptor> descriptors() const override;
    std::unique_ptr<IProtocolDetector> createDetector(ProtocolId) const override;
    std::unique_ptr<IProtocolHandler> createHandler(ProtocolId, const ProtocolStreamKey&) const override;
};
class Mil1553BRouter final : public IProtocolRouter {
public:
    void setSelection(const StreamAddress&, ProtocolSelection) override;
    ProtocolSelection selection(const StreamAddress&) const override;
    void setInputDescriptor(const StreamAddress&, const ProtocolInputDescriptor&) override;
    ProtocolInputDescriptor inputDescriptor(const StreamAddress&) const override;
    void setLimits(const ProtocolRoutingLimits&) override;
    ProtocolRouteResult route(const RawFrame&) override;
    void resetStream(const StreamAddress&, ProtocolResetReason) override;
    void removeStream(const StreamAddress&) override;
private:
    using Address = std::tuple<std::string, std::uint32_t, std::string>;
    Mil1553BFactory factory_;
    Mil1553BDetector detector_;
    ProtocolRoutingLimits limits_;
    std::map<Address, ProtocolSelection> selections_;
    std::map<Address, ProtocolInputDescriptor> inputs_;
    std::map<Address, std::unique_ptr<IProtocolHandler>> handlers_;
};
class Mil1553BMessageDecoder final : public IProtocolMessageDecoder {
public:
    explicit Mil1553BMessageDecoder(const BackendConfiguration&);
    std::vector<ParameterSample> decode(const ProtocolMessage&) override;
private:
    using SelectorKey = std::tuple<std::uint32_t, std::uint32_t, std::uint32_t, bool>;
    std::map<SelectorKey, std::unique_ptr<DictionaryDecoder>> dictionaries_;
};
class ProtocolPipelineDecoder final : public IProtocolPipelineDecoder {
public:
    explicit ProtocolPipelineDecoder(const BackendConfiguration&);
    IProtocolRouter& router() noexcept override { return router_; }
    IProtocolMessageDecoder& messageDecoder() noexcept override { return messages_; }
    std::vector<ParameterSample> decode(const RawFrame&) override;
private:
    BackendConfiguration config_;
    DictionaryDecoder ordinary_;
    Mil1553BRouter router_;
    Mil1553BMessageDecoder messages_;
    std::map<std::pair<std::string, std::uint32_t>, ProtocolInputDefinition> inputs_;
};
}
