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
 * @file main.cpp
 * @brief 应用程序主入口文件
 *
 * 包含程序的标准 C++ main 函数，负责调用核心运行模块。
 */

#include <filesystem>
#include <iostream>
#include <print>
#include <variant>

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

    ecas::foundation::logger::shutdown();
    return 0;
}
