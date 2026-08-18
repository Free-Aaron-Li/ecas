################################################################################
# Application 模块配置文件
# 用于定义最终可执行文件及其依赖项
################################################################################

# 1. 最低 CMake 版本要求
cmake_minimum_required(VERSION 3.28)

# 2. 定义项目信息
project(application
        VERSION 0.1.0
        DESCRIPTION "Main terminal application for ECAS"
        LANGUAGES CXX
)

# 4. 定义可执行文件目标
add_executable(application
        src/main.cpp
        src/config_loader_mbp.cppm
)

# 5. 配置 C++ 模块源
target_sources(application PUBLIC
        FILE_SET cxx_modules TYPE CXX_MODULES
        BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src
        FILES
        ${CMAKE_CURRENT_SOURCE_DIR}/src/run.cppm
        ${CMAKE_CURRENT_SOURCE_DIR}/src/config_loader_mbp.cppm
        ${CMAKE_CURRENT_SOURCE_DIR}/src/settings.cppm
        ${CMAKE_CURRENT_SOURCE_DIR}/src/config_loader_interface.cppm
        ${CMAKE_CURRENT_SOURCE_DIR}/src/config_loader_json.cppm
)

# 6. 配置链接库
# 链接本项目中的 foundation 和 acquisition 核心库
target_link_libraries(application
        PRIVATE
        foundation
        acquisition
)

# 7. 配置编译选项
target_compile_options(application
        PRIVATE
        -Wall
        -Wextra
        -Wpedantic
)

# 8. 确保在构建 application 时更新 Doxygen 文档
if (TARGET doc)
    add_dependencies(application doc)
endif ()