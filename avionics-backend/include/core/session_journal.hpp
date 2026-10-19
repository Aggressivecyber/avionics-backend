#pragma once
#include <atomic>
#include <filesystem>
#include <fstream>
#include <mutex>

namespace avionics {
struct AdapterStatus;
class SessionJournal {
public:
    explicit SessionJournal(const std::filesystem::path&);
    void write(const std::string& source, const std::string& action, const AdapterStatus&,
        const std::string& plugin_path = "", const std::string& plugin_configuration = "");
    bool hadFailures() const noexcept { return had_failures_; }
private:
    std::ofstream output_;
    std::mutex mutex_;
    std::atomic<bool> had_failures_{};
};
}
