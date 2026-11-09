################################################################################
# ECAS (Edge-Cloud Acquisition System) 顶层构建配置
# 负责项目全局设置、子目录包含与 Doxygen 文档配置
################################################################################

# 1. CMake 配置
# 1.1 最低 CMake 版本要求
# 2026-10-13 先生令：进行中的项目一律用最新版 CMake（本机 4.4.4）并合理利用其特性。
#   本工程目前只在 Mac 上配置（g++-16 / Ninja）；已实测 4.4.4 下配置、构建、ctest 全通过。
#   云端 ecas 主机（cmake 3.26.5）只跑部署、不配置本工程（deploy/ 仍是占位）。
cmake_minimum_required(VERSION 4.4)
# 1.2 生成编译命令，用于第三方软件识别
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# 2. 项目名称、版本号、支持语言
project(ecas VERSION 0.0.1 LANGUAGES C CXX)

# 3. C++ 标准：C++26（GCC 16.2 主战场 / Clang 24 对标 / MSVC 暂不考虑）
set(CMAKE_CXX_STANDARD 26)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 4. C++20/26 模块扫描
# 计划决策：Modules 降级为可选（GCC 16 模块仍标 experimental），
# 阶段 0 以「命名空间 + 头文件分层」为主。若启用 Modules（.cppm），
# 需使用 Ninja 生成器并将本开关置 ON。
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)

# 5. 可执行文件输出位置（项目顶层 bin/）
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/../bin)

# 6. Qt 查找路径配置（启用 L8 UI 层时使用）
if (DEFINED ENV{QT_ROOT})
    list(APPEND CMAKE_PREFIX_PATH "$ENV{QT_ROOT}")
endif ()

# 7. 全局编译选项：警告全开（-Werror 由 CI 门禁在 GCC 16.2 主战场启用）
add_compile_options(-Wall -Wextra -Wpedantic)

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
        set(DOXYFILE_IN ${CMAKE_CURRENT_SOURCE_DIR}/tools/doxygen_tools/Doxyfile.in)
        set(DOXYFILE_OUT ${CMAKE_CURRENT_BINARY_DIR}/Doxyfile)

        if (EXISTS ${DOXYFILE_IN})
            # 定义文档输出路径（相对于项目根目录）
            set(DOXYGEN_OUTPUT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/doxygen")

            # 使用 configure_file 在 CMake 配置阶段自动生成 Doxyfile
            configure_file(${DOXYFILE_IN} ${DOXYFILE_OUT} @ONLY)

            # 根据开关决定是否将 docs 设为 ALL (即每次构建都自动运行)
            set(doc_opt "")
            if (ENABLE_DOXYGEN_AUTO_UPDATE)
                set(doc_opt ALL)
            endif ()

            # 添加一个自定义目标 'docs'
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

# 9. 包含子项目（重构后的分层结构：L1–L8 单向依赖）
enable_testing()
add_subdirectory(src)
add_subdirectory(tests)
