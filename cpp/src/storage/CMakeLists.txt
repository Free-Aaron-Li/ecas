################################################################################
# ecas_storage：L5 持久层
# 模块：iotdb / postgresql / cache / encryption
# 依赖：ecas::foundation；IoTDB / PostgreSQL 客户端（Conan，实现时接入）
################################################################################

add_library(ecas_storage INTERFACE)
add_library(ecas::storage ALIAS ecas_storage)

target_include_directories(ecas_storage INTERFACE ${CMAKE_SOURCE_DIR}/src)
target_link_libraries(ecas_storage INTERFACE ecas::foundation)
