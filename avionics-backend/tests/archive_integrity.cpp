#include "core/archive.hpp"
#include <atomic>
#include <barrier>
#include <iostream>
#include <iterator>
#include <thread>

using namespace avionics;
int main(int argc, char** argv) {
    try {
        if (argc != 2) throw std::runtime_error("expected output directory");
        auto check = [](bool condition, const char* message) { if (!condition) throw std::runtime_error(message); };
        Sha256 empty, abc; const std::array<std::uint8_t, 3> letters{'a','b','c'}; abc.update(letters);
        check(hexIdentity(empty.finish()) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "empty SHA256 vector failed");
        check(hexIdentity(abc.finish()) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "abc SHA256 vector failed");
        RawFrame frame; std::uint64_t count{};
        const auto directory = std::filesystem::path(argv[1]) / ("integrity_" + std::to_string(monotonicNowNs()));
        std::filesystem::create_directories(directory);
        const auto file = directory / "sealed.avbus";
        const auto origin = newSessionId(); ArchiveSummary written;
        {
            ArchiveWriter writer(file); frame = {}; frame.source = "a"; frame.payload = {1,2,3}; frame.origin_session_id = origin;
            frame.origin_source = "previous"; frame.origin_record_index = 99;
            for (std::uint64_t i = 1; i <= 3; ++i) { frame.record_index = i; writer.write(frame); }
            writer.finish(); writer.finish(); written = writer.summary();
        }
        ArchiveReader reader(file); count = 0;
        while (reader.next(frame)) {
            ++count; check(frame.origin_session_id == origin && frame.recording_session_id == written.session_id,
                "archive lineage identity changed");
        }
        check(!reader.next(frame) && count == 3 && reader.summary().file_sha256 == written.file_sha256 &&
            reader.summary().integrity == ArchiveIntegrity::Sealed, "seal roundtrip failed");
        std::ifstream input(file, std::ios::binary);
        const std::vector<std::uint8_t> bytes{std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
        Sha256 whole; whole.update(bytes); check(whole.finish() == *written.file_sha256, "full-file digest excludes bytes");
        std::size_t bad_index{};
        auto rejects = [&](const std::vector<std::uint8_t>& changed) {
            const auto bad = directory / ("bad_" + std::to_string(bad_index++) + ".avbus");
            { std::ofstream output(bad, std::ios::binary); output.write(reinterpret_cast<const char*>(changed.data()), static_cast<std::streamsize>(changed.size())); }
            bool rejected{};
            try { ArchiveReader damaged(bad); while (damaged.next(frame)) {} } catch (const std::runtime_error&) { rejected = true; }
            check(rejected, "damaged or unsealed archive was reported complete");
        };
        auto changed = bytes; changed.resize(bytes.size() - 64); rejects(changed); // missing entire seal
        const std::size_t record_bytes = std::size_t(bytes[24]) + (std::size_t(bytes[25]) << 8) + 8;
        changed = bytes; changed.erase(changed.end() - static_cast<std::ptrdiff_t>(64 + record_bytes), changed.end() - 64); rejects(changed);
        changed.resize(changed.size() - 64); rejects(changed); // whole final record AND seal lost
        changed = bytes; changed[35] ^= 1; rejects(changed);
        changed = bytes; changed.back() ^= 1; rejects(changed);
        changed = bytes; changed.push_back(0); rejects(changed);
        for (std::size_t n = 1; n < 64; ++n) { changed = bytes; changed.resize(bytes.size() - n); rejects(changed); }
        const auto unfinished = directory / "unfinished.avbus";
        { ArchiveWriter writer(unfinished); frame = {}; frame.source = "a"; frame.record_index = 1; writer.write(frame); writer.flush(); }
        bool rejected{};
        try { ArchiveReader pending(unfinished); while (pending.next(frame)) {} } catch (const std::runtime_error&) { rejected = true; }
        check(rejected, "unfinalized writer produced a sealed archive");
        std::barrier ready(2); std::atomic<unsigned> winners{}, collisions{};
        auto compete = [&] {
            ready.arrive_and_wait();
            try { ArchiveWriter writer(directory / "exclusive.avbus"); writer.finish(); ++winners; }
            catch (const std::system_error&) { ++collisions; }
        };
        std::thread first(compete), second(compete); first.join(); second.join();
        check(winners == 1 && collisions == 1, "concurrent writers overwrote evidence");
        std::cout << "sealed_records=3 tamper_cases=" << bad_index << " exclusive_writers=1\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
