################################################################################
# Acquisition 模块配置文件
# 用于定义数据采集相关的静态库及相关配置
################################################################################

# 1. 最低 CMake 版本要求
cmake_minimum_required(VERSION 3.28)

# 2. 定义项目信息
project(acquisition
        VERSION 0.1.0
        DESCRIPTION "Data acquisition module for ECAS"
        LANGUAGES CXX
)

# 3. 收集源文件
file(GLOB_RECURSE ACQUISITION_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cxx"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cc"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.c"
)

# 3.1 收集 C++ 模块接口
file(GLOB_RECURSE ACQUISITION_MODULES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cppm"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*.ixx"
)

# 4. 定义库目标
if (ACQUISITION_SOURCES OR ACQUISITION_MODULES)
    add_library(acquisition STATIC)

    if (ACQUISITION_SOURCES)
        target_sources(acquisition
                PRIVATE
                ${ACQUISITION_SOURCES}
        )
    endif ()

    if (ACQUISITION_MODULES)
        target_sources(acquisition
                PUBLIC
                FILE_SET cxx_modules TYPE CXX_MODULES
                BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src
                FILES
                ${ACQUISITION_MODULES}
        )
    endif ()
else ()
    add_library(acquisition INTERFACE)
endif ()

# 5. 按目录树结构在 IDE 中自动分组
if (ACQUISITION_SOURCES OR ACQUISITION_MODULES)
    source_group(TREE ${CMAKE_CURRENT_SOURCE_DIR} FILES
            ${ACQUISITION_SOURCES}
            ${ACQUISITION_MODULES})
endif ()

# 6. 配置包含目录
target_include_directories(acquisition
        INTERFACE
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src>
        $<INSTALL_INTERFACE:include>
)


# 7. 配置编译选项
if (ACQUISITION_SOURCES OR ACQUISITION_MODULES)
    target_compile_options(acquisition
            PRIVATE
            -Wall
            -Wextra
            -Wpedantic
    )
endif ()

# 8 确定依赖关系
target_link_libraries(acquisition
        PUBLIC
        foundation
        pugixml::pugixml
)

# 9. 关联文档生成目标 (如果启用)
if (TARGET doc AND ENABLE_DOXYGEN_AUTO_UPDATE)
    add_dependencies(acquisition doc)
endif ()
