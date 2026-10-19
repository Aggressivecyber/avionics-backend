#include "core/identity.hpp"
#include <algorithm>
#include <limits>
#include <stdexcept>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#else
#include <openssl/evp.h>
#include <openssl/rand.h>
#endif

namespace avionics {
struct Sha256::State {
#if defined(_WIN32)
    BCRYPT_ALG_HANDLE algorithm{};
    BCRYPT_HASH_HANDLE hash{};
    State() {
        auto result = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
        if (result < 0) throw std::runtime_error("SHA256 provider failed: " + std::to_string(result));
        result = BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0);
        if (result < 0) {
            BCryptCloseAlgorithmProvider(algorithm, 0);
            throw std::runtime_error("SHA256 creation failed: " + std::to_string(result));
        }
    }
    ~State() { BCryptDestroyHash(hash); BCryptCloseAlgorithmProvider(algorithm, 0); }
#else
    EVP_MD_CTX* hash;
    State() : hash(EVP_MD_CTX_new()) {
        if (!hash) throw std::bad_alloc();
        if (EVP_DigestInit_ex(hash, EVP_sha256(), nullptr) != 1) {
            EVP_MD_CTX_free(hash); throw std::runtime_error("SHA256 initialization failed");
        }
    }
    ~State() { EVP_MD_CTX_free(hash); }
#endif
};
Sha256::Sha256() : state_(std::make_unique<State>()) {}
Sha256::~Sha256() = default;
void Sha256::update(std::span<const std::uint8_t> bytes) {
#if defined(_WIN32)
    while (!bytes.empty()) {
        const auto count = static_cast<ULONG>(std::min(bytes.size(), static_cast<std::size_t>(std::numeric_limits<ULONG>::max())));
        const auto result = BCryptHashData(state_->hash, const_cast<PUCHAR>(bytes.data()), count, 0);
        if (result < 0) throw std::runtime_error("SHA256 update failed: " + std::to_string(result));
        bytes = bytes.subspan(count);
    }
#else
    if (EVP_DigestUpdate(state_->hash, bytes.data(), bytes.size()) != 1) throw std::runtime_error("SHA256 update failed");
#endif
}
ContentDigest Sha256::finish() {
    ContentDigest result{};
#if defined(_WIN32)
    const auto status = BCryptFinishHash(state_->hash, result.data(), static_cast<ULONG>(result.size()), 0);
    if (status < 0) throw std::runtime_error("SHA256 finalization failed: " + std::to_string(status));
#else
    unsigned length{};
    if (EVP_DigestFinal_ex(state_->hash, result.data(), &length) != 1) throw std::runtime_error("SHA256 finalization failed");
#endif
    return result;
}
std::string hexIdentity(std::span<const std::uint8_t> bytes) {
    constexpr char digits[] = "0123456789abcdef";
    std::string result; result.reserve(bytes.size() * 2);
    for (auto byte : bytes) { result.push_back(digits[byte >> 4]); result.push_back(digits[byte & 15]); }
    return result;
}
SessionId newSessionId() {
    SessionId result{};
#if defined(_WIN32)
    const auto status = BCryptGenRandom(nullptr, result.data(), static_cast<ULONG>(result.size()), BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    if (status < 0) throw std::runtime_error("session identity generation failed: " + std::to_string(status));
#else
    if (RAND_bytes(result.data(), static_cast<int>(result.size())) != 1) throw std::runtime_error("session identity generation failed");
#endif
    result[6] = static_cast<std::uint8_t>((result[6] & 15u) | 64u);
    result[8] = static_cast<std::uint8_t>((result[8] & 63u) | 128u);
    return result;
}
}
