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
        const auto split = directory / "split.avbus";
        ArchiveSummary split_summary;
        {
            ArchiveWriter writer(split, std::nullopt, {350, 0, false});
            frame = {}; frame.source = "sensor"; frame.payload = {1,2,3};
            for (std::uint64_t i = 1; i <= 10; ++i) { frame.record_index = i; writer.write(frame); }
            writer.finish(); split_summary = writer.summary();
        }
        check(split_summary.volumes == 5, "size rotation failed");
        ArchiveReader split_reader(split); count = 0;
        while (split_reader.next(frame)) check(frame.record_index == ++count, "cross-volume record identity changed");
        check(count == 10 && split_reader.summary().file_sha256 == split_summary.file_sha256, "split roundtrip failed");
        Sha256 concatenated;
        for (std::uint32_t n = 1; n <= split_summary.volumes; ++n) {
            const auto path = archiveVolumePath(split, n);
            check(std::filesystem::file_size(path) <= 350, "volume exceeds configured size");
            std::ifstream stream(path, std::ios::binary);
            const std::vector<std::uint8_t> content{std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
            concatenated.update(content);
        }
        check(concatenated.finish() == split_summary.file_sha256, "volume-set digest excludes a volume");
        const auto last_volume = archiveVolumePath(split, split_summary.volumes);
        const auto last_size = std::filesystem::file_size(last_volume);
        std::filesystem::resize_file(last_volume, last_size - 65 - 10); // torn second record
        const auto inspection = checkArchive(split);
        check(!inspection.complete && inspection.archive.records == 9, "recovery scan crossed a torn record");
        const auto restored = directory / "restored.avbus";
        const auto recovery = recoverArchive(split, restored);
        check(!recovery.complete && std::filesystem::file_size(last_volume) == last_size - 75, "recovery changed the source");
        const auto restored_check = checkArchive(restored);
        check(restored_check.complete && restored_check.archive.recovered && restored_check.archive.records == 9 &&
            restored_check.archive.session_id == split_summary.session_id, "recovery lost identity or completeness distinction");
        const auto missing = archiveVolumePath(split, 2);
        std::filesystem::rename(missing, directory / "withheld.avbus");
        check(!checkArchive(split).complete && checkArchive(split).archive.records == 2, "missing volume was ignored");
        std::filesystem::copy_file(archiveVolumePath(split, 3), missing);
        check(!checkArchive(split).complete && checkArchive(split).archive.records == 2, "reordered volume was accepted");
        std::filesystem::copy_file(directory / "withheld.avbus", missing, std::filesystem::copy_options::overwrite_existing);
        {
            std::fstream mixed(missing, std::ios::binary | std::ios::in | std::ios::out);
            mixed.seekg(28); char value{}; mixed.get(value); value ^= 1; mixed.seekp(28); mixed.put(value);
        }
        check(!checkArchive(split).complete && checkArchive(split).archive.records == 2, "previous-volume digest mismatch was accepted");
        const auto timed = directory / "timed.avbus";
        {
            ArchiveWriter writer(timed, std::nullopt, {0, 1, false});
            frame = {}; frame.source = "clock"; frame.record_index = 1; writer.write(frame);
            std::this_thread::sleep_for(std::chrono::milliseconds(1050));
            frame.record_index = 2; writer.write(frame); writer.finish();
            check(writer.summary().volumes == 2, "time rotation failed");
        }
        check(checkArchive(timed).complete, "timed volumes did not seal");
        for (std::size_t cut = 24; cut < bytes.size(); ++cut) {
            const auto torn = directory / "torn.avbus";
            { std::ofstream out(torn, std::ios::binary); out.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(cut)); }
            const auto scan = checkArchive(torn);
            check(!scan.complete && scan.archive.records == std::min<std::uint64_t>(3, (cut - 24) / record_bytes),
                "byte-boundary truncation recovered an incomplete record");
        }
        std::cout << "sealed_records=3 tamper_cases=" << bad_index << " exclusive_writers=1\n";
        std::cout << "size_volumes=5 timed_volumes=2 recovered_records=9 byte_cuts=" << bytes.size() - 24 << '\n';
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
