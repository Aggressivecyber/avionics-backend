#include "core/configuration.hpp"
#include "plugin_api/bus_plugin.h"
#include "plugin_api/mil1553b_capture.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <fstream>
#include <set>
#include <sstream>
#include <stdexcept>

namespace avionics {
namespace {
using Values = std::map<std::string, std::string>;
std::string trim(std::string s) {
    const auto first = s.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    return s.substr(first, s.find_last_not_of(" \t\r\n") - first + 1);
}
std::string take(Values& values, const std::string& key, std::optional<std::string> fallback = {}) {
    const auto found = values.find(key);
    if (found == values.end()) {
        if (fallback) return *fallback;
        throw std::invalid_argument("missing configuration key: " + key);
    }
    auto text = found->second; values.erase(found); return text;
}
template<class T> T number(const std::string& text) {
    T value{};
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size())
        throw std::invalid_argument("invalid numeric value: " + text);
    if constexpr (std::is_floating_point_v<T>) if (!std::isfinite(value)) throw std::invalid_argument("nonfinite config value");
    return value;
}
std::uint64_t milliseconds(const std::string& text) {
    const auto value = number<std::uint64_t>(text);
    if (value > 86400000) throw std::invalid_argument("duration exceeds 24 hours");
    return value * 1000000;
}
void empty(const Values& values) {
    if (!values.empty()) throw std::invalid_argument("unknown configuration key: " + values.begin()->first);
}
Selector selector(Values& values, const std::string& prefix) {
    return {take(values, prefix + "_source"), take(values, prefix + "_parameter")};
}
}
BackendConfiguration BackendConfiguration::load(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::invalid_argument("cannot open backend configuration");
    std::ostringstream bytes; bytes << file.rdbuf();
    if (file.bad() || !bytes) throw std::invalid_argument("failed to read configuration");
    auto document = std::make_shared<ConfigurationDocument>(); document->bytes = bytes.str();
    Sha256 hash;
    hash.update({reinterpret_cast<const std::uint8_t*>(document->bytes.data()), document->bytes.size()});
    document->sha256 = hash.finish();
    std::istringstream input(document->bytes);
    std::map<std::string, Values> sections;
    Values* current = nullptr;
    std::string line;
    std::size_t line_number{};
    while (std::getline(input, line)) {
        ++line_number;
        if (line_number == 1 && line.starts_with("\xef\xbb\xbf")) line.erase(0, 3);
        line = trim(line);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        if (line[0] == '[' && line.back() == ']') {
            const auto name = trim(line.substr(1, line.size() - 2));
            if (name.empty() || !sections.emplace(name, Values{}).second) throw std::invalid_argument("empty/duplicate section");
            current = &sections.at(name);
        } else {
            const auto equal = line.find('=');
            if (!current || equal == std::string::npos) throw std::invalid_argument("invalid INI line " + std::to_string(line_number));
            const auto key = trim(line.substr(0, equal)), value = trim(line.substr(equal + 1));
            if (key.empty() || value.empty() || !current->emplace(key, value).second) throw std::invalid_argument("empty/duplicate key");
        }
    }
    if (input.bad()) throw std::invalid_argument("failed to read configuration");
    BackendConfiguration config; config.document = std::move(document);
    auto metadata = sections.find("dictionary");
    if (metadata == sections.end()) throw std::invalid_argument("missing [dictionary]");
    const auto schema = take(metadata->second, "schema");
    if (schema != "2") throw std::invalid_argument("unsupported configuration schema; expected 2");
    config.version = take(metadata->second, "version"); empty(metadata->second); sections.erase(metadata);
    if (auto limits = sections.find("processing"); limits != sections.end()) {
        config.max_parameter_states = number<std::size_t>(take(limits->second, "max_parameter_states", "4096"));
        config.max_reorder_samples = number<std::size_t>(take(limits->second, "max_reorder_samples", "8192"));
        config.allowed_lateness_ns = milliseconds(take(limits->second, "allowed_lateness_ms"));
        if (!config.max_parameter_states) throw std::invalid_argument("max_parameter_states must be positive");
        if (!config.max_reorder_samples) throw std::invalid_argument("max_reorder_samples must be positive");
        empty(limits->second); sections.erase(limits);
    } else throw std::invalid_argument("schema 2 requires [processing] and explicit allowed_lateness_ms");
    if (auto budget = sections.find("pipeline"); budget != sections.end()) {
        auto& limits = config.pipeline;
        limits.queue_frames = number<std::size_t>(take(budget->second, "queue_frames", "4096"));
        limits.source_frames = number<std::size_t>(take(budget->second, "source_frames", "1024"));
        limits.analysis_frames = number<std::size_t>(take(budget->second, "analysis_frames", "4096"));
        limits.queue_bytes = number<std::size_t>(take(budget->second, "queue_bytes", "67108864"));
        limits.source_bytes = number<std::size_t>(take(budget->second, "source_bytes", "16777216"));
        limits.analysis_bytes = number<std::size_t>(take(budget->second, "analysis_bytes", "67108864"));
        limits.max_sources = number<std::size_t>(take(budget->second, "max_sources", "256"));
        limits.validate(); empty(budget->second); sections.erase(budget);
    }
    if (auto capture = sections.find("capture"); capture != sections.end()) {
        config.capture_command_timeout_ms = number<std::uint32_t>(take(capture->second, "command_timeout_ms", "5000"));
        config.capture_stop_timeout_ms = number<std::uint32_t>(take(capture->second, "stop_timeout_ms", "1000"));
        if (!config.capture_command_timeout_ms || !config.capture_stop_timeout_ms ||
            config.capture_command_timeout_ms > 600000 || config.capture_stop_timeout_ms > 600000)
            throw std::invalid_argument("capture deadlines must be within 1..600000 ms");
        empty(capture->second); sections.erase(capture);
    }
    if (auto recording = sections.find("recording"); recording != sections.end()) {
        config.recording.volume_bytes = number<std::uint64_t>(take(recording->second, "volume_bytes", "268435456"));
        config.recording.volume_seconds = number<std::uint64_t>(take(recording->second, "volume_seconds", "1800"));
        config.warning_free_bytes = number<std::uint64_t>(take(recording->second, "warning_free_bytes", "2147483648"));
        if ((config.recording.volume_bytes && config.recording.volume_bytes < 256) ||
            config.recording.volume_seconds > std::uint64_t(std::numeric_limits<std::int64_t>::max() / 1000000000))
            throw std::invalid_argument("invalid recording volume size or duration");
        empty(recording->second); sections.erase(recording);
    }
    std::set<std::string> field_ids;
    for (auto& [section, values] : sections) {
        const auto space = section.find(' ');
        if (space == std::string::npos) throw std::invalid_argument("section requires type and id: " + section);
        const auto type = section.substr(0, space), id = trim(section.substr(space + 1));
        if (id.empty()) throw std::invalid_argument("empty section id");
        if (type == "parameter") {
            FieldDefinition field; field.id = id;
            field.source = take(values, "source"); field.unit = take(values, "unit");
            field.protocol = number<std::uint32_t>(take(values, "protocol"));
            field.channel = number<std::uint32_t>(take(values, "channel"));
            if (field.protocol == BUS_PROTOCOL_MIL1553B) {
                Mil1553BFieldSelector selected;
                selected.rt_address = number<std::uint32_t>(take(values, "rt_address"));
                selected.subaddress = number<std::uint32_t>(take(values, "subaddress"));
                const auto direction = take(values, "direction");
                if (selected.rt_address > 31 || !selected.subaddress || selected.subaddress >= 31 ||
                    (direction != "receive" && direction != "transmit") || (selected.rt_address == 31 && direction == "transmit"))
                    throw std::invalid_argument("invalid 1553B RT/subaddress/direction selector");
                selected.transmit = direction == "transmit"; field.mil1553b = selected;
            }
            field.bit_offset = number<std::uint32_t>(take(values, "bit_offset"));
            field.bit_width = number<std::uint32_t>(take(values, "bit_width"));
            const auto order = take(values, "byte_order");
            if (order != "little" && order != "big") throw std::invalid_argument("byte_order must be little or big");
            field.big_endian = order == "big";
            const auto encoding = take(values, "type");
            if (encoding == "signed") field.type = FieldType::Signed;
            else if (encoding == "unsigned") field.type = FieldType::Unsigned;
            else if (encoding == "float64") field.type = FieldType::Float64;
            else if (encoding == "bool") field.type = FieldType::Boolean;
            else if (encoding == "enum") field.type = FieldType::Enumeration;
            else throw std::invalid_argument("unsupported field type");
            field.scale = number<double>(take(values, "scale", "1"));
            field.offset = number<double>(take(values, "offset", "0"));
            field.max_age_ns = milliseconds(take(values, "max_age_ms"));
            field.sequence_step = number<std::uint64_t>(take(values, "sequence_step", "0"));
            if (values.contains("valid_bit")) field.valid_bit = number<std::uint32_t>(take(values, "valid_bit"));
            if (field.type == FieldType::Enumeration) {
                std::istringstream items(take(values, "enum_values")); std::string item;
                while (std::getline(items, item, ',')) {
                    const auto colon = item.find(':');
                    if (colon == std::string::npos) throw std::invalid_argument("enum_values requires code:label pairs");
                    const auto code = number<std::int64_t>(trim(item.substr(0, colon)));
                    const auto label = trim(item.substr(colon + 1));
                    if (code < 0 || label.empty() || !field.enum_values.emplace(code, label).second)
                        throw std::invalid_argument("invalid/duplicate enum code");
                }
            }
            if (!field.bit_width || field.bit_width > 64 || std::uint64_t(field.bit_offset) + field.bit_width > BUS_MAX_PAYLOAD * 8ull ||
                (field.valid_bit && *field.valid_bit >= BUS_MAX_PAYLOAD * 8u) || !field.max_age_ns)
                throw std::invalid_argument("field exceeds payload bounds or max_age is zero");
            if (field.type == FieldType::Float64 && (field.bit_width != 64 || field.bit_offset % 8 != 0))
                throw std::invalid_argument("float64 requires aligned 64 bits");
            if (field.type == FieldType::Boolean && field.bit_width != 1) throw std::invalid_argument("bool requires one bit");
            if (field.mil1553b && (std::uint64_t(field.bit_offset) + field.bit_width > 512 ||
                (field.valid_bit && *field.valid_bit >= 512))) throw std::invalid_argument("1553B data exceeds 32 words");
            if ((field.type == FieldType::Boolean || field.type == FieldType::Enumeration) && (field.scale != 1 || field.offset != 0))
                throw std::invalid_argument("bool and enum cannot be scaled");
            if (field.type == FieldType::Enumeration) {
                if (field.bit_width > 63 || field.enum_values.empty()) throw std::invalid_argument("enum requires 1..63 bits and labels");
                for (const auto& [code, label] : field.enum_values) {
                    (void)label;
                    if (static_cast<std::uint64_t>(code) >= (1ull << field.bit_width)) throw std::invalid_argument("enum code exceeds width");
                }
            }
            field_ids.insert(id); config.fields.push_back(std::move(field));
        } else if (type == "protocol") {
            ProtocolInputDefinition definition; definition.address.source = take(values, "source", id);
            definition.address.channel = number<std::uint32_t>(take(values, "channel"));
            const auto protocol = number<std::uint32_t>(take(values, "protocol"));
            const auto selection = take(values, "selection", "auto");
            if (protocol != BUS_PROTOCOL_MIL1553B || (selection != "auto" && selection != "manual") || definition.address.source == "*")
                throw std::invalid_argument("protocol input requires a named source, 1553B and auto/manual selection");
            definition.selection = selection == "manual" ? ProtocolSelection::manual(protocol) : ProtocolSelection::automatic();
            definition.input.capture_representation = take(values, "capture_representation");
            if (definition.input.capture_representation != mil1553b::capture_representation)
                throw std::invalid_argument("unsupported 1553B capture representation");
            config.protocol_inputs.push_back(std::move(definition));
        } else if (type == "clock") {
            ClockDefinition clock; clock.source = id;
            clock.group = take(values, "group"); clock.domain = number<std::uint32_t>(take(values, "domain"));
            clock.offset_ns = number<std::int64_t>(take(values, "offset_ns", "0"));
            clock.uncertainty_ns = number<std::uint64_t>(take(values, "uncertainty_ns", "0"));
            if (clock.uncertainty_ns > 86400000000000ull) throw std::invalid_argument("clock uncertainty exceeds 24h");
            config.clocks.push_back(std::move(clock));
        } else if (type == "consistency") {
            ConsistencyDefinition rule; rule.id = id;
            rule.left = selector(values, "left"); rule.right = selector(values, "right");
            rule.tolerance = number<double>(take(values, "tolerance"));
            rule.window_ns = milliseconds(take(values, "window_ms"));
            if (rule.tolerance < 0 || !rule.window_ns) throw std::invalid_argument("invalid consistency rule");
            config.consistency.push_back(std::move(rule));
        } else if (type == "limit") {
            LimitDefinition rule; rule.id = id;
            rule.input = selector(values, "input");
            rule.upper = number<double>(take(values, "upper"));
            config.limits.push_back(std::move(rule));
        } else if (type == "response") {
            ResponseDefinition rule; rule.id = id;
            rule.command = selector(values, "command"); rule.response = selector(values, "response");
            rule.command_threshold = number<double>(take(values, "command_threshold"));
            rule.response_threshold = number<double>(take(values, "response_threshold"));
            rule.max_delay_ns = milliseconds(take(values, "max_delay_ms"));
            if (!rule.max_delay_ns) throw std::invalid_argument("response window must be positive");
            config.responses.push_back(std::move(rule));
        } else throw std::invalid_argument("unknown section type: " + type);
        empty(values);
    }
    if (config.fields.empty()) throw std::invalid_argument("dictionary has no parameters");
    std::set<std::pair<std::string, std::string>> parameter_states;
    if (config.protocol_inputs.size() > ProtocolRoutingLimits{}.max_streams)
        throw std::invalid_argument("protocol input count exceeds stream budget");
    std::set<std::pair<std::string, std::uint32_t>> protocol_addresses;
    for (const auto& definition : config.protocol_inputs) {
        if (!protocol_addresses.emplace(definition.address.source, definition.address.channel).second)
            throw std::invalid_argument("duplicate protocol source/channel");
        parameter_states.emplace(definition.address.source, "mil1553b.protocol_status.ch" + std::to_string(definition.address.channel));
    }
    for (const auto& field : config.fields) {
        if (field.id.starts_with("mil1553b.protocol_status.")) throw std::invalid_argument("reserved protocol diagnostic parameter id");
        if (field.mil1553b) {
            const auto found = std::find_if(config.protocol_inputs.begin(), config.protocol_inputs.end(), [&](const auto& definition) {
                return definition.address.channel == field.channel && (field.source == "*" || definition.address.source == field.source);
            });
            if (found == config.protocol_inputs.end()) throw std::invalid_argument("1553B field has no configured protocol input");
        }
        if (field.source == "*") {
            for (const auto& clock : config.clocks) parameter_states.emplace(clock.source, field.id);
        } else parameter_states.emplace(field.source, field.id);
    }
    if (parameter_states.size() > config.max_parameter_states)
        throw std::invalid_argument("configuration requires " + std::to_string(parameter_states.size()) +
            " parameter states; max_parameter_states=" + std::to_string(config.max_parameter_states));
    auto validate_selector = [&](const Selector& selected) {
        for (const auto& field : config.fields)
            if (field.id == selected.parameter && (field.source == selected.source || field.source == "*")) return;
        throw std::invalid_argument("rule refers to unknown source/parameter: " + selected.source + "/" + selected.parameter);
    };
    auto distinct = [](const Selector& left, const Selector& right) {
        if (left.source == right.source && left.parameter == right.parameter) throw std::invalid_argument("rule requires distinct parameters");
    };
    for (const auto& rule : config.consistency) { validate_selector(rule.left); validate_selector(rule.right); distinct(rule.left, rule.right); }
    for (const auto& rule : config.responses) { validate_selector(rule.command); validate_selector(rule.response); distinct(rule.command, rule.response); }
    for (const auto& rule : config.limits) validate_selector(rule.input);
    return config;
}
void ConfigurationDocument::save(const std::filesystem::path& path) const {
    std::ofstream output(path, std::ios::binary); output << bytes; output.close();
    if (!output) throw std::runtime_error("cannot write configuration snapshot");
}
}
