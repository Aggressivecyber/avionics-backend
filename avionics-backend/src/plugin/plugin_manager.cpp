#include "core/plugin_manager.hpp"
#include <atomic>
#include <cstring>
#include <stdexcept>

namespace avionics {
namespace {
std::string errorText(const BusError& error) {
    std::size_t size{};
    while (size < BUS_ERROR_CAPACITY && error.message[size]) ++size;
    return std::string(error.message, size);
}
class CPluginAdapter final : public IBusAdapter {
public:
    CPluginAdapter(std::shared_ptr<PluginModule> module, std::string source, std::shared_ptr<FramePipeline> pipeline)
        : module_(std::move(module)), source_(std::move(source)), pipeline_(std::move(pipeline)) {
        host_ = {sizeof(BusHost), BUS_ABI_VERSION, this, &emit, &now};
        BusError error{};
        if (!module_->api().create(&host_, &handle_, &error)) {
            if (handle_) module_->api().destroy(handle_);
            throw std::runtime_error("plugin create: " + errorText(error));
        }
        if (!handle_) throw std::runtime_error("plugin returned null instance");
    }
    ~CPluginAdapter() override { stop(); if (handle_) module_->api().destroy(handle_); }
    void configure(const std::string& config) override {
        if (state_ == AdapterState::Running) throw std::logic_error("cannot configure a running adapter");
        BusError error{};
        if (!module_->api().configure(handle_, config.c_str(), &error))
            throw std::runtime_error("plugin configure: " + errorText(error));
        state_ = AdapterState::Configured;
    }
    void start(std::uint64_t generation) override {
        if (state_ != AdapterState::Configured && state_ != AdapterState::Stopped)
            throw std::logic_error("adapter must be configured or stopped before start");
        generation_ = generation;
        BusError error{};
        if (!module_->api().start(handle_, &error)) {
            module_->api().stop(handle_); state_ = AdapterState::Faulted;
            throw std::runtime_error("plugin start: " + errorText(error));
        }
        state_ = AdapterState::Running;
    }
    void stop() noexcept override {
        if (!handle_) return;
        module_->api().stop(handle_);
        if (state_ == AdapterState::Running || state_ == AdapterState::Faulted) state_ = AdapterState::Stopped;
    }
    AdapterStatus status() const override {
        BusAdapterStatus status{}; status.struct_size = sizeof(status);
        BusError error{};
        if (!module_->api().status(handle_, &status, &error)) throw std::runtime_error("plugin status: " + errorText(error));
        auto state = state_;
        if (state == AdapterState::Running && !status.running)
            state = status.errors ? AdapterState::Faulted : AdapterState::Stopped;
        return {state, generation_, status.emitted, status.rejected, status.errors, malformed_.load()};
    }
private:
    static std::uint64_t BUS_CALL now(void*) noexcept { return monotonicNowNs(); }
    static std::int32_t BUS_CALL emit(void* context, const BusFrameView* view) noexcept {
        auto& self = *static_cast<CPluginAdapter*>(context);
        try {
            if (!view || view->struct_size < sizeof(BusFrameView) || view->payload_size > BUS_MAX_PAYLOAD ||
                (view->payload_size && !view->payload)) throw std::invalid_argument("malformed frame view");
            RawFrame frame;
            frame.source = self.source_; frame.generation = self.generation_;
            frame.protocol = view->protocol; frame.channel = view->channel;
            frame.flags = view->flags; frame.clock_domain = view->clock_domain;
            frame.capture_time_ns = view->capture_time_ns; frame.ingest_time_ns = monotonicNowNs();
            frame.sequence = view->sequence;
            if (view->origin_source) {
                std::size_t size{};
                while (size <= 128 && view->origin_source[size]) ++size;
                if (size > 128) throw std::invalid_argument("origin source too long");
                frame.origin_source.assign(view->origin_source, size);
            }
            frame.origin_generation = view->origin_generation; frame.origin_record_index = view->origin_record_index;
            if (view->payload_size) frame.payload.assign(view->payload, view->payload + view->payload_size);
            return self.pipeline_->submit(std::move(frame)) ? 1 : 0;
        } catch (...) { ++self.malformed_; return 0; }
    }
    std::shared_ptr<PluginModule> module_;
    std::string source_;
    std::shared_ptr<FramePipeline> pipeline_;
    BusHost host_{};
    void* handle_{};
    AdapterState state_{AdapterState::Loaded};
    std::uint64_t generation_{}; // modified only before starting/after joining workers.
    std::atomic<std::uint64_t> malformed_{};
};
}
PluginModule::PluginModule(const std::filesystem::path& path) : library_(path) {
    const auto query = reinterpret_cast<BusQueryApi>(library_.symbol("bus_plugin_query"));
    api_.struct_size = sizeof(api_);
    if (!query(BUS_ABI_VERSION, &api_) || api_.abi_version != BUS_ABI_VERSION || api_.struct_size < sizeof(api_) ||
        !api_.create || !api_.configure || !api_.start || !api_.stop || !api_.destroy || !api_.status)
        throw std::runtime_error("incompatible or incomplete plugin ABI");
    if (!std::memchr(api_.name, 0, sizeof(api_.name))) throw std::runtime_error("invalid plugin name");
}
std::unique_ptr<IBusAdapter> PluginManager::create(const std::filesystem::path& path, const std::string& source,
    std::shared_ptr<FramePipeline> pipeline) {
    const auto canonical = std::filesystem::canonical(path);
    std::shared_ptr<PluginModule> module;
    {
        std::lock_guard lock(mutex_);
        // Retain only live handles; repeated replacement does not grow this cache.
        std::erase_if(modules_, [](const auto& entry) { return entry.second.expired(); });
        module = modules_[canonical].lock();
        if (!module) { module = std::make_shared<PluginModule>(canonical); modules_[canonical] = module; }
    }
    return std::make_unique<CPluginAdapter>(std::move(module), source, std::move(pipeline));
}
std::size_t PluginManager::loadedCount() const {
    std::lock_guard lock(mutex_);
    std::size_t count{};
    for (const auto& [path, module] : modules_) { (void)path; if (!module.expired()) ++count; }
    return count;
}
}
