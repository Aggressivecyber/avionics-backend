#pragma once
#include "core/frame_pipeline.hpp"
#include <shared_mutex>
#include <stdexcept>

namespace avionics {
class DecoderRegistry final : public IParameterDecoder {
public:
    void registerDecoder(std::uint32_t protocol, std::shared_ptr<IParameterDecoder> decoder) {
        if (!decoder) throw std::invalid_argument("decoder must not be null");
        std::unique_lock lock(mutex_);
        decoders_.insert_or_assign(protocol, std::move(decoder));
    }
    std::vector<ParameterSample> decode(const RawFrame& frame) override {
        std::shared_ptr<IParameterDecoder> decoder;
        {
            std::shared_lock lock(mutex_);
            const auto found = decoders_.find(frame.protocol);
            if (found == decoders_.end()) return {};
            decoder = found->second;
        }
        return decoder->decode(frame);
    }
private:
    std::shared_mutex mutex_;
    std::map<std::uint32_t, std::shared_ptr<IParameterDecoder>> decoders_;
};
}
