#include "core/bus_manager.hpp"
#include <cctype>
#include <stdexcept>

namespace avionics {
BusManager::BusManager(std::shared_ptr<FramePipeline> pipeline) : pipeline_(std::move(pipeline)) {
    if (!pipeline_) throw std::invalid_argument("pipeline is required");
}
BusManager::~BusManager() {
    // Destruction requires management callers to have joined; stop callbacks first.
    for (auto& [id, entry] : entries_) { (void)id; entry->adapter->stop(); }
    entries_.clear();
}
std::shared_ptr<BusManager::Entry> BusManager::find(const std::string& id) const {
    std::lock_guard lock(mutex_);
    const auto found = entries_.find(id);
    if (found == entries_.end()) throw std::invalid_argument("unknown bus instance: " + id);
    return found->second;
}
void BusManager::add(const std::string& id, const std::filesystem::path& plugin, const std::string& config) {
    if (id.empty() || id.size() > 64) throw std::invalid_argument("instance id must have 1..64 characters");
    for (unsigned char c : id) if (!std::isalnum(c) && c != '_' && c != '-') throw std::invalid_argument("invalid instance id");
    auto entry = std::make_shared<Entry>();
    entry->adapter = plugins_.create(plugin, id, pipeline_);
    entry->adapter->configure(config);
    std::lock_guard lock(mutex_);
    if (!entries_.emplace(id, entry).second) throw std::invalid_argument("duplicate instance id: " + id);
}
void BusManager::start(const std::string& id) {
    auto entry = find(id);
    std::lock_guard lock(entry->mutex);
    if (entry->removed) throw std::logic_error("instance was removed");
    entry->adapter->start(next_generation_++);
}
void BusManager::drain(const std::string& id) {
    if (!pipeline_->drainSource(id, std::chrono::seconds(5)))
        throw std::runtime_error("source drain failed; adapter remains stopped: " + id);
}
void BusManager::stop(const std::string& id) {
    auto entry = find(id);
    std::lock_guard lock(entry->mutex);
    if (entry->removed) throw std::logic_error("instance was removed");
    entry->adapter->stop(); drain(id);
}
void BusManager::remove(const std::string& id) {
    auto entry = find(id);
    std::lock_guard lock(entry->mutex);
    if (entry->removed) throw std::logic_error("instance was removed");
    entry->adapter->stop(); drain(id);
    entry->removed = true; entry->adapter.reset();
    std::lock_guard map_lock(mutex_);
    entries_.erase(id);
}
void BusManager::replace(const std::string& id, const std::filesystem::path& plugin, const std::string& config) {
    // Load and configure before touching the running instance. Some hardware
    // plugins must defer exclusive device acquisition until start().
    auto candidate = plugins_.create(plugin, id, pipeline_);
    candidate->configure(config);
    auto entry = find(id);
    std::lock_guard lock(entry->mutex);
    if (entry->removed) throw std::logic_error("instance was removed");
    const bool was_running = entry->adapter->status().state == AdapterState::Running;
    entry->adapter->stop(); drain(id);
    if (was_running) {
        try { candidate->start(next_generation_++); }
        catch (const std::exception& start_error) {
            const std::string reason = start_error.what();
            candidate->stop();
            drain(id); // failed starts are permitted to have emitted frames.
            try { entry->adapter->start(next_generation_++); }
            catch (const std::exception& rollback_error) {
                throw std::runtime_error("replacement failed: " + reason + "; rollback failed: " + rollback_error.what());
            }
            throw std::runtime_error("replacement failed; previous adapter restarted: " + reason);
        }
    }
    entry->adapter = std::move(candidate); // old workers joined before module release.
}
AdapterStatus BusManager::status(const std::string& id) const {
    auto entry = find(id);
    std::lock_guard lock(entry->mutex);
    if (entry->removed) throw std::logic_error("instance was removed");
    return entry->adapter->status();
}
}
