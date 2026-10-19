#pragma once
#include "core/types.hpp"

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
    virtual void finish() {}
};
}
