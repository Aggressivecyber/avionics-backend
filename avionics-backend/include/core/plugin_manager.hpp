#pragma once
#include "core/frame_pipeline.hpp"
#include "core/shared_library.hpp"
#include "plugin_api/bus_plugin.h"

namespace avionics {
enum class AdapterState { Loaded, Configured, Running, Stopped, Faulted };
struct AdapterStatus {
    AdapterState state{};
    std::uint64_t generation{}, emitted{}, rejected{}, errors{}, malformed{};
};
class IBusAdapter {
public:
    virtual ~IBusAdapter() = default;
    virtual void configure(const std::string&) = 0;
    virtual void start(std::uint64_t generation) = 0;
    virtual void stop() noexcept = 0;
    virtual AdapterStatus status() const = 0;
};
class PluginModule {
public:
    explicit PluginModule(const std::filesystem::path&);
    const BusPluginApi& api() const noexcept { return api_; }
private:
    SharedLibrary library_; // API pointers never outlive this library.
    BusPluginApi api_{};
};
class PluginManager {
public:
    std::unique_ptr<IBusAdapter> create(const std::filesystem::path&, const std::string& source,
        std::shared_ptr<FramePipeline>);
    std::size_t loadedCount() const;
private:
    mutable std::mutex mutex_;
    std::map<std::filesystem::path, std::weak_ptr<PluginModule>> modules_;
};
}
