################################################################################
# Foundation 模块配置文件
# 用于定义公共基础层工具子项目
################################################################################

# 1. 最低 CMake 版本要求
cmake_minimum_required(VERSION 3.28)

# 2. 定义项目信息
project(foundation
        VERSION 0.1.0
        DESCRIPTION "Public foundation utility library for ECAS"
        LANGUAGES CXX
)

# 3. 定义库目标
add_library(foundation STATIC)
# 3.1 第三方依赖
target_link_libraries(foundation
        PRIVATE
        date::date
        date::date-tz
)

# 4. 配置包含目录
target_include_directories(foundation
        PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src>
        $<INSTALL_INTERFACE:include>
)

# 5. 配置 C++ 模块源
target_sources(foundation
        PUBLIC
        FILE_SET cxx_modules TYPE CXX_MODULES
        BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src
        FILES
        ${CMAKE_CURRENT_SOURCE_DIR}/src/types.cppm
        ${CMAKE_CURRENT_SOURCE_DIR}/src/logger.cppm
)

# 6. 配置编译选项
target_compile_options(foundation
        PRIVATE
        -Wall
        -Wextra
        -Wpedantic
)

# 7. 配置编译特性
target_compile_features(foundation PUBLIC cxx_std_23)