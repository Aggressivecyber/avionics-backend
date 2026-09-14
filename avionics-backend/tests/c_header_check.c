#include "plugin_api/bus_plugin.h"
_Static_assert(sizeof(uint64_t) == 8, "ABI requires 64-bit uint64_t");
int bus_c_header_check(void) { BusPluginApi api = {0}; return (int)api.abi_version; }
