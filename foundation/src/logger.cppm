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
 * @file logger.cppm
 * @brief 定义日志记录相关功能
 */
module;

#include <array>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <format>
#include <fstream>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <ostream>
#include <print>
#include <shared_mutex>
#include <source_location>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

/**
 * @module foundation.logger
 * @brief 日志记录模块
 */
export module foundation.logger;

/**
 * @namespace ecas::foundation::logger
 * @brief 日志记录模块命名空间
 *
 * 提供线程安全的异步日志记录功能，支持多种输出目标（控制台、文件等）。
 * 主要特性包括：
 * - 异步日志写入，避免阻塞主线程
 * - 自动捕获源代码位置信息
 * - 支持格式化字符串
 * - 多级别日志（DEBUG、INFO、WARNING、ERROR）
 * - 批量写入优化
 */
namespace ecas::foundation::logger {
    namespace {
        /**
         * @enum LogLevel
         * @brief 日志级别枚举
         *
         * 定义系统支持的日志级别，从低到高依次为调试、信息、警告、错误。
         */
        enum class LogLevel : unsigned char {
            Debug = 0,  ///< 调试信息，用于开发阶段的详细跟踪
            Info,       ///< 一般信息，记录正常的程序运行状态
            Warning,    ///< 警告信息，表示潜在问题但不影响程序继续运行
            Error       ///< 错误信息，表示发生了需要关注的异常情况
        };

        /**
         * @brief 日志输出槽基类
         *
         * 定义了所有日志输出目标的接口。
         */
        class LogSink {
        public:
            virtual ~LogSink() = default;

            /**
             * @brief 批量写入日志消息
             * @param messages 格式化后的日志消息序列
             */
            virtual void
            write_batch(const std::deque<std::string>& messages) {
                for (const auto& msg: messages) {
                    write(msg);
                }
            }

            /**
             * @brief 写入日志消息
             * @param message 格式化后的日志消息
             */
            virtual void write(const std::string& message) = 0;

            /**
             * @brief 刷新输出缓冲区
             */
            virtual void
            flush() {}
        };

        /**
         * @brief 控制台输出槽
         *
         * 将日志消息输出到标准输出（控制台）。
         */
        class ConsoleSink final : public LogSink {
        public:
            void
            write(const std::string& message) override {
                std::println("{}", message);
            }

            void
            write_batch(const std::deque<std::string>& messages) override {
                for (const auto& msg: messages) {
                    std::println("{}", msg);
                }
            }

            void
            flush() override {
                std::cout.flush();
            }
        };

        /**
         * @brief 文件输出槽
         *
         * 将日志消息输出到指定的文件。支持动态更改文件路径。
         */
        class FileSink final : public LogSink {
        public:
            /**
             * @brief 设置日志文件路径
             * @param path 日志文件路径
             *
             * 如果目录不存在则自动创建。如果文件打开失败，将通过标准错误打印警告。
             */
            void
            set_path(const std::string_view path) {
                std::lock_guard lock(_file_mutex);
                try {
                    std::filesystem::create_directories(
                              std::filesystem::path(path).parent_path());
                } catch (...) {
                    return;
                }

                auto new_file = std::make_shared<std::ofstream>();
                new_file->open(std::string(path), std::ios::app);
                if (new_file->is_open()) {
                    _file_ptr = std::move(new_file);
                }
            }

            /**
             * @brief 执行批量写入操作
             * @param messages 日志消息序列
             */
            void
            write_batch(const std::deque<std::string>& messages) override {
                std::lock_guard lock(_file_mutex);
                if (_file_ptr && _file_ptr->is_open()) {
                    for (const auto& msg: messages) {
                        *_file_ptr << msg << '\n';
                    }
                    // 批量写入后不强制 flush，依赖操作系统缓存或手动 flush
                }
            }

            /**
             * @brief 执行物理写入操作
             * @param message 日志消息
             */
            void
            write(const std::string& message) override {
                std::lock_guard lock(_file_mutex);
                if (_file_ptr && _file_ptr->is_open()) {
                    *_file_ptr << message << std::endl;
                }
            }

            void
            flush() override {
                std::lock_guard lock(_file_mutex);
                if (_file_ptr && _file_ptr->is_open()) {
                    _file_ptr->flush();
                }
            }

        private:
            std::mutex                     _file_mutex;  ///< 保护文件流的互斥锁
            std::shared_ptr<std::ofstream> _file_ptr;    ///< 共享的文件流指针
        };

        /**
         * @class LogFormatter
         * @brief 日志格式化工具类
         *
         * 提供日志消息的格式化功能，包括时间、级别、位置和消息内容。
         */
        class LogFormatter {
        public:
            /**
             * @brief 构造完整日志消息
             * @param level 日志级别
             * @param loc 源码位置信息
             * @param message 日志消息
             * @param time_str 缓存的时间字符串（若为空则即时生成）
             * @return 完整格式化后的日志文本
             */
            static std::string
            format(const LogLevel level, const std::source_location& loc,
                   std::string_view message, std::string_view time_str) {
                /* 若未提供缓存，则回退到即时生成（但不应当发生） */
                std::string fallback_time{};
                if (time_str.empty()) {
                    fallback_time
                              = format_time(std::chrono::system_clock::now());
                    time_str = fallback_time;
                }

                const auto level_str{ level_to_string(level) };
                const auto loc_str{ simplify_location(loc) };

                const size_t needed{ std::formatted_size("{} [{}] [{}] {}",
                                                         time_str, loc_str,
                                                         level_str, message) };
                if (constexpr size_t STACK_BUFFER_SIZE{ 1024 };
                    needed < STACK_BUFFER_SIZE) {
                    /* 使用栈上缓冲区 */
                    std::array<char, STACK_BUFFER_SIZE> buffer{};
                    auto end{ std::format_to(buffer.data(), "{} [{}] [{}] {}",
                                             time_str, loc_str, level_str,
                                             message) };
                    /* result 指向末尾，但并未添加 null 终止符 */
                    return std::string(buffer.data(), end - buffer.data());
                }

                /* 超长消息，动态分配 */
                std::string formatted_message;
                formatted_message.reserve(needed);
                std::format_to(std::back_inserter(formatted_message),
                               "{} [{}] [{}] {}", time_str, loc_str, level_str,
                               message);
                return formatted_message;
            }

            /**
             * @brief 格式化时间点为字符串
             * @param tp 时间点（通常为 system_clock::now()）
             * @return 格式化的时间字符串 "YYYY-MM-DD HH:MM:SS"
             */
            static std::string
            format_time(const std::chrono::system_clock::time_point& tp) {
                const auto local_time = std::chrono::zoned_time{
                    std::chrono::current_zone(), tp
                };
                return std::format("{:%Y-%m-%d %H:%M:%S}", local_time);
            }

        private:
            /**
             * @brief 将日志级别转换为字符串
             * @param level 日志级别
             * @return 日志级别字符串表示
             */
            static std::string_view
            level_to_string(const LogLevel level) {
                switch (level) {
                    case LogLevel::Debug: return "DEBUG";
                    case LogLevel::Info: return "INFO";
                    case LogLevel::Warning: return "WARNING";
                    case LogLevel::Error: return "ERROR";
                    default: return "UNKNOWN";
                }
            }

            /**
             * @brief 简化源代码位置信息
             * @param loc 原始位置信息
             * @return 简化后的字符串
             */
            static std::string
            simplify_location(const std::source_location& loc) {
                /* 1. 获取文件名 */
                std::string_view file{ loc.file_name() };
                if (const auto slash_pos{ file.find_last_of("/\\") };
                    slash_pos != std::string_view::npos) {
                    file = file.substr(slash_pos + 1);
                }

                /* 2. 获取命名空间+函数名 */
                std::string func{ loc.function_name() };
                if (const auto paren_pos{ func.find('(') };
                    paren_pos != std::string::npos) {
                    func = func.substr(0, paren_pos);
                }
                if (const auto space_pos{ func.find_last_of(' ') };
                    space_pos != std::string_view::npos) {
                    func = func.substr(space_pos + 1);
                }

                /* 3. 修剪命名空间，仅取首字母 */
                std::string simplified_func{};
                size_t      start{ 0 };
                size_t      end{ 0 };
                bool        has_namespace{ false };

                while ((end = func.find("::", start)) != std::string::npos) {
                    if (const auto part
                        = std::string_view(func).substr(start, end - start);
                        !part.empty()) {
                        if (part.front() == '{' || part.front() == '(') {
                            ///< 跳过匿名空间
                        } else {
                            if (has_namespace) {
                                simplified_func += '.';
                            }
                            simplified_func += part[0];
                            has_namespace = true;
                        }
                    }
                    start = end + 2; /* 跳过“::” */
                }

                /* 最后函数名后无“::” */
                if (const auto last_part{
                              std::string_view(func).substr(start) };
                    !last_part.empty()) {
                    if (has_namespace) {
                        simplified_func += '.';
                    }
                    simplified_func += last_part;
                }

                /* 针对 C 无命名空间情况 */
                if (simplified_func.empty()) {
                    simplified_func = func;
                }

                return std::format("{}:{} {}", file, loc.line(),
                                   simplified_func);
            }
        };

        /**
         * @class AsyncDispatcher
         * @brief 异步日志分发器
         *
         * 负责管理后台线程、日志消息队列以及所有的日志输出槽（Sinks）。
         * 它将日志的格式化输出与实际的物理写入解耦。
         */
        class AsyncDispatcher {
        public:
            AsyncDispatcher() :
                _worker([this](const std::stop_token& stop_token) {
                    background_worker(stop_token);
                }) {
                /* 1. 初始化缓存一次时间 */
                update_time_cache();
                /* 2. 启动时间更新线程（独立于日志工作线程） */
                _time_updater = std::jthread(
                          [this](const std::stop_token& stop_token) {
                              while (!stop_token.stop_requested()) {
                                  std::this_thread::sleep_for(
                                            std::chrono::seconds(1));
                                  update_time_cache();  ///< 每1秒更新1次
                              }
                          });
                std::unique_lock lock(_sinks_mutex);
                _log_sinks.push_back(std::make_unique<ConsoleSink>());
                _log_sinks.push_back(std::make_unique<FileSink>());
            }

            ~AsyncDispatcher() { shutdown(); }

            /**
             * @brief 获取当前缓存时间字符串
             * @return string_view 指向内部缓冲，保证下一个更新周期前有效
             */
            [[nodiscard]] std::string_view
            get_cached_time() const noexcept {
                const std::string* ptr{ _cached_time.load(
                          std::memory_order_acquire) };
                return ptr ? std::string_view(*ptr) : std::string_view{};
            }

            /**
             * @brief 将日志消息加入队列
             * @param level 日志级别
             * @param loc 源码位置
             * @param message 格式化后的日志消息
             */
            void
            enqueue(LogLevel level, const std::source_location& loc,
                    std::string_view message) {
                if (_shutdown_called.load(std::memory_order_acquire)) {
                    return;
                }

                std::string formatted_message{ LogFormatter::format(
                          level, loc, message, get_cached_time()) };

                /* 入队（移动语义，避免拷贝） */
                {
                    std::lock_guard lock(_queue_mutex);
                    _queue.push_back(std::move(formatted_message));
                }
                _cv.notify_one();
            }

            /**
             * @brief 设置日志文件输出路径
             * @param path 文件路径
             */
            void
            setLogFile(const std::string_view path) {
                std::shared_lock lock(_sinks_mutex);
                for (auto& sink: _log_sinks) {
                    if (auto* file_sink = dynamic_cast<FileSink*>(sink.get())) {
                        file_sink->set_path(path);
                        return;
                    }
                }
            }

            /**
             * @brief 强制刷新所有输出槽
             */
            void
            flush() {
                std::shared_lock lock(_sinks_mutex);
                for (auto& sink: _log_sinks) {
                    if (sink) {
                        sink->flush();
                    }
                }
            }

            /**
             * @brief 关闭分发器并确保所有日志已写入
             */
            void
            shutdown() {
                if (bool expected{ false };
                    !_shutdown_called.compare_exchange_strong(
                              expected, true, std::memory_order_acq_rel)) {
                    return;
                }

                /* 1. 停止时间更新线程 */
                if (_time_updater.joinable()) {
                    _time_updater.request_stop();
                    _time_updater.join();
                }

                /* 2. 停止日志工作线程 */
                _worker.request_stop();
                _cv.notify_all();
                if (_worker.joinable()) {
                    _worker.join();
                }

                /* 3. 清空 sinks */
                std::unique_lock lock(_sinks_mutex);
                _log_sinks.clear();
            }

        private:
            /**
             * @brief 后台工作线程循环
             */
            void
            background_worker(const std::stop_token& stop_token) {
                std::deque<std::string> local_queue;
                for (;;) {
                    {
                        std::unique_lock lock(_queue_mutex);
                        _cv.wait_for(lock, std::chrono::milliseconds(100),
                                     [this, &stop_token] {
                                         return !_queue.empty()
                                                || stop_token.stop_requested();
                                     });

                        if (!_queue.empty()) {
                            local_queue.swap(_queue);
                        } else if (stop_token.stop_requested()) {
                            break;
                        }
                    }

                    if (!local_queue.empty()) {
                        try {
                            std::shared_lock lock(_sinks_mutex);
                            for (const auto& sink: _log_sinks) {
                                if (sink) {
                                    sink->write_batch(local_queue);
                                }
                            }
                        } catch (...) {
                            // 捕获所有异常，避免工作线程崩溃
                        }
                        local_queue.clear();
                    }
                }
            }

            /**
             * @brief 更新时间缓存
             *
             * 使用双缓冲机制更新格式化的时间字符串。
             * 通过原子操作确保读取线程能够无锁访问有效的时间字符串，
             * 而写入线程在后台缓冲区中准备新的时间数据。
             */
            void
            update_time_cache() {
                const auto   now{ std::chrono::system_clock::now() };
                std::string  new_time{ LogFormatter::format_time(now) };
                std::string* target{ _use_buffer1 ? &_time_buf1 : &_time_buf2 };
                *target = std::move(new_time);
                _cached_time.store(target, std::memory_order_release);
                _use_buffer1 = !_use_buffer1;
            }

            std::atomic<const std::string*> _cached_time{
                nullptr
            };  ///< 原子指针，指向当前有效的时间缓存
            std::string _time_buf1;     ///< 时间缓存缓冲区1
            std::string _time_buf2;     ///< 时间缓存缓冲区2
            bool _use_buffer1{ true };  ///< 标记当前使用哪个缓冲区进行写入
            std::jthread                _time_updater;  ///< 时间更新线程
            std::mutex                  _queue_mutex;   ///< 队列互斥锁
            std::shared_mutex           _sinks_mutex;   ///< Sink集合互斥锁
            std::condition_variable_any _cv;            ///< 条件变量
            std::deque<std::string>     _queue;         ///< 消息队列
            std::vector<std::unique_ptr<LogSink>> _log_sinks;  ///< 输出槽集合
            std::jthread                          _worker;     ///< 后台工作线程
            std::atomic<bool> _shutdown_called{ false };  ///< 是否已调用关闭
        };

        /**
         * @class Logger
         * @brief 线程安全的日志记录器（单例模式）
         *
         * 负责对外提供日志接口，并将日志请求委派给 AsyncDispatcher
         * 进行异步处理。
         */
        class Logger {
        public:
            /**
             * @brief 获取 Logger 单例引用
             */
            static Logger&
            instance() {
                static Logger inst;
                return inst;
            }

            /**
             * @brief 配置日志输出文件
             * @param path 文件路径
             */
            void
            setLogFile(const std::string_view path) {
                _dispatcher.setLogFile(path);
            }

            /**
             * @brief 记录日志消息
             * @param level 日志级别
             * @param loc 源码位置信息
             * @param message 原始日志消息
             */
            void
            log(const LogLevel level, const std::source_location& loc,
                const std::string_view message) {
                _dispatcher.enqueue(level, loc, message);
            }

            /**
             * @brief 强制刷新日志
             */
            void
            flush() {
                _dispatcher.flush();
            }

            /**
             * @brief 优雅关闭日志系统
             */
            void
            shutdown() {
                _dispatcher.shutdown();
            }

        private:
            Logger()  = default;
            ~Logger() = default;

            AsyncDispatcher _dispatcher;  ///< 异步日志分发器
        };
    }  // namespace

    /**
     * @brief 记录信息级别日志 (INFO)
     * @param loc 源代码调用位置
     * @param message 已经格式化好的日志消息
     */
    void
    internal_info(const std::source_location& loc,
                  const std::string_view      message) {
        Logger::instance().log(LogLevel::Info, loc, message);
    }

    /**
     * @brief 记录警告级别日志 (WARNING)
     * @param loc 源代码调用位置
     * @param message 已经格式化好的日志消息
     */
    void
    internal_warning(const std::source_location& loc,
                     const std::string_view      message) {
        Logger::instance().log(LogLevel::Warning, loc, message);
    }

    /**
     * @brief 记录错误级别日志 (ERROR)
     * @param loc 源代码调用位置
     * @param message 已经格式化好的日志消息
     */
    void
    internal_error(const std::source_location& loc,
                   const std::string_view      message) {
        Logger::instance().log(LogLevel::Error, loc, message);
    }

    /**
     * @brief 记录调试级别日志 (DEBUG)
     * @param loc 源代码调用位置
     * @param message 已经格式化好的日志消息
     */
    void
    internal_debug(const std::source_location& loc,
                   const std::string_view      message) {
        Logger::instance().log(LogLevel::Debug, loc, message);
    }

    /**
     * @brief 初始化日志系统
     * @param path 日志文件路径
     */
    export void
    init(const std::string_view path) {
        Logger::instance().setLogFile(path);
    }

    /**
     * @brief 关闭日志系统并刷新剩余日志
     */
    export void
    shutdown() {
        Logger::instance().shutdown();
    }

    /**
     * @brief 强制刷新日志到输出目标
     */
    export void
    flush() {
        Logger::instance().flush();
    }

    /**
     * @class LoggerGuard
     * @brief RAII 日志管理器，自动初始化和关闭日志系统
     *
     * 在构造时调用 init() 初始化日志系统，在析构时调用 shutdown()
     * 关闭日志系统， 确保日志资源的生命周期与作用域绑定。
     */
    export class LoggerGuard {
    public:
        /**
         * @brief 构造日志守卫对象并初始化日志系统
         * @param path 日志文件路径，传递给 logger::init()
         */
        explicit LoggerGuard(const std::string_view path) { init(path); }

        /**
         * @brief 析构函数，自动关闭日志系统
         *
         * 当 LoggerGuard 对象离开作用域时，自动调用 shutdown() 关闭日志系统，
         * 确保所有缓冲的日志消息被刷新到输出目标，并释放相关资源。
         */
        ~LoggerGuard() { shutdown(); }

        /* 禁止拷贝（保证唯一所有权） */
        LoggerGuard(const LoggerGuard&)            = delete;
        LoggerGuard& operator=(const LoggerGuard&) = delete;

        /* 允许移动（但实际上并不需要） */
        LoggerGuard(LoggerGuard&&)            = default;
        LoggerGuard& operator=(LoggerGuard&&) = default;
    };

    /**
     * @struct LogFormat
     * @brief 格式化字符串包装类，通过 consteval 构造函数在编译期捕获源码位置
     *
     * 该结构体利用 C++20 特性，允许用户像调用普通 print
     * 一样传递格式化字符串， 而无需手动传递 std::source_location。
     *
     * @tparam Args 格式化参数类型
     */
    export template<typename... Args>
    struct LogFormat {
        std::format_string<Args...> format;  ///< std::format 兼容的格式化字符串
        std::source_location        location;  ///< 自动捕获的源代码调用位置

        /**
         * @brief 构造函数：从字面量常量字符串转换
         * @param s 格式化字符串
         * @param loc 自动填充的调用位置（使用 source_location::current()）
         */
        template<size_t N>
        consteval LogFormat(const char (&s)[N],
                            const std::source_location& loc
                            = std::source_location::current()) :
            format(s), location(loc) {}
    };

    /**
     * @brief 记录信息级别日志 (INFO)
     * @tparam Args 参数包类型
     * @param fmt 包装了源码位置的格式化字符串
     * @param args 传递给格式化字符串的参数
     *
     * 示例: info("User {} logged in", userName);
     */
    export template<typename... Args>
    void
    info(LogFormat<std::type_identity_t<Args>...> fmt, Args&&... args) {
        internal_info(fmt.location,
                      std::format(fmt.format, std::forward<Args>(args)...));
    }

    /**
     * @brief 记录警告级别日志 (WARNING)
     * @tparam Args 格式化参数类型包
     * @param fmt 包装了源码位置的格式化字符串
     * @param args 传递给格式化字符串的参数
     *
     * 示例: warning("Low disk space: {}%", spaceLeft);
     */
    export template<typename... Args>
    void
    warning(LogFormat<std::type_identity_t<Args>...> fmt, Args&&... args) {
        internal_warning(fmt.location,
                         std::format(fmt.format, std::forward<Args>(args)...));
    }

    /**
     * @brief 记录错误级别日志 (ERROR)
     * @tparam Args 格式化参数类型包
     * @param fmt 包装了源码位置的格式化字符串
     * @param args 传递给格式化字符串的参数
     *
     * 示例: error("Connection failed: {}", errorMsg);
     */
    export template<typename... Args>
    void
    error(LogFormat<std::type_identity_t<Args>...> fmt, Args&&... args) {
        internal_error(fmt.location,
                       std::format(fmt.format, std::forward<Args>(args)...));
    }

    /**
     * @brief 记录调试级别日志 (DEBUG)
     * @tparam Args 参数包类型
     * @param fmt 包装了源码位置的格式化字符串
     * @param args 参数
     *
     * 调试日志通常仅在开发环境下启用或受日志级别控制。
     */
    export template<typename... Args>
    void
    debug(LogFormat<std::type_identity_t<Args>...> fmt, Args&&... args) {
#ifndef NDEBUG
        internal_debug(fmt.location,
                       std::format(fmt.format, std::forward<Args>(args)...));
#endif
    }
}  // namespace ecas::foundation::logger
