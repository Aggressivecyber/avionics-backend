#pragma once
#include <cstddef>
#include <stdexcept>

namespace avionics {
struct PipelineLimits {
    std::size_t queue_frames{4096}, source_frames{1024}, analysis_frames{4096};
    std::size_t queue_bytes{64 * 1024 * 1024}, source_bytes{16 * 1024 * 1024}, analysis_bytes{64 * 1024 * 1024};
    std::size_t max_sources{256};
    void validate() const {
        if (!queue_frames || !source_frames || !analysis_frames || !queue_bytes || !source_bytes ||
            !analysis_bytes || !max_sources || source_frames > queue_frames || source_bytes > queue_bytes)
            throw std::invalid_argument("pipeline budgets must be positive and source budgets must fit the global budget");
    }
};
}
