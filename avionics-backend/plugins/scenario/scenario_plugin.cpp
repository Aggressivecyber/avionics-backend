#include "plugin_api/plugin_support.hpp"
#include <array>
#include <bit>

namespace {
using namespace avionics::plugin;
class Scenario final : public Worker {
public:
    using Worker::Worker;
    ~Scenario() override { stop(); }
    void configure(const char* text) override {
        prepareConfigure();
        auto options = parse(text);
        kind_ = take(options, "scenario", "normal");
        if (kind_ != "normal" && kind_ != "limit" && kind_ != "timeout")
            throw std::invalid_argument("unknown scenario");
        period_ = integer(options, "period_ms", 20, 1, 1000);
        duration_ = integer(options, "duration_ms", 4000, 100, 600000);
        trigger_ = integer(options, "trigger_ms", 1000, 1, 599999);
        delay_ = integer(options, "response_ms", 100, 1, 599999);
        baseline_ = integer(options, "baseline_milli", 20000, -1000000, 1000000);
        high_ = integer(options, "high_milli", 35000, -1000000, 1000000);
        noUnknown(options);
        if (duration_ % period_ || trigger_ % period_ || delay_ % period_ ||
            trigger_ + delay_ >= duration_ || duration_ / period_ > 20000)
            throw std::invalid_argument("scenario times must be sample-aligned and finish within 20001 samples");
        configured_ = true;
    }
private:
    void run() override {
        const auto started = std::chrono::steady_clock::now();
        for (std::int64_t ms = 0; ms <= duration_; ms += period_) {
            if (waitUntil(started + std::chrono::milliseconds(ms))) break;
            const std::array<std::int64_t, 3> values{
                kind_ == "limit" && ms >= trigger_ ? high_ : baseline_,
                ms >= trigger_ ? 1000 : 0,
                ms >= trigger_ + delay_ ? 1000 : 0};
            std::array<std::uint8_t, 24> bytes{};
            for (std::size_t field = 0; field < values.size(); ++field) {
                const auto bits = std::bit_cast<std::uint64_t>(values[field]);
                for (std::size_t byte = 0; byte < 8; ++byte)
                    bytes[field * 8 + byte] = static_cast<std::uint8_t>(bits >> (byte * 8));
            }
            BusFrameView frame{};
            frame.struct_size = sizeof(frame); frame.protocol = BUS_PROTOCOL_SIMULATED;
            // Recorded/synthetic time is explicit; it never claims to be the host or a real device clock.
            frame.clock_domain = BUS_CLOCK_RECORDED;
            frame.capture_time_ns = 1000000000ull + static_cast<std::uint64_t>(ms) * 1000000ull;
            frame.sequence = static_cast<std::uint64_t>(ms / period_);
            frame.payload = bytes.data(); frame.payload_size = static_cast<std::uint32_t>(bytes.size());
            emit(frame);
        }
    }
    std::string kind_;
    std::int64_t period_{}, duration_{}, trigger_{}, delay_{}, baseline_{}, high_{};
};
}
extern "C" BUS_EXPORT std::int32_t BUS_CALL bus_plugin_query(std::uint32_t abi, BusPluginApi* output) {
    return Glue<Scenario>::query(abi, output, "scenario-v1");
}
