// Copyright (C) 2026 Aaron <communicate_aaron@outlook.com>
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

/**
 * @file config_loader_mbp.cppm
 * @brief 配置文件加载器模块
 *
 * 提供从文件系统加载和解析 Modbus Poll (MBP) 配置文件的功能。
 */
module;

#include <expected>  // C++23 std::expected，用于函数错误处理
#include <filesystem>
#include <format>   // 格式化输出
#include <fstream>  // 文件流操作
#include <string>   // 字符串类型

/**
 * @module application.config_loader_mbp
 * @brief MBP 解析应用程序配置加载模块
 *
 * 该模块负责：
 * - 读取 MBP 配置文件内容
 * - 将配置文件内容传递给解析器
 * - 提供错误码到字符串的转换功能
 */
export module application.config_loader_mbp;

import foundation.types;
import foundation.logger;
import acquisition.parser.mbp_parser;
import application.config_loader_interface;

namespace ecas::application {
    namespace types  = foundation::types;
    namespace logger = foundation::logger;

    /**
     * @brief 加载并解析 Modbus Poll (MBP) 配置文件
     *
     * 该函数执行以下步骤：
     * 1. 打开指定路径的配置文件
     * 2. 读取文件的全部内容到字符串
     * 3. 将内容传递给 MbpParser 进行 XML 解析
     *
     * @param file_path 配置文件的路径（字符串视图）
     * @return std::expected<types::MbpConfig, types::Error>
     *         成功时返回解析后的 MbpConfig 结构体，
     *         失败时返回包含详细上下文的 Error（如 FileNotFound、XmlParseFailed
     * 等）
     */
    export std::expected<types::MbpConfig, types::Error>
    load_mbp_config(const std::string_view file_path) {
        /* 1. 打开文件 */
        std::ifstream file(file_path.data());
        if (!file.is_open()) {
            return std::unexpected(types::Error{
                      types::ErrorCode::FileNotFound,
                      std::format("failed to open configuration file: '{}'",
                                  file_path) });
        }

        /* 2. 读取全部内容 */
        const std::string xml_content((std::istreambuf_iterator(file)),
                                      std::istreambuf_iterator<char>());
        if (file.bad()) {
            return std::unexpected(types::Error{
                      types::ErrorCode::FileReadError,
                      std::format("failed to read configuration file: '{}'",
                                  file_path) });
        }
        file.close();

        /* 3. 交予 mbp_parser 解析，直接返回 expected */
        std::vector<std::string> warnings;
        auto result = acquisition::MbpParser::load(xml_content, &warnings);
        if (!warnings.empty()) {  ///< 无论成功与否，若有警告，立即记录
            for (const auto& warn: warnings) {
                logger::warning("{}", warn);
            }
        }
        if (!result) {
            return std::unexpected(types::Error{
                      types::ErrorCode::DataParseError,
                      std::format(
                                "failed to parse MBP configuration file: '{}'",
                                file_path),
                      std::move(result.error()) });
        }
        return result;
    }

    /* 适配 ConfigLoader 的工厂函数 */
    export ConfigLoader
    make_mbp_loader() {
        return [](const std::filesystem::path& path)
                         -> std::expected<types::MbpConfig, types::Error> {
            return load_mbp_config(path.string());
        };
    }

    /* 自动注册 MBP 加载器(模块加载时执行) */
    namespace {
        struct AutoRegisterMbp {
            AutoRegisterMbp() {
                ConfigLoaderRegistry::instance().register_loader(
                          ".mbp", make_mbp_loader);
            }
        };

        /* 静态对象,确保在 main 前注册 */
        AutoRegisterMbp _auto_register_mbp;
    }  // namespace
}  // namespace ecas::application
