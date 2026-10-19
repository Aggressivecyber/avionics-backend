#pragma once
#include "core/processing.hpp"
#include <ostream>

namespace avionics {
void writeAlignedSample(std::ostream&, const AlignedSample&);
void writeCorrelationResult(std::ostream&, const CorrelationResult&);
}
