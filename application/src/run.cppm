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

/**
 * @module application.run
 * @brief 程序的主运行模块
 */
export module application.run;

import foundation.types;
import foundation.logger;
import application.settings;
import application.config_loader;

namespace ecas::application {
    namespace types  = foundation::types;
    namespace logger = foundation::logger;

    export std::expected<void, types::Error>
    run(const ApplicationSettings& settings, int argc, char* argv[]) {
        logger::info("Application started.");

        /* 1. 加载配置 */
        auto config_result{ load_mbp_config(settings.mbp_config_path) };
        if (!config_result) {
            return std::unexpected(std::move(config_result.error()));
        }

        /* 2. 配置加载成功，记录简略信息 */
        const auto& cfg = *config_result;
        logger::info(
                  "MBP configuration loaded: Slave={}, Func={}, Addr={}, "
                  "Qty={}",
                  cfg.slaveId, cfg.function, cfg.address, cfg.quantity);

        for (const auto& [idx, meta]: cfg.registerMap) {
            logger::debug("   Register[{}] = {}", idx, meta.name);
        }

        logger::info("Entering main processing loop...");
        logger::info("Application finished.");
        return {};
    }
}  // namespace ecas::application
