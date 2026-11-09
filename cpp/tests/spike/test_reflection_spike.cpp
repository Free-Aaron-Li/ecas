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
 * @file test_reflection_spike.cpp
 * @brief C++26 静态反射 Spike 测试用例（基于 Catch2 3）
 *
 * 验证：
 * 1. 编译期概念（Concept）约束及成员数量推导（member_count）；
 * 2. 编译期成员名称提取（member_names）；
 * 3. 运行期字段遍历与通用动态序列化（for_each_field visitor）。
 */

#include <catch2/catch_test_macros.hpp>
#include <string>
#include <vector>

#include "reflection_spike.hpp"

/**
 * @brief 测试用例模型：样本设备结构体
 */
struct SampleDevice {
    int         id;       ///< 设备唯一标识 ID
    double      voltage;  ///< 电压采样值（V）
    std::string name;     ///< 设备名称
};

namespace ecas::tests::spike::reflect::traits {

    /**
     * @brief SampleDevice 的反射元数据特化
     *
     * 为 SampleDevice
     * 注册静态反射元信息（字段计数、字段名列表、以及字段访问器）。
     */
    template<>
    struct reflect_traits<SampleDevice> {
        /// 成员字段总数
        static constexpr std::size_t member_count = 3;

        /// 编译期获取成员名称列表
        static consteval auto
        member_names() noexcept {
            return std::array<std::string_view, 3>{ "id", "voltage", "name" };
        }

        /// 字段遍历访问器实现，按声明顺序依次转发成员
        template<typename Self, typename Func>
        static constexpr void
        for_each_field(Self&& self, Func&& func) {
            func("id", self.id);
            func("voltage", self.voltage);
            func("name", self.name);
        }
    };

}  // namespace ecas::tests::spike::reflect::traits

/**
 * @test 验证静态反射概念约束（Reflectable Concept）及成员数量计算
 */
TEST_CASE("Static reflection spike - feature detection and member count",
          "[spike][reflection]") {
    // 1. 编译期验证 SampleDevice 满足 Reflectable 概念约束
    STATIC_REQUIRE(ecas::tests::spike::reflect::Reflectable<SampleDevice>);

    // 2. 编译期与运行期同时验证字段总数为 3
    constexpr auto count
              = ecas::tests::spike::reflect::member_count<SampleDevice>();
    STATIC_REQUIRE(count == 3);
    REQUIRE(count == 3);
}

/**
 * @test 验证编译期成员名称提取能力
 */
TEST_CASE("Static reflection spike - member names extraction",
          "[spike][reflection]") {
    // 1. 编译期提取成员名列表
    constexpr auto names
              = ecas::tests::spike::reflect::member_names<SampleDevice>();
    STATIC_REQUIRE(names.size() == 3);

    // 2. 验证各个成员名称顺序与命名正确性
    REQUIRE(names[0] == "id");
    REQUIRE(names[1] == "voltage");
    REQUIRE(names[2] == "name");
}

/**
 * @test 验证基于 for_each_field 的通用字段访问与序列化能力
 */
TEST_CASE("Static reflection spike - for_each_field visitor and serialization",
          "[spike][reflection]") {
    // 构造测试样本实例
    SampleDevice dev{ .id = 101, .voltage = 220.5, .name = "Sensor-A" };

    std::vector<std::string> inspected_names;
    std::vector<std::string> inspected_values;

    // 使用通用 Lambda 访问者模式遍历字段并进行类型识别与格式化
    ecas::tests::spike::reflect::for_each_field(dev, [&](std::string_view name,
                                                         const auto& value) {
        inspected_names.emplace_back(name);
        if constexpr (std::is_same_v<std::decay_t<decltype(value)>, int>) {
            inspected_values.push_back(std::to_string(value));
        } else if constexpr (std::is_same_v<std::decay_t<decltype(value)>,
                                            double>) {
            inspected_values.push_back(std::to_string(value).substr(0, 5));
        } else if constexpr (std::is_same_v<std::decay_t<decltype(value)>,
                                            std::string>) {
            inspected_values.push_back(value);
        }
    });

    // 验证遍历得到的字段名列表
    REQUIRE(inspected_names.size() == 3);
    REQUIRE(inspected_names[0] == "id");
    REQUIRE(inspected_names[1] == "voltage");
    REQUIRE(inspected_names[2] == "name");

    // 验证各字段序列化结果
    REQUIRE(inspected_values.size() == 3);
    REQUIRE(inspected_values[0] == "101");
    REQUIRE(inspected_values[1] == "220.5");
    REQUIRE(inspected_values[2] == "Sensor-A");
}
