################################################################################
# ECAS (Energy Consumption Acquisition System) 根配置文件
# 负责项目全局设置、依赖管理、Doxygen 文档配置及子目录包含
################################################################################

###############
# 全局配置参数
# 主要目的：指定第三方软件包管理工具 vcpkg 的配置文件路径，及指定编译器
#-DCMAKE_TOOLCHAIN_FILE=/Users/lijc/.vcpkg-clion/vcpkg/scripts/buildsystems/vcpkg.cmake
#-DVCPKG_TARGET_TRIPLET=arm64-osx-gcc16
#-DVCPKG_HOST_TRIPLET=arm64-osx-gcc16
#-DVCPKG_OVERLAY_TRIPLETS=/Users/lijc/.vcpkg-overlay-triplets
###############

# 1. CMake 配置
# 1.1 最低 CMake 版本要求
cmake_minimum_required(VERSION 3.28)
# 1.2 生成编译命令，用于第三方软件识别
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# 2. 项目名称，版本号，支持语言
# 项目全称：Energy consumption acquisition system
project(ecas VERSION 0.0.0 LANGUAGES C CXX)

# 3. C++ 标准说明
set(CMAKE_CXX_STANDARD 23)

# 4. 强制编译器支持 C++ 标准
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 5. 禁用编译器扩展
set(CMAKE_CXX_EXTENSIONS OFF)

# 6. 开启 C++20 模块扫描
set(CMAKE_CXX_SCAN_FOR_MODULES ON)
# 6.1 开启 Qt 自动处理功能 (MOC, RCC, UIC) 全局开启
#set(CMAKE_AUTOMOC ON)
#set(CMAKE_AUTORCC ON)
#set(CMAKE_AUTOUIC ON)

# 7.1 确定可执行文件位置（项目顶层）
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/../bin)
# 7.2 Qt 查找路径配置
set(CMAKE_PREFIX_PATH "/Users/lijc/env/qt/6.11.1/macos")
# 7.3. 第三方插件
find_package(date CONFIG REQUIRED)
find_package(pugixml CONFIG REQUIRED)
find_package(Catch2 CONFIG REQUIRED)

# 8. Doxygen 文档生成配置
# 是否生成 Doxygen 文档
option(ENABLE_DOXYGEN "是否启用 Doxygen 文档生成功能" ON)
# 是否在每次构建(Build/Run)时自动更新文档
option(ENABLE_DOXYGEN_AUTO_UPDATE "是否在构建时自动更新文档" ON)
# 是否启用 Doxygen 图表生成功能 (需要 Graphviz)
option(ENABLE_DOXYGEN_GRAPH "是否启用 Doxygen 图表生成功能" OFF)

if (ENABLE_DOXYGEN)
    # 查找系统中是否安装了 Doxygen
    find_package(Doxygen)
    if (DOXYGEN_FOUND)
        # 处理图表生成逻辑
        if (ENABLE_DOXYGEN_GRAPH)
            find_program(DOXYGEN_DOT_EXECUTABLE dot)
            if (DOXYGEN_DOT_EXECUTABLE)
                set(DOXYGEN_HAVE_DOT YES)
                message(STATUS "找到 Graphviz dot 工具: ${DOXYGEN_DOT_EXECUTABLE}，将启用图表生成")
            else ()
                set(DOXYGEN_HAVE_DOT NO)
                message(WARNING "未找到 Graphviz dot 工具，图表生成功能将被禁用")
            endif ()
        else ()
            set(DOXYGEN_HAVE_DOT NO)
        endif ()

        # 定义输入模板和输出配置文件的路径
        # Doxyfile.in 是模板，Doxyfile 是 CMake 运行后生成的实际配置文件
        set(DOXYFILE_IN ${CMAKE_CURRENT_SOURCE_DIR}/doxygen_tools/Doxyfile.in)
        set(DOXYFILE_OUT ${CMAKE_CURRENT_BINARY_DIR}/Doxyfile)

        if (EXISTS ${DOXYFILE_IN})
            # 定义文档输出路径（相对于项目根目录）
            set(DOXYGEN_OUTPUT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/doxygen")

            # 使用 configure_file 在 CMake 配置阶段自动生成 Doxyfile
            # 它会将模板中的 @PROJECT_VERSION@ 等变量替换为当前项目的实际值
            configure_file(${DOXYFILE_IN} ${DOXYFILE_OUT} @ONLY)

            # 根据开关决定是否将 doc 设为 ALL (即每次构建都自动运行)
            set(doc_opt "")
            if (ENABLE_DOXYGEN_AUTO_UPDATE)
                set(doc_opt ALL)
            endif ()

            # 添加一个自定义目标 'doc'
            # 确保输出目录存在，并在正确的目录下执行 Doxygen
            add_custom_target(doc ${doc_opt}
                    COMMAND ${CMAKE_COMMAND} -E make_directory "${DOXYGEN_OUTPUT_DIR}"
                    COMMAND ${DOXYGEN_EXECUTABLE} "${DOXYFILE_OUT}"
                    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
                    COMMENT "正在生成 API 文档 (使用 Doxygen)..."
                    VERBATIM)
        else ()
            message(WARNING "未找到 Doxygen 模板文件: ${DOXYFILE_IN}，跳过目标添加")
        endif ()
    else ()
        message(STATUS "未找到 Doxygen 运行环境，无法启用文档生成功能")
    endif ()
else ()
    message(STATUS "Doxygen 功能已被手动关闭")
endif ()

# 9. 包含子项目
add_subdirectory(foundation)
add_subdirectory(acquisition)
add_subdirectory(application)
