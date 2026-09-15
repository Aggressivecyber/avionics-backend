#include "core/configuration.hpp"
#include "core/field_value.hpp"
#include "plugin_api/bus_plugin.h"
#include <stdexcept>
#include <utility>

namespace avionics {
std::vector<ParameterSample> DictionaryDecoder::decode(const RawFrame& frame) {
    std::vector<ParameterSample> samples;
    for (const auto& field : config_.fields) {
        if (field.protocol != frame.protocol || field.channel != frame.channel ||
            (field.source != "*" && field.source != frame.source)) continue;
        ParameterSample sample;
        sample.source = frame.source; sample.parameter_id = field.id; sample.unit = field.unit;
        sample.generation = frame.generation; sample.origin_generation = frame.origin_generation;
        sample.capture_time_ns = frame.capture_time_ns; sample.ingest_time_ns = frame.ingest_time_ns;
        sample.clock_domain = frame.clock_domain; sample.raw_record_index = frame.record_index;
        sample.sequence = frame.sequence; sample.sequence_step = field.sequence_step; sample.max_age_ns = field.max_age_ns;
        sample.decoder_version = config_.version; sample.valid = (frame.flags & BUS_FRAME_INVALID) == 0;
        try { decodeFieldValue(field, frame.payload, sample.value, sample.valid); }
        catch (const std::out_of_range&) { sample.valid = false; sample.value = 0.0; }
        samples.push_back(std::move(sample));
    }
    return samples;
}
}
