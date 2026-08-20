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
 * @file config_loader_json.cppm
 * @brief 配置文件加载器模块
 *
 * 提供从文件系统加载和解析 JSON 配置文件的功能。
 */
module;

#include <expected>  // C++23 std::expected，用于函数错误处理
#include <filesystem>
#include <format>   // 格式化输出
#include <fstream>  // 文件流操作
#include <string>   // 字符串类型

/**
 * @module application.config_loader_json
 * @brief JSON 解析应用程序配置加载模块
 *
 * 该模块负责：
 * - 读取 JSON 配置文件内容
 * - 将配置文件内容传递给解析器
 * - 提供错误码到字符串的转换功能
 */
export module application.config_loader_json;

import application.config_loader_interface;
import foundation.logger;
import foundation.types;

namespace ecas::application {
    namespace types  = foundation::types;
    namespace logger = foundation::logger;

    export ConfigLoader
    make_json_loader() {
        return []([[maybe_unused]] const std::filesystem::path& path)
                         -> std::expected<types::MbpConfig, types::Error> {
            ///< TODO 实现 JSON 解析器
            ///< 预返回 Error
            return std::unexpected(
                      types::Error{ types::ErrorCode::UnknownError });
        };
    }

    namespace {
        struct AutoRegisterJson {
            AutoRegisterJson() {
                ConfigLoaderRegistry::instance().register_loader(
                          ".json", make_json_loader);
            }
        };

        AutoRegisterJson _auto_register_json;
    }  // namespace
}  // namespace ecas::application
