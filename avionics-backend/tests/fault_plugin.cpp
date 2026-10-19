#include "plugin_api/plugin_support.hpp"
#include <cstdlib>
#include <vector>

namespace {
using namespace avionics::plugin;
class Fault final : public Worker {
public:
    using Worker::Worker;
    void configure(const char* text) override {
        prepareConfigure(); auto options = parse(text); mode_ = take(options, "mode", "exception"); noUnknown(options);
        if (mode_ == "hang_configure") for (;;) std::this_thread::sleep_for(std::chrono::hours(1));
        configured_ = true;
    }
    void start() {
        if (mode_ == "hang_start") for (;;) std::this_thread::sleep_for(std::chrono::hours(1));
        Worker::start();
    }
    void stop() noexcept {
        if (mode_ == "hang_stop") for (;;) std::this_thread::sleep_for(std::chrono::hours(1));
        Worker::stop();
    }
private:
    void run() override {
        if (mode_ == "exception") throw std::runtime_error("injected asynchronous SDK failure");
        if (mode_ == "crash") std::_Exit(77);
        std::vector<std::uint8_t> payload(mode_ == "large_frame" ? BUS_MAX_PAYLOAD : 8, 7);
        BusFrameView frame{}; frame.struct_size = sizeof(frame); frame.protocol = BUS_PROTOCOL_SIMULATED;
        frame.clock_domain = BUS_CLOCK_HOST_MONOTONIC; frame.capture_time_ns = host_.monotonic_now_ns(host_.context);
        frame.payload = payload.data(); frame.payload_size = static_cast<std::uint32_t>(payload.size());
        if (mode_ == "malformed") frame.struct_size = 1;
        emit(frame);
        if (mode_ == "hang_stop") while (!waitFor(std::chrono::hours(1))) {}
    }
    std::string mode_;
};
}
extern "C" BUS_EXPORT std::int32_t BUS_CALL bus_plugin_query(std::uint32_t abi, BusPluginApi* output) {
#if defined(BUS_TEST_HANG_LOAD)
    (void)abi; (void)output; for (;;) std::this_thread::sleep_for(std::chrono::hours(1));
#else
    return Glue<Fault>::query(abi, output, "fault-injection-only");
#endif
}
