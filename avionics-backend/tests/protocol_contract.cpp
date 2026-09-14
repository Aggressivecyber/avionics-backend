#include "core/protocol_interfaces.hpp"
#include <iostream>
#include <stdexcept>
#include <type_traits>

using namespace avionics;
static_assert(std::is_abstract_v<IProtocolDetector>);
static_assert(std::is_abstract_v<IProtocolHandler>);
static_assert(std::is_abstract_v<IProtocolFactory>);
static_assert(std::is_abstract_v<IProtocolRouter>);
static_assert(std::is_abstract_v<IProtocolMessageDecoder>);
static_assert(std::is_abstract_v<IProtocolPipelineDecoder>);
static_assert(std::is_base_of_v<IParameterDecoder, IProtocolPipelineDecoder>);
static_assert(std::has_virtual_destructor_v<IProtocolDetector>);
static_assert(std::has_virtual_destructor_v<IProtocolHandler>);
static_assert(std::has_virtual_destructor_v<IProtocolFactory>);
static_assert(std::has_virtual_destructor_v<IProtocolRouter>);
static_assert(std::has_virtual_destructor_v<IProtocolMessageDecoder>);
static_assert(std::has_virtual_destructor_v<IProtocolPipelineDecoder>);

namespace {
void check(bool condition, const char* message) { if (!condition) throw std::runtime_error(message); }
// Test doubles verify that users can derive from the contracts. They are not
// detectors for any real protocol and intentionally never identify input.
class UnresolvedDetector final : public IProtocolDetector {
public:
    ProtocolId protocolId() const noexcept override { return 1000001; }
    ProtocolDetectionReport probe(const ProtocolDetectionContext&, std::span<const RawFrame>) const override { return {}; }
};
class EmptyHandler final : public IProtocolHandler {
public:
    explicit EmptyHandler(ProtocolStreamKey key) : key_(std::move(key)) {}
    ProtocolId protocolId() const noexcept override { return 1000001; }
    const ProtocolStreamKey& stream() const noexcept override { return key_; }
    ProtocolParseResult parse(const RawFrame&) override { return {}; }
    void reset(ProtocolResetReason) noexcept override {}
private:
    ProtocolStreamKey key_;
};
}
int main() {
    try {
        const ProtocolSelection automatic;
        check(automatic.mode() == ProtocolSelectionMode::Automatic && !automatic.protocol(), "default must be automatic");
        const auto manual = ProtocolSelection::manual(1000001);
        check(manual.mode() == ProtocolSelectionMode::Manual && manual.protocol() == 1000001, "manual selection lost protocol");
        bool invalid_rejected = false;
        try { (void)ProtocolSelection::manual(unknown_protocol); } catch (const std::invalid_argument&) { invalid_rejected = true; }
        check(invalid_rejected, "manual unknown protocol must be rejected");
        RawFrame original; original.source = "capture"; original.channel = 2; original.generation = 3;
        auto first = protocolStreamOf(original);
        original.channel = 4; check(!(first == protocolStreamOf(original)), "channels must be isolated");
        original.channel = 2; original.generation = 4;
        const auto restarted = protocolStreamOf(original);
        check(first.address == restarted.address && !(first == restarted), "restart must preserve address and change epoch");
        original.generation = 3; original.origin_source = "replayed"; original.origin_generation = 9;
        check(!(first == protocolStreamOf(original)), "replayed source identity was lost");
        std::unique_ptr<IProtocolDetector> detector = std::make_unique<UnresolvedDetector>();
        const auto unresolved = detector->probe(ProtocolDetectionContext{}, {});
        check(unresolved.status == ProtocolDetectionStatus::Unknown && !unresolved.selected, "unknown must not imply a protocol");
        std::unique_ptr<IProtocolHandler> handler = std::make_unique<EmptyHandler>(first);
        check(handler->stream() == first, "derived handler cannot retain stream identity");
        check(handler->parse(original).status == ProtocolParseStatus::NotAttempted, "empty parsing must not imply success");
        std::cout << "PASS abstract interfaces, existing decoder inheritance, virtual destruction, auto/manual policy and stream identity\n";
        std::cout << "No concrete protocol or recognition algorithm was tested or implemented.\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << "CONTRACT FAILURE: " << e.what() << '\n'; return 1; }
}
