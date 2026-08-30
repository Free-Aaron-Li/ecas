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
 * @file settings.cppm
 * @brief 应用程序设置加载模块
 *
 * 负责从 config/application.cfg 加载键值对配置。
 */
module;

#include <expected>
#include <fstream>
#include <string>
#include <string_view>

export module application.settings;

import foundation.types;
import application.config_loader_ini;

namespace ecas::application {
    using foundation::types::Error;
    using foundation::types::ErrorCode;

    export struct ApplicationSettings {
        std::string log_path;         ///< 日志文件路径
        std::string mbp_config_path;  ///< MBP 配置文件路径
    };

    export std::expected<ApplicationSettings, Error>
    load_application_settings() {
        /* 配置文件路径 */
        constexpr std::string_view config_file = "../config/application.cfg";

        /* 1. 加载并解析 INI 文件 */
        auto ini_result{ load_ini_config(config_file) };
        if (!ini_result) {
            return std::unexpected(std::move(ini_result.error()));
        }

        /* 2. 提取 [Path] 节 */
        const auto& sections{ ini_result->sections };
        auto        path_it{ sections.find("Path") };
        if (path_it == sections.end()) {
            return std::unexpected(Error{
                      ErrorCode::MissingRequiredField,
                      std::format("Missing required section [Path] in {}",
                                  config_file) });
        }
        const auto& path_kv{ path_it->second };

        /* 3. 提取各字段 lambda */
        auto get_value = [&](const std::string_view key)
                  -> std::optional<std::string> {
            if (const auto it{ path_kv.find(std::string(key)) };
                it != path_kv.end()) {
                return it->second;
            }
            return std::nullopt;
        };

        ApplicationSettings settings;

        /* 4. 获取 log_path */
        if (auto val = get_value("log_path")) {
            settings.log_path = std::move(*val);
        } else {
            return std::unexpected(Error{
                      ErrorCode::MissingRequiredField,
                      "Missing required key 'log_path' in [Path] section" });
        }

        /* 5. 获取 mbp_config_path */
        if (auto val = get_value("mbp_config_path")) {
            settings.mbp_config_path = std::move(*val);
        } else {
            return std::unexpected(
                      Error{ ErrorCode::MissingRequiredField,
                             "Missing required key 'mbp_config_path' in [Path] "
                             "section" });
        }

        return settings;
    }
}  // namespace ecas::application
