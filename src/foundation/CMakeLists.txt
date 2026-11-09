################################################################################
# ecas_foundation：基础库
# 模块：error / log / raii / config / coroutine / reflect / contracts /
#       crypto / audit / concepts（命名空间 ecas::foundation::*）
# 阶段 0：INTERFACE 空库；随实现推进逐步转为 STATIC 并接入 Conan 依赖
################################################################################

add_library(ecas_foundation INTERFACE)
add_library(ecas::foundation ALIAS ecas_foundation)

# 包含根 = src/（头文件路径与命名空间对应，如 <foundation/error/error.hpp>）
target_include_directories(ecas_foundation INTERFACE ${CMAKE_SOURCE_DIR}/src)
