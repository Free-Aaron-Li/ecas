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

module;

#include <expected>
#include <filesystem>
#include <fstream>
#include <string>
export module application.config_loader_ini;

import application.config_loader_interface;
import foundation.types;

namespace ecas::application {
    using foundation::types::ConfigVariant;
    using foundation::types::Error;
    using foundation::types::ErrorCode;
    using foundation::types::IniConfig;

    export std::expected<IniConfig, Error>
    load_ini_config(const std::filesystem::path& path) {
        std::ifstream file(path);
        if (!file.is_open()) {
            return std::unexpected(Error{
                      ErrorCode::FileNotFound,
                      std::format("Cannot open INI file: {}", path.string()) });
        }

        /* 去除首尾空格 */
        auto trim_space = [](std::string_view str) -> std::string_view {
            const auto first = str.find_first_not_of(" \t\r\n");
            if (first == std::string_view::npos) {
                return {};
            }
            const auto last = str.find_last_not_of(" \t\r\n");
            return str.substr(first, last - first + 1);
        };

        /* 去除首尾引号 */
        auto trim_quotes = [](std::string_view str) -> std::string_view {
            if (str.size() >= 2) {
                char f{ str.front() };
                char b{ str.back() };
                if ((f == '"' && b == '"') || (f == '\'' && b == '\'')) {
                    return str.substr(1, str.size() - 2);
                }
            }
            return str;
        };

        IniConfig   config{};
        std::string current_section{};
        std::string line{};
        int64_t     line_num{ 0 };

        while (std::getline(file, line)) {
            ++line_num;
            auto trimmed{ trim_space(line) };
            if (trimmed.empty()) {
                continue;
            }

            /* 注释处理，支持 # 和 ; */
            if (trimmed.front() == '#' || trimmed.front() == ';') {
                continue;
            }

            /* 节处理：[section] */
            if (trimmed.front() == '[') {
                const auto end = trimmed.find(']');
                if (end == std::string_view::npos) {
                    return std::unexpected(Error{
                              ErrorCode::InvalidConfig,
                              std::format("Unclosed section at line {}: '{}'",
                                          line_num, trimmed) });
                }
                current_section = std::string{ trimmed.substr(1, end - 1) };
                continue;
            }

            /* 键值对 */
            auto eq{ trimmed.find('=') };
            if (eq == std::string_view::npos) {
                return std::unexpected(Error{
                          ErrorCode::InvalidConfig,
                          std::format(
                                    "Invalid key at line {}: '{}', missing '='",
                                    line_num, trimmed) });
            }

            auto key{ trim_space(trimmed.substr(0, eq)) };
            auto value{ trim_quotes(trim_space(trimmed.substr(eq + 1))) };
            if (key.empty()) {
                return std::unexpected(
                          Error{ ErrorCode::InvalidConfig,
                                 std::format("Empty key at line {}: '{}'",
                                             line_num, trimmed) });
            }
            if (value.empty()) {
                return std::unexpected(
                          Error{ ErrorCode::InvalidConfig,
                                 std::format("Empty value at line {}: '{}'",
                                             line_num, trimmed) });
            }

            /* 组装 */
            config.sections[current_section][std::string{ key }]
                      = std::string{ value };
        }
        return config;
    }

    export ConfigLoader
    make_ini_loader() {
        return [](const std::filesystem::path& path)
                         -> std::expected<ConfigVariant, Error> {
            auto ini_result{ load_ini_config(path.string()) };
            if (!ini_result) {
                return std::unexpected(std::move(ini_result.error()));
            }
            return ConfigVariant{ std::in_place_type<IniConfig>,
                                  std::move(*ini_result) };
        };
    }

    namespace {
        struct AutoRegisterIni {
            AutoRegisterIni() {
                ConfigLoaderRegistry::instance().register_loader(
                          ".cfg", make_ini_loader);
                ConfigLoaderRegistry::instance().register_loader(
                          ".ini", make_ini_loader);
            }
        };

        AutoRegisterIni _auto_register_ini;
    }  // namespace
}  // namespace ecas::application
