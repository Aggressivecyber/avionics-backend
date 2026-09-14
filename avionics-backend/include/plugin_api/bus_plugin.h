#ifndef AVIONICS_BUS_PLUGIN_H
#define AVIONICS_BUS_PLUGIN_H

#include <stdint.h>
#if defined(_WIN32)
#define BUS_CALL __cdecl
#if defined(BUS_PLUGIN_BUILD)
#define BUS_EXPORT __declspec(dllexport)
#else
#define BUS_EXPORT
#endif
#else
#define BUS_CALL
#define BUS_EXPORT __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif

#define BUS_ABI_VERSION 1u
#define BUS_MAX_PAYLOAD 65536u
#define BUS_ERROR_CAPACITY 256u
#define BUS_PROTOCOL_SIMULATED 65535u
#define BUS_CLOCK_HOST_MONOTONIC 1u
#define BUS_CLOCK_RECORDED 2u
#define BUS_FRAME_INVALID 1u

typedef struct BusError { char message[BUS_ERROR_CAPACITY]; } BusError;

/* All pointers are borrowed for the duration of the call. No STL or exceptions
 * cross this boundary. capture_time_ns is not implicitly sensor sample time. */
typedef struct BusFrameView {
    uint32_t struct_size;
    uint32_t protocol;
    uint32_t channel;
    uint32_t flags;
    uint32_t clock_domain;
    uint32_t payload_size;
    uint64_t capture_time_ns;
    uint64_t sequence;
    const uint8_t* payload;
    /* Optional provenance for replay. Null means this is an original frame. */
    const char* origin_source;
    uint64_t origin_generation;
    uint64_t origin_record_index;
} BusFrameView;

typedef struct BusHost {
    uint32_t struct_size;
    uint32_t abi_version;
    void* context;
    /* Return 1 when copied into the host queue; 0 means rejected/dropped. */
    int32_t (BUS_CALL *emit_frame)(void*, const BusFrameView*);
    uint64_t (BUS_CALL *monotonic_now_ns)(void*);
} BusHost;

typedef struct BusAdapterStatus {
    uint32_t struct_size;
    uint32_t running;
    uint64_t emitted;
    uint64_t rejected;
    uint64_t errors;
} BusAdapterStatus;

typedef struct BusPluginApi {
    uint32_t struct_size;
    uint32_t abi_version;
    char name[64];
    int32_t (BUS_CALL *create)(const BusHost*, void**, BusError*);
    int32_t (BUS_CALL *configure)(void*, const char*, BusError*);
    int32_t (BUS_CALL *start)(void*, BusError*);
    /* stop MUST join every worker and finish all host callbacks before return.
     * destroy MUST be safe after stop, including a failed start. */
    void (BUS_CALL *stop)(void*);
    void (BUS_CALL *destroy)(void*);
    int32_t (BUS_CALL *status)(void*, BusAdapterStatus*, BusError*);
} BusPluginApi;

typedef int32_t (BUS_CALL *BusQueryApi)(uint32_t, BusPluginApi*);
BUS_EXPORT int32_t BUS_CALL bus_plugin_query(uint32_t requested_abi, BusPluginApi* output);

#ifdef __cplusplus
}
#endif
#endif
