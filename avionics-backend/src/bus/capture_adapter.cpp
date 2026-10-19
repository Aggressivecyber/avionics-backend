#include "core/capture_adapter.hpp"

namespace avionics {
CaptureAdapter::CaptureAdapter(const CaptureOptions& options, const std::filesystem::path& plugin, std::string source,
    std::shared_ptr<FramePipeline> pipeline, std::shared_ptr<SessionJournal> journal)
    : options_(options), process_(options.executable), source_(std::move(source)), pipeline_(std::move(pipeline)), journal_(std::move(journal)),
      receiver_([this] { receive(); }) {
    try {
        std::vector<std::uint8_t> arguments;
        wire::put(arguments, wire::protocol_version);
        const auto utf8 = std::filesystem::absolute(plugin).u8string(); wire::text(arguments, std::string(utf8.begin(), utf8.end()));
        execute(wire::Kind::Load, std::move(arguments), options_.command_timeout);
    } catch (...) {
        { std::lock_guard lock(mutex_); closing_ = true; }
        process_.terminate(); receiver_.join(); throw;
    }
}
CaptureAdapter::~CaptureAdapter() {
    stop();
    { std::lock_guard lock(mutex_); closing_ = true; }
    try {
        bool alive; { std::lock_guard lock(mutex_); alive = alive_; }
        if (alive) execute(wire::Kind::Shutdown, {}, options_.stop_timeout);
        if (!process_.wait(options_.stop_timeout)) fault("worker did not exit after shutdown");
    } catch (const std::exception& error) { fault(error.what()); }
    process_.terminate();
    if (receiver_.joinable()) receiver_.join();
}
void CaptureAdapter::execute(wire::Kind kind, std::vector<std::uint8_t> arguments, std::chrono::milliseconds timeout) {
    std::lock_guard command_lock(command_mutex_);
    auto request = wire::packet(kind); const auto id = ++next_request_; wire::put(request, id);
    request.insert(request.end(), arguments.begin(), arguments.end()); const auto bytes = wire::envelope(request);
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    { std::lock_guard lock(mutex_); if (!alive_) throw std::runtime_error(status_.failure); }
    try { process_.write(bytes, timeout); }
    catch (const std::exception& error) { fault(error.what()); throw; }
    std::unique_lock lock(mutex_);
    if (!replied_.wait_until(lock, deadline, [&] { return reply_id_ == id || !alive_; })) {
        lock.unlock(); fault("capture worker command deadline exceeded: " + std::to_string(static_cast<unsigned>(kind)));
        throw std::runtime_error(status().failure);
    }
    if (!alive_ && reply_id_ != id) throw std::runtime_error(status_.failure);
    if (!reply_error_.empty()) throw std::runtime_error(reply_error_);
}
void CaptureAdapter::configure(const std::string& config) {
    if (config.size() > 16384) throw std::invalid_argument("plugin configuration exceeds 16 KiB");
    std::vector<std::uint8_t> arguments; wire::text(arguments, config);
    execute(wire::Kind::Configure, std::move(arguments), options_.command_timeout);
}
void CaptureAdapter::start(std::uint64_t generation) {
    std::vector<std::uint8_t> arguments; wire::put(arguments, generation);
    execute(wire::Kind::Start, std::move(arguments), options_.command_timeout);
}
void CaptureAdapter::stop() noexcept {
    bool alive;
    { std::lock_guard lock(mutex_); alive = alive_; if (alive && (status_.state == AdapterState::Stopped || status_.state == AdapterState::Loaded)) return; }
    if (!alive) { if (receiver_.joinable()) receiver_.join(); return; }
    try { execute(wire::Kind::Stop, {}, options_.stop_timeout); }
    catch (const std::exception& error) { fault(error.what()); if (receiver_.joinable()) receiver_.join(); }
}
AdapterStatus CaptureAdapter::status() const { std::lock_guard lock(mutex_); return status_; }
void CaptureAdapter::fault(const std::string& reason) {
    AdapterStatus failed;
    {
        std::lock_guard lock(mutex_);
        if (!alive_) return;
        alive_ = false; status_.state = AdapterState::Faulted; ++status_.errors; status_.failure = reason; failed = status_;
    }
    if (!process_.terminate()) { std::lock_guard lock(mutex_); status_.failure += "; OS worker termination was not confirmed"; failed = status_; }
    replied_.notify_all();
    if (journal_) {
        try { journal_->write(source_, "worker_fault", failed); }
        catch (const std::exception& error) { std::lock_guard lock(mutex_); status_.failure += "; " + std::string(error.what()); }
    }
}
void CaptureAdapter::receive() {
    try {
        std::vector<std::uint8_t> packet;
        std::string previous_fault;
        while (wire::receive(process_, packet)) {
            std::span<const std::uint8_t> input(packet); const auto kind = static_cast<wire::Kind>(wire::take<std::uint8_t>(input));
            if (kind == wire::Kind::Frame) {
                auto frame = wire::frame(input); wire::end(input); frame.source = source_; frame.ingest_time_ns = monotonicNowNs();
                pipeline_->submit(std::move(frame));
                continue;
            }
            std::uint64_t id{}; std::string error;
            if (kind == wire::Kind::Reply) { id = wire::take<std::uint64_t>(input); error = wire::text(input); }
            else if (kind != wire::Kind::Status) throw std::runtime_error("unexpected capture worker packet kind");
            auto current = wire::status(input); wire::end(input);
            {
                std::lock_guard lock(mutex_);
                if (!alive_) break;
                status_ = current;
                if (kind == wire::Kind::Reply) { reply_id_ = id; reply_error_ = std::move(error); replied_.notify_all(); }
            }
            if (current.state == AdapterState::Faulted && current.failure != previous_fault) {
                previous_fault = current.failure;
                if (journal_) journal_->write(source_, "plugin_fault", current);
            }
        }
        bool expected; { std::lock_guard lock(mutex_); expected = closing_ || !alive_; }
        if (!expected) {
            process_.wait(std::chrono::milliseconds(100));
            fault("capture worker exited unexpectedly, code=" + std::to_string(process_.exitCode()));
        }
    } catch (const std::exception& error) { fault(error.what()); }
    { std::lock_guard lock(mutex_); alive_ = false; }
    replied_.notify_all();
}
}
