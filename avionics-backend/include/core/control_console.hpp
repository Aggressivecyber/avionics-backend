#pragma once
#include "core/bus_manager.hpp"
#include "core/processing.hpp"
#include <deque>

namespace avionics {
// A lossy, bounded display tap. The file sink remains the complete analysis record.
class MonitorSink final : public IAnalysisSink {
public:
    explicit MonitorSink(std::shared_ptr<IAnalysisSink> file) : file_(std::move(file)) {}
    void sample(const AlignedSample&) override;
    void result(const CorrelationResult&) override;
    void flush() override { file_->flush(); }
    void writeSnapshot(std::ostream&, std::uint64_t after_sample, std::uint64_t after_event) const;
private:
    std::shared_ptr<IAnalysisSink> file_;
    mutable std::mutex mutex_;
    std::uint64_t sample_index_{}, event_index_{};
    std::deque<std::pair<std::uint64_t, AlignedSample>> samples_;
    std::deque<std::pair<std::uint64_t, CorrelationResult>> events_;
};
void runControlConsole(BusManager&, const FramePipeline&, const ProcessingService&, const MonitorSink&,
    const SessionJournal&, const std::filesystem::path&, const BackendConfiguration&);
}
