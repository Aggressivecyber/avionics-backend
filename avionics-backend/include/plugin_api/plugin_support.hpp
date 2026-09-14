#pragma once
// Optional C++ helpers used INSIDE plugins; not part of the public binary ABI.
#include "plugin_api/bus_plugin.h"
#include <atomic>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

namespace avionics::plugin {
using Options = std::map<std::string, std::string>;
inline Options parse(const char* text) {
    if (!text) throw std::invalid_argument("configuration must not be null");
    Options options;
    std::string input(text);
    std::size_t at{};
    while (at < input.size()) {
        auto end = input.find(';', at);
        if (end == std::string::npos) end = input.size();
        const auto item = input.substr(at, end - at);
        const auto equals = item.find('=');
        if (equals == std::string::npos || equals == 0 || equals + 1 == item.size())
            throw std::invalid_argument("configuration requires key=value pairs separated by semicolons");
        if (!options.emplace(item.substr(0, equals), item.substr(equals + 1)).second)
            throw std::invalid_argument("duplicate configuration key");
        at = end + 1;
    }
    return options;
}
inline std::string take(Options& options, const std::string& key, std::string fallback) {
    auto found = options.find(key);
    if (found == options.end()) return fallback;
    auto value = found->second; options.erase(found); return value;
}
inline std::int64_t integer(Options& options, const std::string& key, std::int64_t fallback,
    std::int64_t low, std::int64_t high) {
    const auto text = take(options, key, std::to_string(fallback));
    std::int64_t result{};
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || result < low || result > high)
        throw std::invalid_argument("invalid configuration value: " + key);
    return result;
}
inline void noUnknown(const Options& options) {
    if (!options.empty()) throw std::invalid_argument("unknown configuration key: " + options.begin()->first);
}

class Worker {
public:
    explicit Worker(const BusHost& host) : host_(host) {}
    virtual ~Worker() { stop(); }
    virtual void configure(const char*) = 0;
    void start() {
        if (!configured_) throw std::logic_error("adapter is not configured");
        if (running_) throw std::logic_error("adapter is already running");
        if (thread_.joinable()) thread_.join();
        beforeStart();
        stop_requested_ = false; emitted_ = 0; rejected_ = 0; errors_ = 0; running_ = true;
        try {
            thread_ = std::thread([this] {
                try { run(); } catch (...) { ++errors_; }
                running_ = false;
            });
        } catch (...) { running_ = false; throw; }
    }
    void stop() noexcept {
        { std::lock_guard lock(wait_mutex_); stop_requested_ = true; }
        changed_.notify_all();
        if (thread_.joinable()) thread_.join();
        running_ = false;
    }
    void status(BusAdapterStatus& out) const noexcept {
        out = {sizeof(out), running_ ? 1u : 0u, emitted_.load(), rejected_.load(), errors_.load()};
    }
protected:
    void prepareConfigure() {
        if (running_) throw std::logic_error("stop before configuring");
        stop();
    }
    bool waitFor(std::chrono::nanoseconds delay) {
        std::unique_lock lock(wait_mutex_);
        return changed_.wait_for(lock, delay, [&] { return stop_requested_.load(); });
    }
    bool waitUntil(std::chrono::steady_clock::time_point deadline) {
        std::unique_lock lock(wait_mutex_);
        return changed_.wait_until(lock, deadline, [&] { return stop_requested_.load(); });
    }
    void emit(const BusFrameView& frame) noexcept {
        if (host_.emit_frame(host_.context, &frame)) ++emitted_; else ++rejected_;
    }
    virtual void beforeStart() {}
    virtual void run() = 0;
    BusHost host_;
    bool configured_{};
    std::atomic<bool> stop_requested_{true};
private:
    std::atomic<bool> running_{};
    std::atomic<std::uint64_t> emitted_{}, rejected_{}, errors_{};
    std::thread thread_;
    std::mutex wait_mutex_;
    std::condition_variable changed_;
};

template<class Function> std::int32_t guard(Function&& function, BusError* error) noexcept {
    if (error) error->message[0] = '\0';
    try { function(); return 1; }
    catch (const std::exception& e) { if (error) std::snprintf(error->message, BUS_ERROR_CAPACITY, "%s", e.what()); }
    catch (...) { if (error) std::snprintf(error->message, BUS_ERROR_CAPACITY, "%s", "unknown plugin error"); }
    return 0;
}
template<class T> struct Glue {
    static std::int32_t BUS_CALL create(const BusHost* host, void** output, BusError* error) noexcept {
        if (output) *output = nullptr;
        return guard([&] {
            if (!host || !output || host->struct_size < sizeof(BusHost) || host->abi_version != BUS_ABI_VERSION ||
                !host->emit_frame || !host->monotonic_now_ns) throw std::invalid_argument("invalid host ABI");
            *output = new T(*host);
        }, error);
    }
    static T& instance(void* handle) {
        if (!handle) throw std::invalid_argument("null adapter handle");
        return *static_cast<T*>(handle);
    }
    static std::int32_t BUS_CALL configure(void* handle, const char* config, BusError* error) noexcept {
        return guard([&] { instance(handle).configure(config); }, error);
    }
    static std::int32_t BUS_CALL start(void* handle, BusError* error) noexcept {
        return guard([&] { instance(handle).start(); }, error);
    }
    static void BUS_CALL stop(void* handle) noexcept { if (handle) static_cast<T*>(handle)->stop(); }
    static void BUS_CALL destroy(void* handle) noexcept { delete static_cast<T*>(handle); }
    static std::int32_t BUS_CALL status(void* handle, BusAdapterStatus* output, BusError* error) noexcept {
        return guard([&] {
            if (!output || output->struct_size < sizeof(BusAdapterStatus)) throw std::invalid_argument("invalid status buffer");
            instance(handle).status(*output);
        }, error);
    }
    static std::int32_t query(std::uint32_t requested, BusPluginApi* output, const char* name) noexcept {
        if (requested != BUS_ABI_VERSION || !output || output->struct_size < sizeof(BusPluginApi)) return 0;
        BusPluginApi api{};
        api.struct_size = sizeof(api); api.abi_version = BUS_ABI_VERSION;
        std::snprintf(api.name, sizeof(api.name), "%s", name);
        api.create = &create; api.configure = &configure; api.start = &start;
        api.stop = &stop; api.destroy = &destroy; api.status = &status;
        *output = api; return 1;
    }
};
}
