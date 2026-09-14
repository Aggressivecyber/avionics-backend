#pragma once
#include "core/archive.hpp"
#include <condition_variable>
#include <deque>
#include <map>
#include <mutex>
#include <thread>

namespace avionics {
class IParameterDecoder {
public:
    virtual ~IParameterDecoder() = default;
    virtual std::vector<ParameterSample> decode(const RawFrame&) = 0;
};
class IParameterConsumer {
public:
    virtual ~IParameterConsumer() = default;
    virtual void consume(const ParameterSample&) = 0;
};
struct SourceCounters { std::uint64_t accepted{}, completed{}, dropped{}; };
struct PipelineStats {
    std::uint64_t accepted{}, completed{}, dropped{}, decode_errors{}, unsupported{}, samples{};
    bool healthy{true};
    std::string failure;
    std::map<std::string, SourceCounters> sources;
};
class FramePipeline {
public:
    FramePipeline(std::size_t capacity, std::unique_ptr<IFrameRecorder>,
        std::unique_ptr<IParameterDecoder>, std::shared_ptr<IParameterConsumer>);
    ~FramePipeline();
    FramePipeline(const FramePipeline&) = delete;
    FramePipeline& operator=(const FramePipeline&) = delete;
    bool submit(RawFrame frame);
    bool drainSource(const std::string&, std::chrono::milliseconds timeout);
    PipelineStats stats() const;
    void shutdown(); // callers stop sources first; queued frames are drained.
private:
    void run() noexcept;
    void fail(const std::string&);
    const std::size_t capacity_;
    std::unique_ptr<IFrameRecorder> recorder_;
    std::unique_ptr<IParameterDecoder> decoder_;
    std::shared_ptr<IParameterConsumer> consumer_;
    mutable std::mutex mutex_;
    std::mutex shutdown_mutex_;
    std::condition_variable available_, completed_;
    std::deque<RawFrame> queue_;
    PipelineStats stats_;
    bool closing_{};
    std::uint64_t next_record_{1};
    std::thread worker_;
};
}
