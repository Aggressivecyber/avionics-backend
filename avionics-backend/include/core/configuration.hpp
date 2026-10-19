#pragma once
#include "core/parameter_interfaces.hpp"
#include "core/pipeline_limits.hpp"
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <tuple>

namespace avionics {
enum class FieldType { Signed, Unsigned, Float64, Boolean, Enumeration };
struct FieldDefinition {
    std::string id, source, unit;
    std::uint32_t protocol{}, channel{}, bit_offset{}, bit_width{};
    bool big_endian{};
    FieldType type{};
    double scale{1}, offset{};
    std::optional<std::uint32_t> valid_bit;
    std::map<std::int64_t, std::string> enum_values;
    std::uint64_t max_age_ns{}, sequence_step{};
};
struct ClockDefinition {
    std::string source, group;
    std::uint32_t domain{};
    std::int64_t offset_ns{};
    std::uint64_t uncertainty_ns{};
};
struct Selector {
    std::string source, parameter;
    bool matches(const ParameterSample& sample) const { return source == sample.source && parameter == sample.parameter_id; }
};
struct ConsistencyDefinition {
    std::string id;
    Selector left, right;
    double tolerance{};
    std::uint64_t window_ns{};
};
struct ResponseDefinition {
    std::string id;
    Selector command, response;
    double command_threshold{}, response_threshold{};
    std::uint64_t max_delay_ns{};
};
struct ConfigurationDocument {
    std::string bytes;
    ContentDigest sha256;
    void save(const std::filesystem::path&) const;
};
struct BackendConfiguration {
    std::string version;
    std::shared_ptr<const ConfigurationDocument> document;
    std::optional<SessionId> recording_session_id;
    std::size_t max_parameter_states{4096};
    std::size_t max_reorder_samples{8192};
    std::uint64_t allowed_lateness_ns{};
    PipelineLimits pipeline;
    std::vector<FieldDefinition> fields;
    std::vector<ClockDefinition> clocks;
    std::vector<ConsistencyDefinition> consistency;
    std::vector<ResponseDefinition> responses;
    static BackendConfiguration load(const std::filesystem::path&);
};
class DictionaryDecoder final : public IParameterDecoder {
public:
    explicit DictionaryDecoder(BackendConfiguration);
    std::vector<ParameterSample> decode(const RawFrame&) override;
private:
    BackendConfiguration config_;
    using InputKey = std::tuple<std::uint32_t, std::uint32_t, std::string>;
    std::map<InputKey, std::vector<std::size_t>> fields_by_input_;
};
}
