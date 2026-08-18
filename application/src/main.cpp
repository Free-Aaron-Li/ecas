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
 * @file main.cpp
 * @brief 应用程序主入口文件
 *
 * 包含程序的标准 C++ main 函数，负责调用核心运行模块。
 */

#include <filesystem>
#include <iostream>
#include <print>

import application.run;
import foundation.types;
import foundation.logger;
import application.settings;
import application.config_loader_mbp;
import application.config_loader_interface;

/**
 * @brief 全局主函数，程序的起点
 *
 * 该函数负责接收操作系统传递的参数，并将其转发给 application.run 模块。
 *
 * @param argc 命令行参数数量
 * @param argv 命令行参数数组，其中 argv[0] 通常是程序名
 * @return 程序退出状态码，0 表示正常退出
 */
int
main(const int argc, char* argv[]) {
    /* 1. 加载应用程序设置 */
    auto setting_result{ ecas::application::load_application_settings() };
    if (!setting_result) {
        std::println(std::cerr, "Failed to load application setting:  {}",
                     ecas::foundation::types::format_error_chain(
                               setting_result.error()));
        return 1;
    }
    const auto& settings = *setting_result;

    /* 2. 使用 RAII 守卫管理日志生命周期 */
    ecas::foundation::logger::LoggerGuard logger_guard{ settings.log_path };

    /* 3. 创建配置加载器 */
    auto loader_result{
        ecas::application::ConfigLoaderRegistry::instance().create(
                  std::filesystem::path(settings.mbp_config_path))
    };
    if (!loader_result) {
        ecas::foundation::logger::error(
                  "Failed to create config loader:\n    {}",
                  ecas::foundation::types::format_error_chain(
                            loader_result.error()));
        return 1;
    }

    /* 4. 运行业务 */
    if (auto result{ ecas::application::run(settings, *loader_result, argc,
                                            argv) };
        !result) {
        auto err_str{ ecas::foundation::types::format_error_chain(
                  result.error()) };
        ecas::foundation::logger::error("Application failed:\n    {}", err_str);
        ecas::foundation::logger::shutdown();
        return 1;
    }

    ecas::foundation::logger::shutdown();
    return 0;
}
