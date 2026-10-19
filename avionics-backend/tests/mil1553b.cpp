#include "core/mil1553b.hpp"
#include "plugin_api/bus_plugin.h"
#include "plugin_api/mil1553b_capture.hpp"
#include <fstream>
#include <iostream>

using namespace avionics;
using namespace avionics::mil1553b;
namespace {
void check(bool value, const char* reason) { if (!value) throw std::runtime_error(reason); }
RawFrame fixture(std::initializer_list<CapturedWord> words, std::uint8_t bus = 0) {
    RawFrame frame; frame.source = "bus1553"; frame.generation = 7; frame.protocol = BUS_PROTOCOL_MIL1553B;
    frame.capture_time_ns = 1000000; frame.ingest_time_ns = 1000100; frame.record_index = 42; frame.sequence = 9;
    frame.clock_domain = BUS_CLOCK_HOST_MONOTONIC;
    frame.recording_session_id = SessionId{1};
    frame.payload = encodeCapture(bus, {words.begin(), words.size()});
    return frame;
}
ProtocolParseResult parse(const RawFrame& frame) { return Mil1553BHandler(protocolStreamOf(frame)).parse(frame); }
const ParameterSample& sample(const std::vector<ParameterSample>& samples, const std::string& id) {
    for (const auto& item : samples) if (item.parameter_id == id) return item;
    throw std::runtime_error("missing sample: " + id);
}
}
int main(int argc, char** argv) {
    try {
        check(argc == 3, "expected configuration and output directory");
        // Fixed words and parity bits are golden inputs, not built with the plugin's command generator.
        const auto receive = fixture({{0x1822, Sync::CommandStatus, 1}, {0x03e8, Sync::Data, 1},
            {0x07d0, Sync::Data, 1}, {0x1800, Sync::CommandStatus, 1}});
        const auto transmit = fixture({{0x2442, Sync::CommandStatus, 1}, {0x2000, Sync::CommandStatus, 0},
            {0x03e8, Sync::Data, 1}, {0x07d0, Sync::Data, 1}}, 1);
        const auto rt_rt = fixture({{0x2861, Sync::CommandStatus, 0}, {0x2461, Sync::CommandStatus, 0},
            {0x2000, Sync::CommandStatus, 0}, {0x07da, Sync::Data, 1}, {0x2800, Sync::CommandStatus, 1}});
        const auto broadcast = fixture({{0xf881, Sync::CommandStatus, 0}, {0x1234, Sync::Data, 0}});
        auto command = decode1553BCommand(0x2442);
        check(command.rt_address == 4 && command.subaddress == 2 && command.transmit && command.wordCount() == 2 && !command.isMode(), "command fields");
        const auto receive_result = parse(receive);
        check(receive_result.status == ProtocolParseStatus::Complete && receive_result.messages.front().message_kind == "bc_to_rt", "BC-to-RT golden message");
        check(receive_result.messages.front().payload == std::vector<std::uint8_t>({3,232,7,208}), "data words must retain MSB-first payload");
        check(receive_result.messages.front().evidence.front().record_index == 42, "raw evidence index");
        check(parse(transmit).messages.front().message_kind == "rt_to_bc", "RT-to-BC golden message");
        check(parse(rt_rt).messages.front().message_kind == "rt_to_rt", "RT-to-RT golden message");
        check(parse(broadcast).messages.front().message_kind == "bc_broadcast", "broadcast must not require receiving status");
        const auto rt_broadcast = fixture({{0xf861, Sync::CommandStatus, 1}, {0x2461, Sync::CommandStatus, 0},
            {0x2000, Sync::CommandStatus, 0}, {0x07da, Sync::Data, 1}});
        check(parse(rt_broadcast).status == ProtocolParseStatus::Complete && parse(rt_broadcast).messages.front().message_kind == "rt_broadcast", "RT broadcast format");
        std::vector<CapturedWord> full{syntheticWord(0x1820, Sync::CommandStatus)};
        for (std::uint16_t i = 0; i < 32; ++i) full.push_back(syntheticWord(i, Sync::Data));
        full.push_back(syntheticWord(0x1800, Sync::CommandStatus));
        auto maximum = receive; maximum.payload = encodeCapture(0, full);
        check(decode1553BCommand(0x1820).wordCount() == 32 && parse(maximum).messages.front().payload.size() == 64, "zero word-count field means 32");
        std::size_t rejected{};
        for (std::size_t length = 0; length < receive.payload.size(); ++length) {
            auto truncated = receive; truncated.payload.resize(length);
            check(parse(truncated).status == ProtocolParseStatus::Malformed, "truncated envelope accepted"); ++rejected;
        }
        for (auto [index, value] : std::vector<std::pair<std::size_t, std::uint8_t>>{{0,0},{8,2},{9,37},{10,1},{14,2},{15,0},{18,0}}) {
            auto bad = receive; bad.payload[index] = value;
            check(parse(bad).status == ProtocolParseStatus::Malformed, "invalid envelope/parity/sync accepted"); ++rejected;
        }
        const auto wrong_status = fixture({{0x1822, Sync::CommandStatus, 1}, {0x03e8, Sync::Data, 1},
            {0x07d0, Sync::Data, 1}, {0x2000, Sync::CommandStatus, 0}});
        check(parse(wrong_status).status == ProtocolParseStatus::Malformed, "wrong status RT accepted");
        const auto extra_broadcast_status = fixture({{0xf881, Sync::CommandStatus, 0}, {0x1234, Sync::Data, 0}, {0x1800, Sync::CommandStatus, 1}});
        check(parse(extra_broadcast_status).status == ProtocolParseStatus::Malformed, "broadcast receiving status accepted");
        // MIL-STD-1553B figure 3: BCR=0x0010, Busy=0x0008, Subsystem Flag=0x0004.
        const auto busy = fixture({{0x2442, Sync::CommandStatus, 1}, {0x2008, Sync::CommandStatus, 1}});
        check(parse(busy).status == ProtocolParseStatus::Complete && parse(busy).messages.front().payload.empty() &&
            (parse(busy).messages.front().flags & BUS_FRAME_INVALID), "busy RT must return status only with invalid application data");
        const auto prior_broadcast = fixture({{0x2442, Sync::CommandStatus, 1}, {0x2010, Sync::CommandStatus, 1},
            {0x03e8, Sync::Data, 1}, {0x07d0, Sync::Data, 1}});
        check(parse(prior_broadcast).status == ProtocolParseStatus::Complete &&
            !(parse(prior_broadcast).messages.front().flags & BUS_FRAME_INVALID), "broadcast-received flag must not be treated as Busy");
        const auto subsystem_fault = fixture({{0x2442, Sync::CommandStatus, 1}, {0x2004, Sync::CommandStatus, 1},
            {0x03e8, Sync::Data, 1}, {0x07d0, Sync::Data, 1}});
        check(parse(subsystem_fault).status == ProtocolParseStatus::Complete &&
            (parse(subsystem_fault).messages.front().flags & BUS_FRAME_INVALID), "subsystem flag must invalidate potentially faulty application data");
        const auto rt_busy = fixture({{0x2861, Sync::CommandStatus, 0}, {0x2461, Sync::CommandStatus, 0},
            {0x2008, Sync::CommandStatus, 1}});
        check(parse(rt_busy).status == ProtocolParseStatus::Unsupported, "RT-to-RT Busy must remain explicitly unsupported");
        const auto refused = fixture({{0x2442, Sync::CommandStatus, 1}, {0x2400, Sync::CommandStatus, 1}});
        check(parse(refused).status == ProtocolParseStatus::Complete && (parse(refused).messages.front().flags & BUS_FRAME_INVALID), "message-error RT response");
        auto reserved_status = receive;
        reserved_status.payload = encodeCapture(0, std::vector<CapturedWord>{syntheticWord(0x1822, Sync::CommandStatus),
            syntheticWord(0x03e8, Sync::Data), syntheticWord(0x07d0, Sync::Data), syntheticWord(0x1a00, Sync::CommandStatus)});
        check(parse(reserved_status).status == ProtocolParseStatus::Malformed, "nonzero instrumentation status bit accepted");
        const auto mode = fixture({{0x1c01, Sync::CommandStatus, 1}, {0x1800, Sync::CommandStatus, 1}});
        check(parse(mode).status == ProtocolParseStatus::Unsupported, "mode command must be explicit Unsupported");
        auto restarted = receive; ++restarted.generation;
        Mil1553BHandler original(protocolStreamOf(receive));
        check(original.parse(restarted).status == ProtocolParseStatus::Malformed, "handler accepted another generation");
        Mil1553BRouter router;
        const auto address = protocolStreamOf(receive).address;
        check(router.selection(address).mode() == ProtocolSelectionMode::Automatic, "default automatic policy");
        check(router.route(receive).detection.status == ProtocolDetectionStatus::Rejected, "protocol number alone identified a capture format");
        router.setInputDescriptor(address, {{}, false, capture_representation});
        check(router.route(receive).parsing.status == ProtocolParseStatus::Complete, "automatic monitor-envelope identification");
        router.setSelection(address, ProtocolSelection::manual(BUS_PROTOCOL_MIL1553B));
        auto parity_bad = receive; parity_bad.payload[15] ^= 1;
        const auto manual_bad = router.route(parity_bad);
        check(manual_bad.detection.basis == ProtocolSelectionBasis::Manual && manual_bad.detection.selected == BUS_PROTOCOL_MIL1553B &&
            manual_bad.parsing.status == ProtocolParseStatus::Malformed, "manual selection switched after bad frame");
        check(router.route(restarted).parsing.status == ProtocolParseStatus::Complete, "new generation failed to create its own handler");
        check(router.selection(address).mode() == ProtocolSelectionMode::Manual, "restart lost manual policy");
        bool unknown_rejected{};
        try { router.setSelection(address, ProtocolSelection::manual(429)); } catch (const std::invalid_argument&) { unknown_rejected = true; }
        check(unknown_rejected && router.selection(address).protocol() == BUS_PROTOCOL_MIL1553B, "unknown manual protocol changed policy");
        auto limits = ProtocolRoutingLimits{}; limits.max_streams = 1; router.setLimits(limits);
        check(router.route(receive).parsing.status == ProtocolParseStatus::Complete, "first budgeted stream");
        auto other_channel = receive; other_channel.channel = 2;
        const auto second_address = protocolStreamOf(other_channel).address;
        bool budget_denied{};
        try { router.setInputDescriptor(second_address, {{}, false, capture_representation}); } catch (const std::invalid_argument&) { budget_denied = true; }
        check(budget_denied && router.selection(second_address).mode() == ProtocolSelectionMode::Automatic, "input descriptor budget or channel policy isolation");
        router.removeStream(address);
        router.setInputDescriptor(second_address, {{}, false, capture_representation});
        check(router.route(other_channel).parsing.status == ProtocolParseStatus::Complete, "remove did not release stream budget");
        auto replay_stream = other_channel; replay_stream.origin_source = "original"; replay_stream.origin_generation = 1;
        check(router.route(replay_stream).detection.status == ProtocolDetectionStatus::Rejected, "replay origin bypassed stream budget");
        Mil1553BFactory factory;
        check(factory.createDetector(BUS_PROTOCOL_MIL1553B)->protocolId() == BUS_PROTOCOL_MIL1553B && !factory.createHandler(429, {}), "protocol factory contract");
        auto config = BackendConfiguration::load(argv[1]);
        ProtocolPipelineDecoder pipeline(config);
        const auto received = pipeline.decode(receive);
        check(received.size() == 3 && std::get<double>(sample(received, "command").value) == 1 &&
            std::get<double>(sample(received, "temperature_a").value) == 20, "RT receive selector and engineering conversion");
        auto untagged = receive; untagged.protocol = 0;
        check(sample(pipeline.decode(untagged), "command").valid, "explicit monitor representation was not recognized without a protocol number");
        const auto other_rt = fixture({syntheticWord(0x3822, Sync::CommandStatus), syntheticWord(0x03e8, Sync::Data),
            syntheticWord(0x07d0, Sync::Data), syntheticWord(0x3800, Sync::CommandStatus)});
        const auto other_subaddress = fixture({syntheticWord(0x1842, Sync::CommandStatus), syntheticWord(0x03e8, Sync::Data),
            syntheticWord(0x07d0, Sync::Data), syntheticWord(0x1800, Sync::CommandStatus)});
        const auto other_direction = fixture({syntheticWord(0x1c22, Sync::CommandStatus), syntheticWord(0x1800, Sync::CommandStatus),
            syntheticWord(0x03e8, Sync::Data), syntheticWord(0x07d0, Sync::Data)});
        check(pipeline.decode(other_rt).size() == 1 && pipeline.decode(other_subaddress).size() == 1 &&
            pipeline.decode(other_direction).size() == 1, "unrelated RT/subaddress/direction leaked into engineering dictionary");
        const auto transmitted = pipeline.decode(transmit);
        check(transmitted.size() == 2 && std::get<double>(sample(transmitted, "position").value) == 1, "RT transmit selector");
        const auto routed = pipeline.decode(rt_rt);
        check(routed.size() == 3 && std::get<double>(sample(routed, "temperature_b").value) == 20.1 &&
            std::get<double>(sample(routed, "received_temperature").value) == 20.1, "RT-to-RT endpoint selectors");
        check(std::get<std::uint64_t>(sample(pipeline.decode(broadcast), "broadcast_counter").value) == 0x1234, "broadcast dictionary selector");
        const auto invalid = pipeline.decode(parity_bad);
        check(invalid.size() == 7 && std::get<EnumValue>(invalid.front().value).code == 1, "bad parity diagnostic or invalidation missing");
        for (const auto& item : invalid) check(!item.valid, "bad parity produced valid data or a watermark-advancing diagnostic");
        const auto busy_samples = pipeline.decode(busy);
        check(!sample(busy_samples, "position").valid && std::get<EnumValue>(busy_samples.front().value).code == 4, "busy data reported valid");
        const auto fault_samples = pipeline.decode(subsystem_fault);
        check(!sample(fault_samples, "position").valid && std::get<EnumValue>(fault_samples.front().value).code == 4, "subsystem-fault data reported valid");
        check(sample(pipeline.decode(prior_broadcast), "position").valid, "broadcast-received flag discarded normal data");
        auto replay = receive; replay.origin_source = "original_bus"; replay.origin_generation = 3; replay.origin_record_index = 17;
        replay.origin_session_id = SessionId{2};
        const auto replay_samples = pipeline.decode(replay);
        check(sample(replay_samples, "command").valid && sample(replay_samples, "command").origin_record_index == 17 &&
            sample(replay_samples, "command").origin_session_id == replay.origin_session_id &&
            sample(replay_samples, "command").recording_session_id == receive.recording_session_id, "replay evidence identity");
        const auto output = std::filesystem::path(argv[2]) / std::to_string(monotonicNowNs());
        std::filesystem::create_directories(output);
        const auto bytes = config.document->bytes;
        for (const auto& [from, to] : std::vector<std::pair<std::string, std::string>>{{"rt_address=3", "rt_address=32"},
            {"subaddress=1", "subaddress=0"}, {"direction=receive", "direction=wrong"},
            {"capture_representation=mil1553b.monitor-v1", "capture_representation=unknown"}, {"max_age_ms=100", "max_age_ms=0"}}) {
            auto bad_config = bytes; const auto at = bad_config.find(from); check(at != std::string::npos, "configuration fixture key missing");
            bad_config.replace(at, from.size(), to);
            const auto path = output / ("bad-" + std::to_string(rejected++) + ".ini");
            { std::ofstream file(path, std::ios::binary); file << bad_config; }
            bool denied{}; try { (void)BackendConfiguration::load(path); } catch (const std::invalid_argument&) { denied = true; }
            check(denied, "bad protocol configuration accepted");
        }
        std::cout << "PASS 1553B golden transfers, broadcasts, 32 words, busy, unsupported modes, " << rejected
            << " malformed inputs/configurations, routing isolation and engineering selectors\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << "1553B TEST FAILURE: " << error.what() << '\n'; return 1; }
}
