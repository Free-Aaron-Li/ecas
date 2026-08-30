// Copyright (C) 2026 Aaron <communicate_aaron@outlook.com>
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * @file config_loader_json.cppm
 * @brief 配置文件加载器模块
 *
 * 提供从文件系统加载和解析 JSON 配置文件的功能。
 */
module;

#include <expected>  // C++23 std::expected，用于函数错误处理
#include <filesystem>
#include <format>  // 格式化输出
#include <string>  // 字符串类型

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
import foundation.types;

namespace ecas::application {
    using foundation::types::ConfigVariant;
    using foundation::types::Error;
    using foundation::types::ErrorCode;
    using foundation::types::JsonConfig;

    export std::expected<JsonConfig, Error>
    load_json_config(const std::filesystem::path& path) {
        // TODO: 实现 JSON 解析
        return std::unexpected(
                  Error{ ErrorCode::UnknownError,
                         std::format("Not implemented json file: {}",
                                     path.string()) });
    }

    export ConfigLoader
    make_json_loader() {
        return [](const std::filesystem::path& path)
                         -> std::expected<ConfigVariant, Error> {
            auto result{ load_json_config(path.string()) };
            if (!result) {
                return std::unexpected(std::move(result.error()));
            }
            return ConfigVariant(std::in_place_type<JsonConfig>,
                                 std::move(*result));
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
