#include "core/frame_pipeline.hpp"
#include "plugin_api/bus_plugin.h"
#include <stdexcept>

namespace avionics {
FramePipeline::FramePipeline(std::size_t capacity, std::unique_ptr<IFrameRecorder> recorder,
    std::unique_ptr<IParameterDecoder> decoder, std::shared_ptr<IParameterConsumer> consumer)
    : capacity_(capacity), recorder_(std::move(recorder)), decoder_(std::move(decoder)), consumer_(std::move(consumer)) {
    if (!capacity_ || !recorder_ || !decoder_ || !consumer_) throw std::invalid_argument("invalid pipeline dependencies");
    worker_ = std::thread(&FramePipeline::run, this);
}
FramePipeline::~FramePipeline() { shutdown(); }
bool FramePipeline::submit(RawFrame f) {
    if (f.source.empty() || f.source.size() > 128 || f.origin_source.size() > 128 || f.payload.size() > BUS_MAX_PAYLOAD)
        throw std::invalid_argument("invalid raw frame");
    std::lock_guard lock(mutex_);
    auto& source = stats_.sources[f.source];
    if (closing_ || !stats_.healthy || queue_.size() >= capacity_) {
        ++stats_.dropped; ++source.dropped;
        return false;
    }
    f.record_index = next_record_;
    queue_.push_back(std::move(f));
    ++next_record_; ++stats_.accepted; ++source.accepted;
    available_.notify_one();
    return true;
}
bool FramePipeline::drainSource(const std::string& id, std::chrono::milliseconds timeout) {
    std::unique_lock lock(mutex_);
    const auto target = stats_.sources[id].accepted;
    return completed_.wait_for(lock, timeout, [&] {
        return !stats_.healthy || stats_.sources.at(id).completed >= target;
    }) && stats_.healthy && stats_.sources.at(id).completed >= target;
}
PipelineStats FramePipeline::stats() const { std::lock_guard lock(mutex_); return stats_; }
void FramePipeline::shutdown() {
    std::lock_guard shutdown_lock(shutdown_mutex_);
    { std::lock_guard lock(mutex_); closing_ = true; }
    available_.notify_all();
    if (worker_.joinable()) worker_.join();
}
void FramePipeline::fail(const std::string& message) {
    std::lock_guard lock(mutex_);
    stats_.healthy = false; stats_.failure = message; closing_ = true;
    completed_.notify_all();
}
void FramePipeline::run() noexcept {
    try {
        while (true) {
            RawFrame frame;
            {
                std::unique_lock lock(mutex_);
                available_.wait(lock, [&] { return closing_ || !queue_.empty(); });
                if (queue_.empty()) break;
                frame = std::move(queue_.front()); queue_.pop_front();
            }
            recorder_->write(frame); // always record before decoding.
            std::vector<ParameterSample> samples;
            bool malformed = false;
            try { samples = decoder_->decode(frame); }
            catch (const std::invalid_argument&) { malformed = true; }
            for (const auto& sample : samples) consumer_->consume(sample);
            {
                std::lock_guard lock(mutex_);
                if (malformed) ++stats_.decode_errors;
                else if (samples.empty()) ++stats_.unsupported;
                stats_.samples += samples.size();
                ++stats_.completed; ++stats_.sources.at(frame.source).completed;
                completed_.notify_all();
            }
        }
        recorder_->flush(); // stream flush; does not promise power-loss durability.
    } catch (const std::exception& e) { fail(e.what()); }
      catch (...) { fail("unknown pipeline failure"); }
}
}
