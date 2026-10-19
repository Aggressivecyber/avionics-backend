#pragma once
#include "core/configuration.hpp"
#include <deque>
#include <fstream>
#include <memory>
#include <mutex>

namespace avionics {
struct AlignedSample {
    ParameterSample sample;
    std::optional<std::uint64_t> time_ns;
    std::string time_group;
    std::uint64_t uncertainty_ns{};
    std::vector<std::string> quality;
    bool usable() const { return time_ns.has_value() && quality.empty(); }
};
struct EvidenceRef {
    std::string source, parameter, decoder_version;
    std::uint64_t generation{}, origin_generation{}, record_index{}, time_ns{};
};
struct CorrelationResult {
    std::string rule, outcome, time_group;
    std::uint64_t time_ns{};
    std::optional<double> metric;
    std::optional<std::uint64_t> latency_low_ns, latency_high_ns;
    std::vector<EvidenceRef> evidence;
};
struct ProcessingStats {
    std::uint64_t samples{}, usable{}, rejected{}, results{};
    std::size_t buffered{};
    std::map<std::string, std::uint64_t> outcomes;
};
class TimeQualityProcessor {
public:
    explicit TimeQualityProcessor(std::vector<ClockDefinition>, std::size_t capacity = 4096);
    AlignedSample process(const ParameterSample&);
private:
    std::map<std::pair<std::string, std::uint32_t>, ClockDefinition> clocks_;
    std::size_t capacity_;
    struct Last { ParameterSample sample; std::uint64_t time_ns; };
    std::map<std::pair<std::string, std::string>, Last> previous_;
};
class IAnalysisSink {
public:
    virtual ~IAnalysisSink() = default;
    virtual void sample(const AlignedSample&) = 0;
    virtual void result(const CorrelationResult&) = 0;
    virtual void flush() = 0;
};
class JsonlAnalysisSink final : public IAnalysisSink {
public:
    explicit JsonlAnalysisSink(const std::filesystem::path& new_directory);
    void sample(const AlignedSample&) override;
    void result(const CorrelationResult&) override;
    void flush() override;
private:
    std::ofstream parameters_, events_;
};
class ProcessingService final : public IParameterConsumer {
public:
    ProcessingService(BackendConfiguration, std::shared_ptr<IAnalysisSink>);
    void consume(const ParameterSample&) override;
    // The caller must supply a VERIFIED watermark in this mapped clock group.
    // This method does not translate wall time to device time.
    void advance(const std::string& group, std::uint64_t watermark_ns);
    void finish() override; // pending responses at EOF are unresolved, never fabricated timeouts.
    ProcessingStats stats() const;
    std::vector<CorrelationResult> results() const;
private:
    struct PairState { std::optional<AlignedSample> left, right; };
    struct ResponseState {
        std::optional<AlignedSample> command, response, pending;
    };
    void processConsistency(const AlignedSample&);
    void processResponse(const AlignedSample&);
    void processAligned(AlignedSample);
    using PendingKey = std::tuple<std::uint64_t, std::string, std::string, std::uint64_t,
        std::uint64_t, std::uint64_t, std::uint64_t>;
    struct ReorderGroup {
        std::uint64_t max_seen{}, closed_before{};
        std::optional<std::uint64_t> closed_through;
        std::multimap<PendingKey, AlignedSample> samples;
    };
    void drain(ReorderGroup&, std::uint64_t limit, bool inclusive);
    void expire(const std::string& group, std::uint64_t time_ns, bool inclusive);
    void emit(CorrelationResult);
    BackendConfiguration config_;
    TimeQualityProcessor quality_;
    std::shared_ptr<IAnalysisSink> sink_;
    std::vector<PairState> pairs_;
    std::vector<ResponseState> responses_;
    std::map<std::pair<std::string, std::string>, std::vector<std::size_t>> consistency_by_input_, responses_by_input_;
    std::map<std::string, std::uint64_t> watermarks_;
    std::map<std::string, ReorderGroup> reorder_;
    std::size_t buffered_{};
    std::deque<CorrelationResult> history_;
    ProcessingStats stats_;
    mutable std::mutex mutex_;
    bool finished_{};
};
}
