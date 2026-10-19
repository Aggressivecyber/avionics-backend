#pragma once
#include "core/archive.hpp"
#include "core/parameter_interfaces.hpp"
#include "core/pipeline_limits.hpp"
#include <condition_variable>
#include <deque>
#include <map>
#include <mutex>
#include <optional>
#include <thread>

namespace avionics {
struct SourceCounters { std::uint64_t accepted{}, completed{}, dropped{}, recorded{}; };
struct RejectionCounters {
    std::uint64_t closed{}, storage_fault{}, queue_frames{}, queue_bytes{}, source_frames{}, source_bytes{}, source_capacity{};
};
struct PipelineStats {
    std::uint64_t accepted{}, completed{}, dropped{}, decode_errors{}, unsupported{}, samples{};
    bool healthy{true};
    std::string failure;
    std::uint64_t recorded{}, analysis_skipped{}, analysis_cursor{};
    bool analysis_healthy{true};
    std::string analysis_failure;
    std::optional<std::uint64_t> analysis_first_missing;
    std::size_t queued_frames{}, queued_bytes{}, high_water_bytes{}, analysis_queued_frames{}, analysis_queued_bytes{};
    RejectionCounters rejections;
    std::map<std::string, SourceCounters> sources;
};
class FramePipeline {
public:
    FramePipeline(std::size_t capacity, std::unique_ptr<IFrameRecorder>,
        std::unique_ptr<IParameterDecoder>, std::shared_ptr<IParameterConsumer>);
    FramePipeline(PipelineLimits, std::unique_ptr<IFrameRecorder>,
        std::unique_ptr<IParameterDecoder>, std::shared_ptr<IParameterConsumer>);
    ~FramePipeline();
    FramePipeline(const FramePipeline&) = delete;
    FramePipeline& operator=(const FramePipeline&) = delete;
    bool submit(RawFrame frame);
    void reserveSource(const std::string&);
    bool drainSource(const std::string&, std::chrono::milliseconds timeout);
    PipelineStats stats() const;
    void shutdown(); // callers stop sources first; queued frames are drained.
private:
    void run() noexcept;
    void analyze() noexcept;
    void fail(const std::string&);
    void failAnalysis(const std::string&, std::optional<std::uint64_t> record);
    const PipelineLimits limits_;
    std::unique_ptr<IFrameRecorder> recorder_;
    std::unique_ptr<IParameterDecoder> decoder_;
    std::shared_ptr<IParameterConsumer> consumer_;
    mutable std::mutex mutex_;
    std::mutex shutdown_mutex_;
    std::condition_variable available_, completed_, analysis_available_;
    std::deque<RawFrame> queue_, analysis_queue_;
    struct QueueUse { std::size_t frames{}, bytes{}; };
    std::map<std::string, QueueUse> queued_by_source_;
    std::size_t queued_bytes_{}, analysis_bytes_{};
    PipelineStats stats_;
    bool closing_{}, analysis_closing_{};
    std::uint64_t next_record_{1};
    std::thread worker_, analysis_worker_;
};
}
