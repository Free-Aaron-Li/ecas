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

module;

#include <expected>
#include <filesystem>
#include <format>
#include <functional>
#include <map>
#include <string>
#include <string_view>
#include <utility>

export module application.config_loader_interface;

import foundation.types;

namespace ecas::application {
    using foundation::types::ConfigVariant;
    using foundation::types::Error;

    /**
     * @brief 配置加载器类型：接受文件路径，返回 ConfigVariant 或错误链
     *
     * @note 使用 std::move_only_function 替代 std::function，避免不必要的拷贝，
     * 充分使用 C++23 的移动语义优化。
     */
    export using ConfigLoader
              = std::move_only_function<std::expected<ConfigVariant, Error>(
                        const std::filesystem::path&) const>;

    export class ConfigLoaderRegistry {
    public:
        using LoaderCreator = std::move_only_function<ConfigLoader() const>;

        /**
         * @brief 注册扩展名对应的加载器创建器
         * @param extension 文件扩展名（含点号，如“.json”）
         * @param creator 创建 ConfigLoader 的可调用对象
         * @return 是否成功（若扩展名已存在则失败）
         */
        bool
        register_loader(const std::string_view extension,
                        LoaderCreator          creator) {
            std::string key(extension);
            if (_registry.contains(key)) {
                return false;
            }
            _registry.emplace(std::move(key), std::move(creator));
            return true;
        }

        /**
         * @brief 根据文件路径创建对应的加载器
         * @param path 配置文件路径
         * @return 成功返回 ConfigLoader，否则返回 Error
         */
        std::expected<ConfigLoader, Error>
        create(const std::filesystem::path& path) const {
            const std::string extension = path.extension().string();
            if (const auto it = _registry.find(extension);
                it != _registry.end()) {
                return it->second();
            }
            return std::unexpected(Error{
                      foundation::types::ErrorCode::InvalidConfig,
                      std::format(
                                "No config loader registered for extension: {}",
                                extension) });
        }

        /**
         * @brief 获取 ConfigLoaderRegistry 的单例实例
         * @return ConfigLoaderRegistry 的单例实例
         */
        static ConfigLoaderRegistry&
        instance() {
            static ConfigLoaderRegistry registry;
            return registry;
        }

    private:
        std::map<std::string, LoaderCreator>
                  _registry;  ///< 存储配置加载器的注册表（按照扩展名排序）
    };
}  // namespace ecas::application
