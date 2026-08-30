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
 * @file thread_pool.cppm
 * @brief 高性能无锁线程池，基于 moodycamel::ConcurrentQueue 实现，
 *        支持异步任务提交、std::future 及灵活错误处理。
 *
 * 设计目标：
 * - 无锁任务队列，支持多生产者/多消费者，适用于高频任务提交场景。
 * - 工作线程使用 std::jthread + stop_token 实现优雅停止。
 * - 异常通过 std::future
 * 传播，或由全局默认错误处理器处理（针对发射后不管的任务）。
 * - 提供队列长度监控接口，便于系统调优。
 *
 * 典型用途：配置热加载、数据库连接池调度、网络 IO 回调分发等。
 */
module;

#include <cstddef>
#include <functional>
#include <future>
#include <moodycamel/concurrentqueue.h>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

export module foundation.thread_pool;

namespace ecas::foundation::threadpool {
    /**
     * @brief 通用线程池（单例模式），基于无锁队列的高性能实现。
     */
    export class ThreadPool {
    public:
        /**
         * @brief 获取全局唯一线程池实例
         * @param num_threads 工作线程数（默认 hardware_concurrency，至少为 1）
         * @param max_queue_size
         * 任务队列最大容量（0=无限，存在内存风险，建议设置上限）
         * @return ThreadPool& 单例引用
         */
        static ThreadPool&
        instance(const std::size_t num_threads    = 0,
                 const std::size_t max_queue_size = 0) {
            static ThreadPool pool(num_threads, max_queue_size);
            return pool;
        }

        /**
         * @brief 提交任务，返回 std::future 用于获取结果或异常
         * @tparam F 可调用对象类型
         * @tparam Args 参数类型
         * @param f 可调用对象
         * @param args 参数
         * @return std::future<decltype(f(args...))> 任务的 future 对象
         * @throw std::runtime_error 线程池已停止或任务队列已满
         */
        template<typename F, typename... Args>
        auto
        submit(F&& f, Args&&... args) -> std::future<decltype(f(args...))> {
            using return_type = decltype(f(args...));
            auto task{ std::make_shared<std::packaged_task<return_type()>>(
                      std::bind(std::forward<F>(f),
                                std::forward<Args>(args)...)) };
            std::future<return_type> result{ task->get_future() };

            /* 将任务包装为 void() 可调用，放入队列 */
            if (!enqueue_task([task] { (*task)(); })) {
                throw std::runtime_error(
                          "ThreadPool queue is full or stopped!");
            }
            return result;
        }

        /**
         * @brief 提交异步任务，发射后不管，不返回 future
         * @param f 可调用对象（void 返回）
         * @return bool 是否成功提交任务
         */
        bool
        submit_async(std::function<void()> f) {
            auto task{ std::make_shared<std::packaged_task<void()>>(
                      std::move(f)) };
            return enqueue_task([task] { (*task)(); });
        }

        /**
         * @brief 设置全局默认错误处理器，用于 `submit_async` 中未捕获的异常
         * @param error_handler 接受 const std::exception& 的可调用对象
         */
        void
        set_default_error_handler(
                  std::function<void(const std::exception&)> error_handler) {
            std::lock_guard lock(handler_mutex_);
            default_error_handler_ = std::move(error_handler);
        }

        /**
         * @brief 获取当前任务队列大小
         * @return std::size_t 当前任务队列大小
         */
        std::size_t
        queue_size() const noexcept {
            return task_queue_.size_approx();
        }

        /* 禁止拷贝和移动 */
        ThreadPool(const ThreadPool&)            = delete;
        ThreadPool& operator=(const ThreadPool&) = delete;

    private:
        /**
         * @brief 构造函数，启动指定数量的工作线程。
         * @param num_threads 线程数
         * @param max_queue_size 任务队列最大容量
         */
        explicit ThreadPool(std::size_t num_threads,
                            std::size_t max_queue_size) :
            task_queue_(max_queue_size), stop_requested_(false) {
            if (num_threads == 0) {
                num_threads = std::thread::hardware_concurrency();
            }
            if (num_threads == 0) {
                num_threads = 1;
            }

            workers_.reserve(num_threads);
            for (auto i{ 0 }; i < num_threads; ++i) {
                workers_.emplace_back(std::jthread(
                          [this](std::stop_token st) { worker_loop(st); }));
            }
        }

        ~ThreadPool() { stop_requested_ = true; }

        /**
         * @brief 将任务入队
         * @param task 任务函数
         * @return bool 成功入队返回 true，队列满或已停止返回 false
         */
        bool
        enqueue_task(std::function<void()>&& task) {
            if (stop_requested_.load(std::memory_order_acquire)) {
                return false;
            }
            return task_queue_.enqueue(std::move(task));
        }

        /**
         * @brief 工作线程主循环
         * @param st 停止标记
         */
        void
        worker_loop(std::stop_token st) {
            while (!st.stop_requested()
                   && !stop_requested_.load(std::memory_order_acquire)) {

                std::function<void()> task;

                /* 尝试从无锁队列取任务（阻塞等待最多 100ms 后重试
                 * 使用 try_dequeue 以允许检查停止标记 */
                while (!st.stop_requested()
                       && !stop_requested_.load(std::memory_order_acquire)) {
                    if (task_queue_.try_dequeue(task)) {
                        break;  ///< 取到任务
                    }
                    /* 队列空时，短暂休眠以减少 CPU 空转 */
                    std::this_thread::sleep_for(std::chrono::microseconds(100));
                }

                /* 若因停止退出，放弃任务 */
                if (st.stop_requested()
                    || stop_requested_.load(std::memory_order_acquire)) {
                    break;
                }

                /* 执行任务 */
                try {
                    task();
                } catch (const std::exception& e) {
                    /* 调用默认错误处理器（若有） */
                    std::function<void(const std::exception&)> handler;
                    {
                        std::lock_guard lock(handler_mutex_);
                        handler = default_error_handler_;
                    }
                    if (handler) {
                        handler(e);
                    }
                } catch (...) {
                    std::function<void(const std::exception&)> handler;
                    {
                        std::lock_guard lock(handler_mutex_);
                        handler = default_error_handler_;
                    }
                    if (handler) {
                        handler(std::runtime_error(
                                  "Unknown exception in thread pool task"));
                    }
                }
            }
        }

        moodycamel::ConcurrentQueue<std::function<void()>>
                  task_queue_;  ///< 无锁任务队列（容量可限制，0=无限）
        std::vector<std::jthread> workers_;         ///< 工作线程集合
        std::atomic<bool>         stop_requested_;  ///< 停止请求标志

        /* 默认错误处理器（用于 submit_async） */
        mutable std::mutex handler_mutex_;
        std::function<void(const std::exception&)>
                  default_error_handler_;  ///< 默认错误处理器
    };
}  // namespace ecas::foundation::threadpool
