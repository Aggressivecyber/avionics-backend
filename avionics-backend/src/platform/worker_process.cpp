#include "core/worker_process.hpp"
#include <atomic>
#include <mutex>
#include <system_error>
#include <thread>
#include <vector>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <csignal>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

namespace avionics {
#if defined(_WIN32)
namespace {
struct Handle {
    HANDLE value{};
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
};
void checked(BOOL success, const char* operation) {
    if (!success) throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), operation);
}
struct Attributes {
    std::vector<std::uint8_t> bytes;
    LPPROC_THREAD_ATTRIBUTE_LIST list;
    Attributes() {
        SIZE_T size{}; InitializeProcThreadAttributeList(nullptr, 2, 0, &size); bytes.resize(size);
        list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(bytes.data());
        checked(InitializeProcThreadAttributeList(list, 2, 0, &size), "initialize worker attributes");
    }
    ~Attributes() { DeleteProcThreadAttributeList(list); }
};
}
struct WorkerProcess::State {
    Handle read_pipe, write_pipe, process, job, cancelled, read_event, write_event;
    State() {
        cancelled.value = CreateEventW(nullptr, TRUE, FALSE, nullptr); checked(cancelled.value != nullptr, "create cancellation event");
        read_event.value = CreateEventW(nullptr, TRUE, FALSE, nullptr); checked(read_event.value != nullptr, "create IPC read event");
        write_event.value = CreateEventW(nullptr, TRUE, FALSE, nullptr); checked(write_event.value != nullptr, "create IPC write event");
    }
    ~State() { if (process.value) { TerminateJobObject(job.value, 255); WaitForSingleObject(process.value, 2000); } }
};
WorkerProcess::WorkerProcess(const std::filesystem::path& executable) : state_(std::make_unique<State>()) {
    static std::atomic<std::uint64_t> serial{};
    const auto name = L"\\\\.\\pipe\\avbus-" + std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(++serial);
    const auto frames_name = name + L"-frames", commands_name = name + L"-commands";
    state_->read_pipe.value = CreateNamedPipeW(frames_name.c_str(), PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1, 131072, 65536, 0, nullptr);
    checked(state_->read_pipe.value != INVALID_HANDLE_VALUE, "create worker frame pipe");
    state_->write_pipe.value = CreateNamedPipeW(commands_name.c_str(), PIPE_ACCESS_OUTBOUND | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1, 65536, 65536, 0, nullptr);
    checked(state_->write_pipe.value != INVALID_HANDLE_VALUE, "create worker command pipe");
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    Handle input, output, errors;
    input.value = CreateFileW(commands_name.c_str(), GENERIC_READ, 0, &security, OPEN_EXISTING, 0, nullptr);
    checked(input.value != INVALID_HANDLE_VALUE, "open worker command pipe");
    output.value = CreateFileW(frames_name.c_str(), GENERIC_WRITE, 0, &security, OPEN_EXISTING, 0, nullptr);
    checked(output.value != INVALID_HANDLE_VALUE, "open worker frame pipe");
    checked(DuplicateHandle(GetCurrentProcess(), GetStdHandle(STD_ERROR_HANDLE), GetCurrentProcess(), &errors.value,
        0, TRUE, DUPLICATE_SAME_ACCESS), "duplicate worker diagnostics");
    state_->job.value = CreateJobObjectW(nullptr, nullptr); checked(state_->job.value != nullptr, "create worker job");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{}; limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    checked(SetInformationJobObject(state_->job.value, JobObjectExtendedLimitInformation, &limits, sizeof(limits)), "set worker job lifetime");
    Attributes attributes;
    HANDLE inherited[]{input.value, output.value, errors.value};
    checked(UpdateProcThreadAttribute(attributes.list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherited, sizeof(inherited), nullptr, nullptr), "restrict inherited worker handles");
    checked(UpdateProcThreadAttribute(attributes.list, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, &state_->job.value, sizeof(HANDLE), nullptr, nullptr), "assign worker job at creation");
    STARTUPINFOEXW startup{}; startup.StartupInfo.cb = sizeof(startup); startup.lpAttributeList = attributes.list;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input.value; startup.StartupInfo.hStdOutput = output.value; startup.StartupInfo.hStdError = errors.value;
    PROCESS_INFORMATION created{};
    auto command = L"\"" + std::filesystem::absolute(executable).wstring() + L"\"";
    checked(CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, TRUE,
        CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT, nullptr, nullptr, &startup.StartupInfo, &created), "launch capture worker");
    state_->process.value = created.hProcess; Handle thread; thread.value = created.hThread;
}
bool WorkerProcess::read(std::span<std::uint8_t> bytes) {
    const auto event = state_->read_event.value;
    while (!bytes.empty()) {
        OVERLAPPED operation{}; operation.hEvent = event; ResetEvent(event); DWORD count{};
        if (!ReadFile(state_->read_pipe.value, bytes.data(), static_cast<DWORD>(bytes.size()), &count, &operation)) {
            const auto error = GetLastError();
            if (error == ERROR_BROKEN_PIPE || error == ERROR_PIPE_NOT_CONNECTED) return false;
            if (error != ERROR_IO_PENDING) checked(FALSE, "read worker IPC");
            const HANDLE waits[]{event, state_->cancelled.value};
            const auto result = WaitForMultipleObjects(2, waits, FALSE, INFINITE);
            if (result != WAIT_OBJECT_0) {
                CancelIoEx(state_->read_pipe.value, &operation); GetOverlappedResult(state_->read_pipe.value, &operation, &count, TRUE);
                if (result == WAIT_OBJECT_0 + 1) return false;
                checked(FALSE, "wait for worker IPC");
            }
            if (!GetOverlappedResult(state_->read_pipe.value, &operation, &count, FALSE)) {
                const auto reason = GetLastError();
                if (reason == ERROR_BROKEN_PIPE || reason == ERROR_PIPE_NOT_CONNECTED || reason == ERROR_OPERATION_ABORTED) return false;
                checked(FALSE, "complete worker IPC read");
            }
        }
        if (!count) return false;
        bytes = bytes.subspan(count);
    }
    return true;
}
void WorkerProcess::write(std::span<const std::uint8_t> bytes, std::chrono::milliseconds timeout) {
    const auto event = state_->write_event.value; ResetEvent(event);
    OVERLAPPED operation{}; operation.hEvent = event; DWORD count{};
    if (!WriteFile(state_->write_pipe.value, bytes.data(), static_cast<DWORD>(bytes.size()), &count, &operation)) {
        if (GetLastError() != ERROR_IO_PENDING) checked(FALSE, "send worker command");
        const auto result = WaitForSingleObject(event, static_cast<DWORD>(timeout.count()));
        if (result != WAIT_OBJECT_0) {
            CancelIoEx(state_->write_pipe.value, &operation); GetOverlappedResult(state_->write_pipe.value, &operation, &count, TRUE);
            throw std::runtime_error("worker command write deadline exceeded");
        }
        checked(GetOverlappedResult(state_->write_pipe.value, &operation, &count, FALSE), "complete worker command write");
    }
    if (count != bytes.size()) throw std::runtime_error("short worker command write");
}
bool WorkerProcess::wait(std::chrono::milliseconds timeout) {
    const auto result = WaitForSingleObject(state_->process.value, static_cast<DWORD>(timeout.count()));
    if (result == WAIT_FAILED) checked(FALSE, "wait for capture worker exit");
    return result == WAIT_OBJECT_0;
}
bool WorkerProcess::terminate() noexcept {
    SetEvent(state_->cancelled.value);
    return TerminateJobObject(state_->job.value, 255) && WaitForSingleObject(state_->process.value, 2000) == WAIT_OBJECT_0;
}
std::uint32_t WorkerProcess::exitCode() const {
    DWORD code{}; checked(GetExitCodeProcess(state_->process.value, &code), "query worker exit"); return code;
}
std::filesystem::path executableDirectory() {
    std::vector<wchar_t> path(32768); const auto count = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
    if (!count || count == path.size()) throw std::runtime_error("cannot locate running executable");
    return std::filesystem::path(std::wstring(path.data(), count)).parent_path();
}
#else
namespace {
void posixChecked(int result, const char* operation) {
    if (result) throw std::system_error(result, std::generic_category(), operation);
}
struct SpawnSetup {
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attributes;
    SpawnSetup() {
        posixChecked(posix_spawn_file_actions_init(&actions), "initialize worker file actions");
        const auto result = posix_spawnattr_init(&attributes);
        if (result) { posix_spawn_file_actions_destroy(&actions); posixChecked(result, "initialize worker attributes"); }
    }
    ~SpawnSetup() { posix_spawn_file_actions_destroy(&actions); posix_spawnattr_destroy(&attributes); }
};
struct FileDescriptor { int value; ~FileDescriptor() { close(value); } };
}
struct WorkerProcess::State {
    int socket{-1}; pid_t child{-1}; int status{}; bool reaped{};
    std::mutex reaping_mutex;
    ~State() { if (child > 0 && !reaped) { kill(-child, SIGKILL); waitpid(child, &status, 0); } if (socket >= 0) close(socket); }
};
WorkerProcess::WorkerProcess(const std::filesystem::path& executable) : state_(std::make_unique<State>()) {
    int sockets[2]; if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, sockets)) throw std::system_error(errno, std::generic_category(), "create worker socket");
    state_->socket = sockets[0]; FileDescriptor child_socket{sockets[1]}; SpawnSetup setup;
    posixChecked(posix_spawn_file_actions_adddup2(&setup.actions, sockets[1], STDIN_FILENO), "redirect worker input");
    posixChecked(posix_spawn_file_actions_adddup2(&setup.actions, sockets[1], STDOUT_FILENO), "redirect worker output");
    posixChecked(posix_spawn_file_actions_addclose(&setup.actions, sockets[0]), "close parent socket in worker");
    posixChecked(posix_spawn_file_actions_addclose(&setup.actions, sockets[1]), "close duplicated worker socket");
    posixChecked(posix_spawnattr_setflags(&setup.attributes, POSIX_SPAWN_SETPGROUP), "set worker process group flag");
    posixChecked(posix_spawnattr_setpgroup(&setup.attributes, 0), "set worker process group");
    const auto path = executable.string(); char* arguments[]{const_cast<char*>(path.c_str()), nullptr};
    const auto error = posix_spawn(&state_->child, path.c_str(), &setup.actions, &setup.attributes, arguments, environ);
    if (error) { state_->child = -1; throw std::system_error(error, std::generic_category(), "launch capture worker"); }
}
bool WorkerProcess::read(std::span<std::uint8_t> bytes) {
    while (!bytes.empty()) {
        const auto count = recv(state_->socket, bytes.data(), bytes.size(), 0);
        if (!count) return false;
        if (count < 0) { if (errno == EINTR) continue; throw std::system_error(errno, std::generic_category(), "read worker IPC"); }
        bytes = bytes.subspan(static_cast<std::size_t>(count));
    }
    return true;
}
void WorkerProcess::write(std::span<const std::uint8_t> bytes, std::chrono::milliseconds timeout) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (!bytes.empty()) {
        pollfd writable{state_->socket, POLLOUT, 0};
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0) throw std::runtime_error("worker command write deadline exceeded");
        const auto ready = poll(&writable, 1, static_cast<int>(remaining));
        if (ready < 0) { if (errno == EINTR) continue; throw std::system_error(errno, std::generic_category(), "wait for worker socket"); }
        if (!ready) throw std::runtime_error("worker command write deadline exceeded");
        const auto count = send(state_->socket, bytes.data(), bytes.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
        if (count < 0) { if (errno == EINTR || errno == EAGAIN) continue; throw std::system_error(errno, std::generic_category(), "send worker command"); }
        if (!count) throw std::runtime_error("worker socket closed during command write");
        bytes = bytes.subspan(static_cast<std::size_t>(count));
    }
}
bool WorkerProcess::wait(std::chrono::milliseconds timeout) {
    std::lock_guard lock(state_->reaping_mutex);
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    do {
        if (state_->reaped) return true;
        const auto result = waitpid(state_->child, &state_->status, WNOHANG);
        if (result == state_->child) { state_->reaped = true; return true; }
        if (result < 0 && errno != EINTR) throw std::system_error(errno, std::generic_category(), "wait for worker");
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    } while (std::chrono::steady_clock::now() < deadline);
    return false;
}
bool WorkerProcess::terminate() noexcept {
    std::lock_guard lock(state_->reaping_mutex);
    shutdown(state_->socket, SHUT_RDWR);
    if (state_->reaped) return true;
    if (kill(-state_->child, SIGKILL) && errno != ESRCH) return false;
    pid_t result;
    do { result = waitpid(state_->child, &state_->status, 0); } while (result < 0 && errno == EINTR);
    state_->reaped = result == state_->child; return state_->reaped;
}
std::uint32_t WorkerProcess::exitCode() const {
    std::lock_guard lock(state_->reaping_mutex);
    return state_->reaped ? static_cast<std::uint32_t>(WIFEXITED(state_->status) ? WEXITSTATUS(state_->status) : 128 + WTERMSIG(state_->status)) : 259;
}
std::filesystem::path executableDirectory() { return std::filesystem::canonical("/proc/self/exe").parent_path(); }
#endif
WorkerProcess::~WorkerProcess() = default;
}
