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
#include <format>
#include <functional>
#include <map>
#include <string>
#include <string_view>
#include <utility>

export module application.config_loader_interface;

import foundation.types;

namespace ecas::application {
    using foundation::types::Error;
    using foundation::types::MbpConfig;

    /**
     * @brief 配置加载器类型：接受文件路径，返回 MbpConfig 或错误链
     *
     * @note 使用 std::move_only_function 替代 std::function，避免不必要的拷贝，
     * 充分使用 C++23 的移动语义优化。
     */
    export using ConfigLoader
              = std::move_only_function<std::expected<MbpConfig, Error>(
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
