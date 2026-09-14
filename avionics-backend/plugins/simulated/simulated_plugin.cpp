#include "plugin_api/plugin_support.hpp"
#include <array>
#include <bit>
#include <limits>

namespace {
using namespace avionics::plugin;
class Simulated final : public Worker {
public:
    using Worker::Worker;
    ~Simulated() override { stop(); }
    void configure(const char* text) override {
        prepareConfigure();
        auto options = parse(text);
        const auto period = integer(options, "period_ms", 10, 1, 60000);
        const auto initial = integer(options, "initial_milli", 20000, INT32_MIN, INT32_MAX);
        const auto step = integer(options, "step_milli", 100, INT32_MIN, INT32_MAX);
        const auto channel = integer(options, "channel", 0, 0, 65535);
        const auto fail = integer(options, "fail_start", 0, 0, 1);
        noUnknown(options);
        period_ms_ = period; initial_ = initial; step_ = step;
        channel_ = static_cast<std::uint32_t>(channel); fail_start_ = fail != 0; configured_ = true;
    }
private:
    void beforeStart() override { if (fail_start_) throw std::runtime_error("injected start failure"); }
    void run() override {
        std::uint64_t sequence{};
        while (!stop_requested_) {
            const std::int64_t value = initial_ + static_cast<std::int64_t>(sequence % 1000) * step_;
            const auto bits = std::bit_cast<std::uint64_t>(value);
            std::array<std::uint8_t, 8> bytes{};
            for (std::size_t i = 0; i < bytes.size(); ++i) bytes[i] = static_cast<std::uint8_t>(bits >> (8 * i));
            BusFrameView frame{};
            frame.struct_size = sizeof(frame); frame.protocol = BUS_PROTOCOL_SIMULATED;
            frame.channel = channel_; frame.clock_domain = BUS_CLOCK_HOST_MONOTONIC;
            frame.capture_time_ns = host_.monotonic_now_ns(host_.context); frame.sequence = sequence++;
            frame.payload_size = static_cast<std::uint32_t>(bytes.size()); frame.payload = bytes.data();
            emit(frame);
            if (waitFor(std::chrono::milliseconds(period_ms_))) break;
        }
    }
    std::int64_t period_ms_{10}, initial_{20000}, step_{100};
    std::uint32_t channel_{};
    bool fail_start_{};
};
}
extern "C" BUS_EXPORT std::int32_t BUS_CALL bus_plugin_query(std::uint32_t abi, BusPluginApi* output) {
    return Glue<Simulated>::query(abi, output, "simulated-v1");
}
