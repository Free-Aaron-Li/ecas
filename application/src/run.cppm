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
 * @file run.cppm
 * @brief 程序运行入口文件
 */
module;

#include <expected>
#include <fstream>
#include <string>
#include <thread>
#include <variant>

/**
 * @module application.run
 * @brief 程序的主运行模块
 */
export module application.run;

import foundation.types;
import foundation.logger;
import application.settings;
import application.config_loader_interface;
import application.config_wathcer;

namespace ecas::application {
    namespace logger = foundation::logger;
    using foundation::types::Error;

    /**
     * @brief 运行应用程序的核心业务逻辑
     * @param settings 应用程序配置
     * @param config_loader 配置文件加载器
     * @param argc 命令行参数数量
     * @param argv 命令行参数数组
     * @return 成功返回 void，失败返回 Error
     */
    export std::expected<void, Error>
    run(const ApplicationSettings& settings, const ConfigLoader& config_loader,
        [[maybe_unused]] int argc, [[maybe_unused]] char* argv[]) {
        logger::info("Application started.");

        /* 1. 通过接口加载配置 */
        auto mbp_config_result = config_loader(settings.mbp_config_path);
        if (!mbp_config_result) {
            return std::unexpected(std::move(mbp_config_result.error()));
        }

        /* 2. 配置加载成功，提取 MbpConfig */
        const auto& mbp_config_variant = *mbp_config_result;
        const auto* cfg = std::get_if<foundation::types::MbpConfig>(
                  &mbp_config_variant);
        if (!cfg) {
            return std::unexpected(
                      Error{ foundation::types::ErrorCode::InvalidConfig,
                             "Loaded config is not MbpConfig" });
        }

        logger::info(
                  "MBP configuration loaded: Slave={}, Func={}, Addr={}, "
                  "Qty={}",
                  cfg->slaveId, cfg->function, cfg->address, cfg->quantity);

        for (const auto& [idx, meta]: cfg->registerMap) {
            logger::debug("   Register[{}] = {}", idx, meta.name);
        }

        logger::info("Application finished.");
        return {};
    }

    export std::expected<void, Error>
    run_with_watcher([[maybe_unused]] const ApplicationSettings& settings,
                     const ConfigWatcher<foundation::types::MbpConfig>& watcher,
                     [[maybe_unused]] int argc, [[maybe_unused]] char* argv[]) {
        logger::info("Application started with config watcher.");

        ///< 主循环实例：每秒答应当前配置的 Slave ID
        for (;;) {
            auto cfg = watcher.get();
            if (cfg) {
                logger::info(
                          "MBP configuration loaded: Slave={}, Func={}, "
                          "Addr={}, "
                          "Qty={}",
                          cfg->slaveId, cfg->function, cfg->address,
                          cfg->quantity);
            } else {
                logger::warning("No valid MBP config available.");
            }
            std::this_thread::sleep_for(std::chrono::seconds(5));
            ///< TODO：添加退出条件
        }
        return {};
    }
}  // namespace ecas::application
