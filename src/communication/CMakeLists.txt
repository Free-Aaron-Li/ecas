################################################################################
# ecas_communication：L4 传输层
# 模块：mqtt / gateway / discovery / channel
# 依赖：ecas::foundation；MQTT 依赖 redboltz-mqtt_cpp + OpenSSL（Conan，实现时接入）
################################################################################

add_library(ecas_communication INTERFACE)
add_library(ecas::communication ALIAS ecas_communication)

target_include_directories(ecas_communication INTERFACE ${CMAKE_SOURCE_DIR}/src)
target_link_libraries(ecas_communication INTERFACE ecas::foundation)
