#pragma once
#include <array>
#include <cstdint>
#include <memory>
#include <span>
#include <string>

namespace avionics {
using ContentDigest = std::array<std::uint8_t, 32>;
using SessionId = std::array<std::uint8_t, 16>;
class Sha256 {
public:
    Sha256();
    ~Sha256();
    Sha256(const Sha256&) = delete;
    Sha256& operator=(const Sha256&) = delete;
    void update(std::span<const std::uint8_t>);
    ContentDigest finish();
private:
    struct State;
    std::unique_ptr<State> state_;
};
std::string hexIdentity(std::span<const std::uint8_t>);
SessionId newSessionId();
}
