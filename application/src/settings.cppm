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

namespace ecas::application {
    export struct ApplicationSettings {
        std::string log_path;         ///< 日志文件路径
        std::string mbp_config_path;  ///< MBP 配置文件路径
    };

    export std::expected<ApplicationSettings, foundation::types::Error>
    load_application_settings() {
        using foundation::types::Error;
        using foundation::types::ErrorCode;

        constexpr std::string_view config_file = "../config/application.cfg";
        std::ifstream              file(config_file.data());
        if (!file.is_open()) {
            return std::unexpected(Error{
                      ErrorCode::FileNotFound,
                      std::format("Could not open configuration file: {}",
                                  config_file) });
        }

        ApplicationSettings settings{};
        std::string         line{};
        auto                line_num{ 0 };

        /* 获取去除首尾空格的一行 */
        auto trim = [](const std::string_view str) -> std::string_view {
            const auto first{ str.find_first_not_of(" \t\r\n") };
            if (first == std::string_view::npos) {
                return {};
            }
            const auto last{ str.find_last_not_of(" \t\r\n") };
            return str.substr(first, last - first + 1);
        };

        /* 去除值两侧的引号 */
        auto trim_quotes = [](const std::string_view str) -> std::string_view {
            if (str.size() >= 2) {
                const char front{ str.front() };
                if (const char back{ str.back() };
                    (front == '"' && back == '"')
                    || (front == '\'' && back == '\'')) {
                    return str.substr(1, str.size() - 2);
                }
            }
            return str;
        };

        while (std::getline(file, line)) {
            ++line_num;
            auto trimmed{ trim(line) };
            if (trimmed.empty() || trimmed[0] == '#') {
                continue;  ///< 忽略空行和注释
            }

            /* 只取第一个“=”作为键值对分隔符 */
            auto eq_pos = trimmed.find('=');
            if (eq_pos == std::string_view::npos) {
                return std::unexpected(
                          Error{ ErrorCode::InvalidConfig,
                                 std::format("Invalid format at line {}: {} in "
                                             "application.cfg",
                                             line_num, trimmed) });
            }
            auto key{ trim(trimmed.substr(0, eq_pos)) };
            auto value{ trim(trimmed.substr(eq_pos + 1)) };
            value = trim_quotes(value);

            if (key.empty()) {
                return std::unexpected(
                          Error{ ErrorCode::InvalidConfig,
                                 std::format("Not has key, invalid format at "
                                             "line {}: {} in "
                                             "application.cfg",
                                             line_num, trimmed) });
            }
            if (value.empty()) {
                return std::unexpected(
                          Error{ ErrorCode::InvalidConfig,
                                 std::format("Not has value, invalid format at "
                                             "line {}: {} in "
                                             "application.cfg",
                                             line_num, trimmed) });
            }

            if (key == "log_path") {
                settings.log_path = value;
            } else if (key == "mbp_config_path") {
                settings.mbp_config_path = value;
            } else {
                // 未知键产生警告（需确保日志系统已初始化，但该函数在日志初始化之前调用，因此不能使用
                // logger::warning） 暂时输出到 stderr 或忽略，但建议在 main
                // 中捕获并记录
                // 此处先通过标准错误输出警告，或将警告信息通过返回值传递？
                // 简单起见，用 std::cerr（但项目未引入
                // <iostream>，可考虑返回包含警告的 expected） 由于本函数返回
                // expected，我们无法同时返回警告，可以在调用处（main）处理。
                // 目前保留 TODO 注释，具体实现可扩展。
            }
        }

        if (settings.log_path.empty()) {
            return std::unexpected(
                      Error{ ErrorCode::MissingRequiredField,
                             "Missing required setting \"log_path\"" });
        }
        if (settings.mbp_config_path.empty()) {
            return std::unexpected(
                      Error{ ErrorCode::MissingRequiredField,
                             "Missing required setting \"mbp_config_path\"" });
        }
        return settings;
    }
}  // namespace ecas::application
