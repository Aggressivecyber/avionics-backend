#pragma once
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <span>

namespace avionics {
class WorkerProcess {
public:
    explicit WorkerProcess(const std::filesystem::path& executable);
    ~WorkerProcess();
    bool read(std::span<std::uint8_t>); // exact bytes, false only on EOF/cancellation
    void write(std::span<const std::uint8_t>, std::chrono::milliseconds timeout);
    bool wait(std::chrono::milliseconds timeout);
    bool terminate() noexcept;
    std::uint32_t exitCode() const;
private:
    struct State;
    std::unique_ptr<State> state_;
};
std::filesystem::path executableDirectory();
}
