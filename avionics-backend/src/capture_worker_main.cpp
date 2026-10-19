#include "core/worker_wire.hpp"
#include "core/path.hpp"
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <mutex>
#include <thread>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <unistd.h>
#if defined(__linux__)
#include <sys/prctl.h>
#include <signal.h>
#endif
#endif

namespace {
using namespace avionics;
class Input {
public:
    bool read(std::span<std::uint8_t> bytes) {
        const auto count = std::fread(bytes.data(), 1, bytes.size(), stdin);
        if (count == bytes.size()) return true;
        if (count || std::ferror(stdin)) throw std::runtime_error("truncated or unreadable worker command");
        return false;
    }
};
class Output {
public:
    Output() {
#if defined(_WIN32)
        _setmode(_fileno(stdin), _O_BINARY);
        const auto descriptor = _dup(_fileno(stdout));
        if (descriptor < 0 || _setmode(descriptor, _O_BINARY) < 0) throw std::runtime_error("cannot open worker protocol output");
        stream_.reset(_fdopen(descriptor, "wb"));
        if (_dup2(_fileno(stderr), _fileno(stdout)) != 0) throw std::runtime_error("cannot redirect plugin console output");
#else
        const auto descriptor = dup(STDOUT_FILENO);
        if (descriptor < 0) throw std::runtime_error("cannot open worker protocol output");
        stream_.reset(fdopen(descriptor, "wb"));
        if (dup2(STDERR_FILENO, STDOUT_FILENO) < 0) throw std::runtime_error("cannot redirect plugin console output");
#endif
        if (!stream_) throw std::runtime_error("cannot initialize worker protocol stream");
        std::setvbuf(stream_.get(), nullptr, _IONBF, 0);
    }
    void send(std::span<const std::uint8_t> payload) {
        const auto bytes = wire::envelope(payload); std::lock_guard lock(mutex_);
        if (std::fwrite(bytes.data(), 1, bytes.size(), stream_.get()) != bytes.size()) throw std::runtime_error("worker protocol output failed");
    }
private:
    std::unique_ptr<std::FILE, int(*)(std::FILE*)> stream_{nullptr, &std::fclose};
    std::mutex mutex_;
};
struct CaptureHost {
    Output& output;
    explicit CaptureHost(Output& destination) : output(destination) {}
    std::atomic<std::uint64_t> generation{}, malformed{};
    std::mutex mutex;
    std::string failure;
    static std::uint64_t BUS_CALL now(void*) noexcept { return monotonicNowNs(); }
    static std::int32_t BUS_CALL emit(void* context, const BusFrameView* view) noexcept {
        auto& self = *static_cast<CaptureHost*>(context);
        try {
            if (!view || view->struct_size != sizeof(BusFrameView) || view->payload_size > BUS_MAX_PAYLOAD ||
                (view->payload_size && !view->payload) || view->origin_session_valid > 1) throw std::invalid_argument("malformed frame view");
            RawFrame frame; frame.generation = self.generation;
            frame.protocol = view->protocol; frame.channel = view->channel; frame.flags = view->flags; frame.clock_domain = view->clock_domain;
            frame.capture_time_ns = view->capture_time_ns; frame.sequence = view->sequence;
            if (view->origin_source) {
                std::size_t size{}; while (size <= 128 && view->origin_source[size]) ++size;
                if (size > 128) throw std::invalid_argument("origin source exceeds 128 bytes");
                frame.origin_source.assign(view->origin_source, size);
            }
            frame.origin_generation = view->origin_generation; frame.origin_record_index = view->origin_record_index;
            if (view->origin_session_valid) {
                SessionId identity{}; std::memcpy(identity.data(), view->origin_session_id, 16); frame.origin_session_id = identity;
            }
            if (view->payload_size) frame.payload.assign(view->payload, view->payload + view->payload_size);
            auto packet = wire::packet(wire::Kind::Frame); wire::frame(packet, frame); self.output.send(packet); return 1;
        } catch (const std::exception& error) {
            std::lock_guard lock(self.mutex); self.failure = error.what(); ++self.malformed; return 0;
        } catch (...) {
            std::lock_guard lock(self.mutex); self.failure = "non-standard frame callback exception"; ++self.malformed; return 0;
        }
    }
};
std::string errorText(const BusError& error) {
    const auto end = static_cast<const char*>(std::memchr(error.message, 0, sizeof(error.message)));
    if (!end) throw std::runtime_error("unterminated plugin error text");
    return {error.message, end};
}
}
int main() {
    using namespace avionics;
#if defined(_WIN32)
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
#elif defined(__linux__)
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() == 1) return 1;
#endif
    try {
        Input input; Output output; CaptureHost capture{output};
        BusHost host{sizeof(BusHost), BUS_ABI_VERSION, &capture, &CaptureHost::emit, &CaptureHost::now};
        std::unique_ptr<PluginModule> module;
        std::unique_ptr<void, std::function<void(void*)>> adapter(nullptr, [](void*) {});
        AdapterState state{AdapterState::Loaded}; std::mutex api_mutex; std::string control_failure;
        auto snapshot = [&] {
            AdapterStatus result; result.state = state; result.generation = capture.generation;
            result.failure = control_failure;
            if (adapter) {
                BusAdapterStatus status{sizeof(status), 0, 0, 0, 0}; BusError error{};
                if (!module->api().status(adapter.get(), &status, &error)) throw std::runtime_error("plugin status: " + errorText(error));
                result.emitted = status.emitted; result.rejected = status.rejected; result.errors = status.errors;
                result.plugin_abi = module->api().abi_version;
                if (state == AdapterState::Running && !status.running) result.state = AdapterState::Stopped;
                if (status.errors) {
                    result.state = AdapterState::Faulted;
                    if (!module->api().last_error(adapter.get(), &error)) throw std::runtime_error("plugin error query: " + errorText(error));
                    result.failure = errorText(error);
                }
            }
            { std::lock_guard lock(capture.mutex); result.malformed = capture.malformed; if (result.malformed) { result.state = AdapterState::Faulted; result.failure += capture.failure; } }
            return result;
        };
        std::jthread reporter;
        bool shutdown{}; std::vector<std::uint8_t> packet;
        while (!shutdown && wire::receive(input, packet)) {
            std::span<const std::uint8_t> cursor(packet); const auto command = static_cast<wire::Kind>(wire::take<std::uint8_t>(cursor));
            const auto id = wire::take<std::uint64_t>(cursor); std::string error;
            std::unique_lock lock(api_mutex);
            try {
                BusError details{};
                if (command == wire::Kind::Load) {
                    if (module) throw std::logic_error("worker already loaded a plugin");
                    if (wire::take<std::uint32_t>(cursor) != wire::protocol_version) throw std::runtime_error("capture worker protocol version mismatch");
                    const auto path = wire::text(cursor); wire::end(cursor); module = std::make_unique<PluginModule>(pathFromUtf8(path));
                    void* handle{};
                    const auto created = module->api().create(&host, &handle, &details);
                    adapter = {handle, [&](void* instance) { module->api().stop(instance); module->api().destroy(instance); }};
                    if (!created || !handle) throw std::runtime_error("plugin create: " + errorText(details));
                    reporter = std::jthread([&](std::stop_token stop) {
                        while (!stop.stop_requested()) {
                            AdapterStatus status;
                            { std::lock_guard api_lock(api_mutex);
                                try { status = snapshot(); }
                                catch (const std::exception& fault) { status.state = AdapterState::Faulted; status.generation = capture.generation; status.errors = 1; status.failure = fault.what(); }
                            }
                            auto message = wire::packet(wire::Kind::Status); wire::status(message, status);
                            try { output.send(message); } catch (const std::exception& fault) { std::fprintf(stderr, "worker reporter failed: %s\n", fault.what()); std::_Exit(2); }
                            std::this_thread::sleep_for(std::chrono::milliseconds(20));
                        }
                    });
                } else {
                    if (!adapter) throw std::logic_error("load a plugin before controlling it");
                    if (command == wire::Kind::Configure) {
                        const auto config = wire::text(cursor, 16384); wire::end(cursor);
                        if (!module->api().configure(adapter.get(), config.c_str(), &details)) throw std::runtime_error("plugin configure: " + errorText(details));
                        state = AdapterState::Configured;
                    } else if (command == wire::Kind::Start) {
                        capture.generation = wire::take<std::uint64_t>(cursor); wire::end(cursor);
                        if (state != AdapterState::Configured && state != AdapterState::Stopped) throw std::logic_error("configure or stop before start");
                        if (!module->api().start(adapter.get(), &details)) { state = AdapterState::Faulted; throw std::runtime_error("plugin start: " + errorText(details)); }
                        control_failure.clear();
                        state = AdapterState::Running;
                    } else if (command == wire::Kind::Stop || command == wire::Kind::Shutdown) {
                        wire::end(cursor); module->api().stop(adapter.get()); state = AdapterState::Stopped;
                        if (command == wire::Kind::Shutdown) shutdown = true;
                    } else throw std::runtime_error("unknown capture worker command");
                }
            } catch (const std::exception& fault) { error = fault.what(); if (state == AdapterState::Faulted) control_failure = error; }
            auto reply = wire::packet(wire::Kind::Reply); wire::put(reply, id); wire::text(reply, error);
            wire::status(reply, snapshot()); output.send(reply);
        }
        reporter.request_stop(); if (reporter.joinable()) reporter.join();
        adapter.reset(); return 0;
    } catch (const std::exception& error) { std::fprintf(stderr, "capture worker failed: %s\n", error.what()); return 1; }
}
