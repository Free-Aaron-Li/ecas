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
 * @file reflection_spike.hpp
 * @brief C++26 静态反射 Spike 基础框架定义
 *
 * 提供基于概念约束与特化 traits 的静态反射接口定义：
 * 1. reflect_traits 特化模板声明；
 * 2. Reflectable 概念定义；
 * 3. member_count / member_names 编译期访问辅助函数；
 * 4. for_each_field 访问者遍历调度函数。
 */

#ifndef ECAS_REFLECTION_SPIKE_HPP
#define ECAS_REFLECTION_SPIKE_HPP

#include <concepts>
#include <cstddef>
#include <type_traits>
#include <utility>

namespace ecas::tests::spike::reflect {
    namespace traits {

        /**
         * @brief 反射元数据 traits 模板声明
         *
         * 针对需要支持反射的类型进行特化，需提供：
         * - static constexpr std::size_t member_count
         * - static consteval / constexpr auto member_names()
         * - static constexpr void for_each_field(Self&&, Func&&)
         */
        template<typename T>
        struct reflect_traits;
    }  // namespace traits

    /**
     * @brief Reflectable 概念约束
     *
     * 校验目标类型是否特化了 reflect_traits 并满足静态反射所需接口
     */
    template<typename T>
    concept Reflectable = requires {
        typename traits::reflect_traits<std::remove_cvref_t<T>>;
        {
            traits::reflect_traits<std::remove_cvref_t<T>>::member_count
        } -> std::convertible_to<std::size_t>;
        { traits::reflect_traits<std::remove_cvref_t<T>>::member_names() };
    };

    /**
     * @brief 编译期获取反射类型的成员字段数量
     */
    template<Reflectable T>
    [[nodiscard]] consteval std::size_t
    member_count() noexcept {
        return traits::reflect_traits<std::remove_cvref_t<T>>::member_count;
    }

    /**
     * @brief 编译期获取反射类型的成员字段名称列表
     */
    template<Reflectable T>
    [[nodiscard]] consteval auto
    member_names() noexcept {
        return traits::reflect_traits<std::remove_cvref_t<T>>::member_names();
    }

    /**
     * @brief 遍历反射类型的所有成员字段
     *
     * @param obj 待访问的目标对象
     * @param func 访问者函数或 Lambda，接受 (name, value) 参数
     */
    template<typename T, typename Func>
        requires Reflectable<std::remove_cvref_t<T>>
    constexpr void
    for_each_field(T&& obj, Func&& func) {
        traits::reflect_traits<std::remove_cvref_t<T>>::for_each_field(
                  std::forward<T>(obj), std::forward<Func>(func));
    }

}  // namespace ecas::tests::spike::reflect

#endif  // ECAS_REFLECTION_SPIKE_HPP
