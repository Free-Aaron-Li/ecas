################################################################################
# ecas_acquisition：L1–L3 采集层
# 模块：driver / protocol / scheduler / buffer / control
# 依赖：ecas::foundation；协议解析依赖 pugixml（Conan，实现时接入）
################################################################################

add_library(ecas_acquisition INTERFACE)
add_library(ecas::acquisition ALIAS ecas_acquisition)

target_include_directories(ecas_acquisition INTERFACE ${CMAKE_SOURCE_DIR}/src)
target_link_libraries(ecas_acquisition INTERFACE ecas::foundation)
