#include "plugin_api/plugin_support.hpp"
#include "plugin_api/mil1553b_capture.hpp"

namespace {
using namespace avionics::plugin;
using namespace avionics::mil1553b;
class Mil1553BSimulated final : public Worker {
public:
    using Worker::Worker;
    ~Mil1553BSimulated() override { stop(); }
    void configure(const char* text) override {
        prepareConfigure();
        auto options = parse(text);
        period_ms_ = integer(options, "period_ms", 5, 1, 60000);
        channel_ = static_cast<std::uint32_t>(integer(options, "channel", 0, 0, 65535));
        fault_every_ = static_cast<std::uint64_t>(integer(options, "fault_every", 0, 0, 1000000));
        noUnknown(options); configured_ = true;
    }
private:
    void run() override {
        std::uint64_t sequence{};
        while (!stop_requested_) {
            const auto cycle = sequence / 4;
            const auto command = static_cast<std::uint16_t>(cycle % 8 >= 4 ? 1000 : 0);
            const auto temperature = static_cast<std::uint16_t>(2000 + cycle % 50);
            std::vector<CapturedWord> words;
            switch (sequence % 4) {
            case 0: words = {syntheticWord(0x1822, Sync::CommandStatus), syntheticWord(command, Sync::Data),
                syntheticWord(temperature, Sync::Data), syntheticWord(0x1800, Sync::CommandStatus)}; break;
            case 1: words = {syntheticWord(0x2442, Sync::CommandStatus), syntheticWord(0x2000, Sync::CommandStatus),
                syntheticWord(command, Sync::Data), syntheticWord(temperature, Sync::Data)}; break;
            case 2: words = {syntheticWord(0x2861, Sync::CommandStatus), syntheticWord(0x2461, Sync::CommandStatus),
                syntheticWord(0x2000, Sync::CommandStatus), syntheticWord(static_cast<std::uint16_t>(temperature + 10), Sync::Data),
                syntheticWord(0x2800, Sync::CommandStatus)}; break;
            case 3: words = {syntheticWord(0xf881, Sync::CommandStatus), syntheticWord(static_cast<std::uint16_t>(cycle % 65536), Sync::Data)}; break;
            }
            auto bytes = encodeCapture(static_cast<std::uint8_t>(cycle % 2), words);
            if (fault_every_ && (sequence + 1) % fault_every_ == 0) bytes[15] ^= 1; // Deliberate captured parity error.
            BusFrameView frame{};
            frame.struct_size = sizeof(frame); frame.protocol = BUS_PROTOCOL_MIL1553B; frame.channel = channel_;
            frame.clock_domain = BUS_CLOCK_HOST_MONOTONIC; frame.capture_time_ns = host_.monotonic_now_ns(host_.context);
            frame.sequence = sequence++; frame.payload = bytes.data(); frame.payload_size = static_cast<std::uint32_t>(bytes.size());
            emit(frame);
            if (waitFor(std::chrono::milliseconds(period_ms_))) break;
        }
    }
    std::int64_t period_ms_{5};
    std::uint32_t channel_{};
    std::uint64_t fault_every_{};
};
}
extern "C" BUS_EXPORT std::int32_t BUS_CALL bus_plugin_query(std::uint32_t abi, BusPluginApi* output) {
    return Glue<Mil1553BSimulated>::query(abi, output, "mil1553b-synthetic-monitor-v1");
}
