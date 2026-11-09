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
 * @file cpp26_hello.cpp
 * @brief C++26 工具链冒烟程序：Concepts + std::expected（Result<T>）
 *
 * 用于验证「编译器 + 标准库 + 错误传递范式」这一条最小链路：
 * 1. Concepts 约束数值类型（编译期失败而非运行期）；
 * 2. 对外接口返回 Result<T>，异常不跨边界（六条铁律第 2 条）；
 * 3. 不使用宏、不裸 new，全部 RAII 语义。
 *
 * 独立可执行，不进入 CMake 测试目标；复现命令见 docs/ai/cpp26_support.md §5。
 *
 * 编译：g++-16 -std=c++26 -Wall -Wextra -Wpedantic -Werror cpp/tests/spike/cpp26_hello.cpp
 */

#include <concepts>
#include <cstdio>
#include <expected>
#include <string>
#include <string_view>

namespace ecas::spike {

/// 只接受整数或浮点数的算术类型（编译期约束）
template <typename T>
concept Numeric = std::integral<T> || std::floating_point<T>;

/// 统一错误类型：结构化描述，由上层决定记录与处置（铁律第 3 条）
struct Error {
  std::string message;
};

/// 对外接口统一返回 Result<T>（铁律第 2 条）
template <typename T>
using Result = std::expected<T, Error>;

/// 除以二；零值与非法输入以 Error 返回，不抛异常
constexpr Result<double> half(Numeric auto value) {
  if (value == 0) {
    return std::unexpected(Error{"输入为零，拒绝计算"});
  }
  return static_cast<double>(value) / 2.0;
}

}  // namespace ecas::spike

namespace {

using ecas::spike::Error;
using ecas::spike::half;
using ecas::spike::Numeric;

static_assert(Numeric<int>);
static_assert(Numeric<double>);
static_assert(!Numeric<std::string_view>);

void report(int input) {
  const auto result = half(input);
  if (result) {
    std::printf("half(%d) = %.1f\n", input, *result);
    return;
  }
  std::printf("half(%d) -> 错误：%s\n", input, result.error().message.c_str());
}

}  // namespace

int main() {
  report(4);
  report(0);

  // 编译期求值：Concepts + expected 均可用于 constexpr 上下文
  static_assert(*half(8) == 4.0);
  std::puts("C++26 smoke: concepts + std::expected OK");
  return 0;
}
