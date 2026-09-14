#include "plugin_api/plugin_support.hpp"
#include "core/archive.hpp"
#include "core/path.hpp"
#include <memory>

namespace {
using namespace avionics::plugin;
class Replay final : public Worker {
public:
    using Worker::Worker;
    ~Replay() override { stop(); }
    void configure(const char* text) override {
        prepareConfigure();
        auto options = parse(text);
        auto path = take(options, "path", "");
        const auto speed = integer(options, "speed", 1, 0, 1000);
        noUnknown(options);
        if (path.empty()) throw std::invalid_argument("replay requires path");
        avionics::ArchiveReader validate(avionics::pathFromUtf8(path));
        path_ = std::move(path); speed_ = static_cast<std::uint64_t>(speed); configured_ = true;
    }
private:
    void beforeStart() override { reader_ = std::make_unique<avionics::ArchiveReader>(avionics::pathFromUtf8(path_)); }
    void run() override {
        avionics::RawFrame recorded;
        std::uint64_t first_ingest{};
        bool first = true;
        const auto begin = std::chrono::steady_clock::now();
        while (!stop_requested_ && reader_->next(recorded)) {
            if (first) { first_ingest = recorded.ingest_time_ns; first = false; }
            if (speed_ && recorded.ingest_time_ns >= first_ingest) {
                const auto delay = (recorded.ingest_time_ns - first_ingest) / speed_;
                // Bound corrupt/unreasonable timelines and avoid duration overflow.
                constexpr std::uint64_t max_delay = 7ull * 24 * 60 * 60 * 1000000000;
                if (delay > max_delay) throw std::runtime_error("replay timeline exceeds seven days");
                if (waitUntil(begin + std::chrono::nanoseconds(delay))) break;
            }
            if (stop_requested_) break;
            BusFrameView frame{};
            frame.struct_size = sizeof(frame); frame.protocol = recorded.protocol;
            frame.channel = recorded.channel; frame.flags = recorded.flags; frame.clock_domain = BUS_CLOCK_RECORDED;
            frame.capture_time_ns = recorded.capture_time_ns; frame.sequence = recorded.sequence;
            frame.payload_size = static_cast<std::uint32_t>(recorded.payload.size()); frame.payload = recorded.payload.data();
            // Preserve ultimate source across replay-of-replay operations.
            const bool has_origin = !recorded.origin_source.empty();
            frame.origin_source = has_origin ? recorded.origin_source.c_str() : recorded.source.c_str();
            frame.origin_generation = has_origin ? recorded.origin_generation : recorded.generation;
            frame.origin_record_index = has_origin ? recorded.origin_record_index : recorded.record_index;
            emit(frame);
        }
    }
    std::string path_;
    std::uint64_t speed_{1};
    std::unique_ptr<avionics::ArchiveReader> reader_;
};
}
extern "C" BUS_EXPORT std::int32_t BUS_CALL bus_plugin_query(std::uint32_t abi, BusPluginApi* output) {
    return Glue<Replay>::query(abi, output, "replay-v1");
}
