#pragma once
#include "core/plugin_manager.hpp"
#include <atomic>

namespace avionics {
class BusManager {
public:
    explicit BusManager(std::shared_ptr<FramePipeline>);
    ~BusManager();
    void add(const std::string& id, const std::filesystem::path& plugin, const std::string& config);
    void start(const std::string& id);
    void stop(const std::string& id);
    void remove(const std::string& id);
    void replace(const std::string& id, const std::filesystem::path& plugin, const std::string& config);
    AdapterStatus status(const std::string& id) const;
    std::size_t loadedPluginCount() const { return plugins_.loadedCount(); }
private:
    struct Entry { std::mutex mutex; std::unique_ptr<IBusAdapter> adapter; bool removed{}; };
    std::shared_ptr<Entry> find(const std::string&) const;
    void drain(const std::string&);
    std::shared_ptr<FramePipeline> pipeline_;
    PluginManager plugins_;
    mutable std::mutex mutex_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
    std::atomic<std::uint64_t> next_generation_{1};
};
}
