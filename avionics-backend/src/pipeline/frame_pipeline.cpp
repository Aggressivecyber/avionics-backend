#include "core/frame_pipeline.hpp"
#include "plugin_api/bus_plugin.h"
#include <algorithm>
#include <stdexcept>

namespace avionics {
namespace {
std::size_t frameMemoryBytes(const RawFrame& frame) {
    return sizeof(RawFrame) + frame.source.capacity() + frame.origin_source.capacity() + frame.payload.capacity();
}
}
FramePipeline::FramePipeline(std::size_t capacity, std::unique_ptr<IFrameRecorder> recorder,
    std::unique_ptr<IParameterDecoder> decoder, std::shared_ptr<IParameterConsumer> consumer)
    : FramePipeline(PipelineLimits{capacity, capacity, capacity}, std::move(recorder), std::move(decoder), std::move(consumer)) {}
FramePipeline::FramePipeline(PipelineLimits limits, std::unique_ptr<IFrameRecorder> recorder,
    std::unique_ptr<IParameterDecoder> decoder, std::shared_ptr<IParameterConsumer> consumer)
    : limits_(limits), recorder_(std::move(recorder)), decoder_(std::move(decoder)), consumer_(std::move(consumer)) {
    limits_.validate();
    if (!recorder_ || !decoder_ || !consumer_) throw std::invalid_argument("invalid pipeline dependencies");
    analysis_worker_ = std::thread(&FramePipeline::analyze, this);
    try { worker_ = std::thread(&FramePipeline::run, this); }
    catch (...) {
        { std::lock_guard lock(mutex_); analysis_closing_ = true; }
        analysis_available_.notify_all(); analysis_worker_.join(); throw;
    }
}
FramePipeline::~FramePipeline() { shutdown(); }
void FramePipeline::reserveSource(const std::string& id) {
    if (id.empty() || id.size() > 128) throw std::invalid_argument("invalid source id");
    std::lock_guard lock(mutex_);
    if (!stats_.sources.contains(id) && stats_.sources.size() >= limits_.max_sources)
        throw std::invalid_argument("source state capacity exceeded; start a new session or increase max_sources");
    stats_.sources.try_emplace(id); queued_by_source_.try_emplace(id);
}
bool FramePipeline::submit(RawFrame f) {
    if (f.source.empty() || f.source.size() > 128 || f.origin_source.size() > 128 || f.payload.size() > BUS_MAX_PAYLOAD)
        throw std::invalid_argument("invalid raw frame");
    std::lock_guard lock(mutex_);
    if (!stats_.sources.contains(f.source) && stats_.sources.size() >= limits_.max_sources) {
        ++stats_.dropped; ++stats_.rejections.source_capacity; return false;
    }
    auto& source = stats_.sources[f.source]; auto& used = queued_by_source_[f.source];
    const auto bytes = frameMemoryBytes(f);
    std::uint64_t* rejection = nullptr;
    if (!stats_.healthy) rejection = &stats_.rejections.storage_fault;
    else if (closing_) rejection = &stats_.rejections.closed;
    else if (queue_.size() >= limits_.queue_frames) rejection = &stats_.rejections.queue_frames;
    else if (bytes > limits_.queue_bytes - queued_bytes_) rejection = &stats_.rejections.queue_bytes;
    else if (used.frames >= limits_.source_frames) rejection = &stats_.rejections.source_frames;
    else if (bytes > limits_.source_bytes - used.bytes) rejection = &stats_.rejections.source_bytes;
    if (rejection) { ++*rejection; ++stats_.dropped; ++source.dropped; return false; }
    f.record_index = next_record_;
    queue_.push_back(std::move(f));
    ++next_record_; ++stats_.accepted; ++source.accepted; ++used.frames;
    used.bytes += bytes; queued_bytes_ += bytes;
    stats_.high_water_bytes = std::max(stats_.high_water_bytes, queued_bytes_);
    available_.notify_one();
    return true;
}
bool FramePipeline::drainSource(const std::string& id, std::chrono::milliseconds timeout) {
    std::unique_lock lock(mutex_);
    const auto found = stats_.sources.find(id);
    if (found == stats_.sources.end()) throw std::invalid_argument("unknown source drain: " + id);
    const auto target = found->second.accepted;
    return completed_.wait_for(lock, timeout, [&] {
        return !stats_.healthy || stats_.sources.at(id).recorded >= target;
    }) && stats_.healthy && stats_.sources.at(id).recorded >= target;
}
PipelineStats FramePipeline::stats() const {
    std::lock_guard lock(mutex_); auto out = stats_;
    out.queued_frames = queue_.size(); out.queued_bytes = queued_bytes_;
    out.analysis_queued_frames = analysis_queue_.size(); out.analysis_queued_bytes = analysis_bytes_;
    return out;
}
void FramePipeline::shutdown() {
    std::lock_guard shutdown_lock(shutdown_mutex_);
    { std::lock_guard lock(mutex_); closing_ = true; }
    available_.notify_all();
    if (worker_.joinable()) worker_.join();
    { std::lock_guard lock(mutex_); analysis_closing_ = true; }
    analysis_available_.notify_all();
    if (analysis_worker_.joinable()) analysis_worker_.join();
}
void FramePipeline::fail(const std::string& message) {
    std::lock_guard lock(mutex_);
    stats_.healthy = false; stats_.failure = message; closing_ = true;
    completed_.notify_all();
}
void FramePipeline::failAnalysis(const std::string& message, std::optional<std::uint64_t> record) {
    // Caller holds mutex_. Recording remains independently healthy.
    if (stats_.analysis_healthy) { stats_.analysis_failure = message; stats_.analysis_healthy = false; }
    if (record && (!stats_.analysis_first_missing || *record < *stats_.analysis_first_missing)) stats_.analysis_first_missing = record;
    if (!analysis_queue_.empty() && (!stats_.analysis_first_missing || analysis_queue_.front().record_index < *stats_.analysis_first_missing))
        stats_.analysis_first_missing = analysis_queue_.front().record_index;
    stats_.analysis_skipped += analysis_queue_.size();
    analysis_queue_.clear(); analysis_bytes_ = 0; analysis_available_.notify_all();
}
void FramePipeline::run() noexcept {
    try {
        auto flushed = std::chrono::steady_clock::now();
        while (true) {
            RawFrame frame;
            {
                std::unique_lock lock(mutex_);
                available_.wait_until(lock, flushed + std::chrono::seconds(1), [&] { return closing_ || !queue_.empty(); });
                if (queue_.empty()) {
                    if (closing_) break;
                    lock.unlock(); recorder_->flush(); flushed = std::chrono::steady_clock::now(); continue;
                }
                frame = std::move(queue_.front()); queue_.pop_front();
                const auto bytes = frameMemoryBytes(frame);
                queued_bytes_ -= bytes; auto& used = queued_by_source_.at(frame.source); --used.frames; used.bytes -= bytes;
            }
            recorder_->write(frame); // always record before decoding.
            if (std::chrono::steady_clock::now() - flushed >= std::chrono::seconds(1)) {
                recorder_->flush(); flushed = std::chrono::steady_clock::now();
            }
            {
                std::lock_guard lock(mutex_);
                ++stats_.recorded; ++stats_.sources.at(frame.source).recorded; completed_.notify_all();
                if (!stats_.analysis_healthy) { ++stats_.analysis_skipped; continue; }
                const auto bytes = frameMemoryBytes(frame);
                if (analysis_queue_.size() >= limits_.analysis_frames || bytes > limits_.analysis_bytes - analysis_bytes_) {
                    failAnalysis("analysis queue budget exceeded; replay the raw archive", frame.record_index);
                    ++stats_.analysis_skipped; continue;
                }
                analysis_queue_.push_back(std::move(frame)); analysis_bytes_ += bytes; analysis_available_.notify_one();
            }
        }
        recorder_->finish(); // seals a successful archive; no power-loss durability claim.
    } catch (const std::exception& e) { fail(e.what()); }
      catch (...) { fail("unknown recording failure"); }
}
void FramePipeline::analyze() noexcept {
    std::optional<std::uint64_t> current_record;
    try {
        while (true) {
            RawFrame frame;
            {
                std::unique_lock lock(mutex_);
                analysis_available_.wait(lock, [&] { return analysis_closing_ || !stats_.analysis_healthy || !analysis_queue_.empty(); });
                if (!stats_.analysis_healthy || (analysis_closing_ && analysis_queue_.empty())) break;
                frame = std::move(analysis_queue_.front()); analysis_queue_.pop_front();
                analysis_bytes_ -= frameMemoryBytes(frame);
            }
            current_record = frame.record_index;
            std::vector<ParameterSample> samples; bool malformed = false;
            try { samples = decoder_->decode(frame); }
            catch (const std::invalid_argument&) { malformed = true; }
            for (const auto& sample : samples) consumer_->consume(sample);
            {
                std::lock_guard lock(mutex_);
                if (malformed) ++stats_.decode_errors; else if (samples.empty()) ++stats_.unsupported;
                stats_.samples += samples.size(); ++stats_.completed; ++stats_.sources.at(frame.source).completed;
                stats_.analysis_cursor = frame.record_index; current_record.reset();
            }
        }
        if (stats().analysis_healthy) consumer_->finish();
    } catch (const std::exception& error) {
        std::lock_guard lock(mutex_); if (current_record) ++stats_.analysis_skipped; failAnalysis(error.what(), current_record);
    } catch (...) {
        std::lock_guard lock(mutex_); if (current_record) ++stats_.analysis_skipped; failAnalysis("unknown analysis failure", current_record);
    }
}
}
