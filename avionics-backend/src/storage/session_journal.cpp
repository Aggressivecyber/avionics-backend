#include "core/session_journal.hpp"
#include "core/plugin_manager.hpp"
#include "core/json.hpp"
#include "core/types.hpp"

namespace avionics {
SessionJournal::SessionJournal(const std::filesystem::path& path) {
    if (std::filesystem::exists(path)) throw std::runtime_error("lifecycle journal already exists");
    output_.open(path, std::ios::binary);
    if (!output_) throw std::runtime_error("cannot create lifecycle journal");
}
void SessionJournal::write(const std::string& source, const std::string& action, const AdapterStatus& status,
    const std::string& plugin_path, const std::string& plugin_configuration) {
    if (status.state == AdapterState::Faulted || status.errors || status.malformed) had_failures_ = true;
    std::lock_guard lock(mutex_);
    output_ << "{\"schema\":1,\"host_time_ns\":" << monotonicNowNs() << ",\"source\":" << jsonString(source)
        << ",\"action\":" << jsonString(action) << ",\"state\":" << static_cast<unsigned>(status.state)
        << ",\"generation\":" << status.generation << ",\"emitted\":" << status.emitted
        << ",\"rejected\":" << status.rejected << ",\"errors\":" << status.errors << ",\"malformed\":" << status.malformed
        << ",\"plugin_abi\":" << status.plugin_abi << ",\"failure\":" << jsonString(status.failure)
        << ",\"plugin_path\":" << jsonString(plugin_path) << ",\"plugin_configuration\":" << jsonString(plugin_configuration) << "}\n";
    output_.flush(); if (!output_) throw std::runtime_error("lifecycle journal write failed");
}
}
