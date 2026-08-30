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
 * @file config_manager.cppm
 * @brief 配置管理器，负责创建并管理所有配置监控器
 */
module;

#include <memory>

export module config_manager;

import application.settings;
import application.config_loader_mbp;
import foundation.types;

namespace ecas::application {

    /**
     * @brief 配置管理器，持有所有配置监控器
     */
    export class ConfigManager {
    public:
        using MbpConfigPtr
                  = std::shared_ptr<const foundation::types::MbpConfig>;
        using IniConfigPtr
                  = std::shared_ptr<const foundation::types::IniConfig>;
        using JsonConfigPtr
                  = std::shared_ptr<const foundation::types::JsonConfig>;

        explicit ConfigManager(const ApplicationSettings& settings) {}
    };
}  // namespace ecas::application
