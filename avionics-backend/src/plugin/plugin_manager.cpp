#include "core/plugin_manager.hpp"
#include <cstring>
#include <stdexcept>

namespace avionics {
PluginModule::PluginModule(const std::filesystem::path& path) : library_(path) {
    const auto query = reinterpret_cast<BusQueryApi>(library_.symbol("bus_plugin_query"));
    api_.struct_size = sizeof(api_);
    if (!query(BUS_ABI_VERSION, &api_) || api_.abi_version != BUS_ABI_VERSION || api_.struct_size != sizeof(api_) || !api_.last_error ||
        !api_.create || !api_.configure || !api_.start || !api_.stop || !api_.destroy || !api_.status)
        throw std::runtime_error("incompatible or incomplete plugin ABI");
    if (!std::memchr(api_.name, 0, sizeof(api_.name))) throw std::runtime_error("invalid plugin name");
}
}
