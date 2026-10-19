#pragma once
#include "core/capture_adapter.hpp"
#include "core/session_journal.hpp"
#include <atomic>

namespace avionics {
class BusManager {
public:
    explicit BusManager(std::shared_ptr<FramePipeline>, std::shared_ptr<SessionJournal> = nullptr, CaptureOptions = {});
    ~BusManager();
    void add(const std::string& id, const std::filesystem::path& plugin, const std::string& config);
    void start(const std::string& id);
    void stop(const std::string& id);
    void remove(const std::string& id);
    void replace(const std::string& id, const std::filesystem::path& plugin, const std::string& config);
    void shutdown();
    AdapterStatus status(const std::string& id) const;
    std::size_t sourceCount() const { std::lock_guard lock(mutex_); return entries_.size(); }
private:
    struct Entry { std::mutex mutex; std::unique_ptr<IBusAdapter> adapter; bool removed{}; };
    std::shared_ptr<Entry> find(const std::string&) const;
    void drain(const std::string&);
    std::shared_ptr<FramePipeline> pipeline_;
    std::shared_ptr<SessionJournal> journal_;
    CaptureOptions capture_options_;
    mutable std::mutex mutex_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
    std::atomic<std::uint64_t> next_generation_{1};
};
}
