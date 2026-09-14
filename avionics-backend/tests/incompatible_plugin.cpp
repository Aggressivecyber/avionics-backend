#include "plugin_api/bus_plugin.h"
extern "C" BUS_EXPORT int32_t BUS_CALL bus_plugin_query(uint32_t, BusPluginApi* output) {
    if (output) output->abi_version = BUS_ABI_VERSION + 1;
    return 1;
}
