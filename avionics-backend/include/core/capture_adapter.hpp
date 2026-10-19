#pragma once
#include "core/plugin_manager.hpp"
#include "core/frame_pipeline.hpp"
#include "core/session_journal.hpp"
#include "core/worker_process.hpp"
#include "core/worker_wire.hpp"
#include <condition_variable>
#include <thread>

namespace avionics {
struct CaptureOptions {
    std::filesystem::path executable;
    std::chrono::milliseconds command_timeout{5000}, stop_timeout{1000};
};
class CaptureAdapter final : public IBusAdapter {
public:
    CaptureAdapter(const CaptureOptions&, const std::filesystem::path& plugin, std::string source,
        std::shared_ptr<FramePipeline>, std::shared_ptr<SessionJournal>);
    ~CaptureAdapter() override;
    void configure(const std::string&) override;
    void start(std::uint64_t generation) override;
    void stop() noexcept override;
    AdapterStatus status() const override;
private:
    void execute(wire::Kind, std::vector<std::uint8_t>, std::chrono::milliseconds);
    void receive();
    void fault(const std::string&);
    CaptureOptions options_;
    WorkerProcess process_;
    std::string source_;
    std::shared_ptr<FramePipeline> pipeline_;
    std::shared_ptr<SessionJournal> journal_;
    mutable std::mutex mutex_;
    std::mutex command_mutex_;
    std::condition_variable replied_;
    AdapterStatus status_;
    bool alive_{true}, closing_{};
    std::uint64_t next_request_{}, reply_id_{};
    std::string reply_error_;
    std::thread receiver_;
};
}
