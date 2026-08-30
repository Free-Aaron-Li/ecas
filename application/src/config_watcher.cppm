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

#include <atomic>
#include <expected>
#include <filesystem>
#include <functional>
#include <stop_token>
#include <thread>

export module application.config_wathcer;

import foundation.thread_pool;
import foundation.types;

namespace ecas::application {
    using foundation::types::Error;

    /**
     * @brief 配置文件监控器，用于热加载
     * @tparam ConfigType 配置类型（如 MbpConfig）
     */
    export template<typename ConfigType>
    class ConfigWatcher {
    public:
        using ConfigPtr = std::shared_ptr<const ConfigType>;
        using Reloader  = std::function<std::expected<ConfigPtr, Error>()>;

        /**
         * @brief 构造函数
         * @param path 文件路径
         * @param reloader 加载函数
         * @param interval 检查间隔（默认 2 秒）
         * @param auto_load 是否立即加载一次
         */
        ConfigWatcher(std::filesystem::path path, Reloader reloader,
                      const std::chrono::milliseconds interval
                      = std::chrono::seconds{ 2 },
                      const bool auto_load = true) :
            path_(std::move(path)), reloader_(std::move(reloader)),
            interval_(interval), current_(nullptr), stop_source_(),
            worker_([this](const std::stop_token &st) { worker_loop(st); }) {
            if (auto_load) {
                refresh_sync();  ///< 同步首次加载
            }
        }

        ~ConfigWatcher() {
            stop_source_.request_stop();
            if (worker_.joinable()) {
                worker_.join();
            }
            ///< TODO 等待所有已提交的加载任务完成（由任务自身通过原子计数器实现）
        }

        /**
         * @brief 获取当前配置（同步）
         */
        ConfigPtr
        get() const noexcept {
            return current_.load(std::memory_order_acquire);
        }

        /**
         * @brief 手动触发重新加载（同步等待）
         */
        void
        refresh_sync() {
            reload_and_update();
        }

        /**
         * @brief 异步触发重新加载（提交到线程池）
         */
        void
        refresh_async() {
            submit_reload_task();
            ///< TODO 还需理解
        }


    private:
        void
        worker_loop(const std::stop_token &st) {
            auto last_write{ std::filesystem::file_time_type::min() };
            while (!st.stop_requested()) {
                std::error_code ec{};
                auto current_write{ std::filesystem::last_write_time(path_,
                                                                     ec) };
                if (!ec && current_write != last_write) {
                    last_write = current_write;
                    submit_reload_task();  ///< 异步加载
                } else if (ec) {
                    ///< TODO
                    ///< 文件可能不存在，记录错误（但这里无日志，调用方可设置错误处理器）
                }
                std::this_thread::sleep_for(interval_);
            }
        }

        void
        submit_reload_task() {
            /* 使用线程池异步执行加载
             * 注意：若任务抛异常，线程池会调用默认错误处理器 */
            foundation::threadpool::ThreadPool::instance().submit_async(
                      [this] { reload_and_update(); });
        }

        void
        reload_and_update() {
            auto result{ reloader_() };
            if (result) {
                current_.store(*result, std::memory_order_release);
            }  ///< 否则保持旧配置
        }

        std::filesystem::path     path_;
        Reloader                  reloader_;
        std::chrono::milliseconds interval_;
        std::atomic<ConfigPtr>    current_;
        std::stop_source          stop_source_;
        std::jthread              worker_;
    };
}  // namespace ecas::application
