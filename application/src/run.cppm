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
 * @file run.cppm
 * @brief 程序运行入口文件
 */
module;

#include <expected>
#include <fstream>
#include <string>
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
}  // namespace ecas::application
