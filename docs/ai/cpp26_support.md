# ECAS C++26 工具链支持报告（T1 工具链验证）

> 状态：✅ 已完成（2026-08-30）
> 依据：看板任务 T1「工具链验证」；目标工具链见《全局设计》§3.2 / §8.2
> 冒烟程序：`../../cpp/tests/spike/cpp26_hello.cpp`（concepts + `std::expected` → `Result<T>`，六条铁律合规）

---

## 1. 本机编译器版本清单

| 编译器                            | 版本                                       | 路径                                 | 设计文档角色                        | 实测结论                                   |
|-----------------------------------|--------------------------------------------|--------------------------------------|-------------------------------------|--------------------------------------------|
| Homebrew GCC                      | **16.2.0**（gcc-16 / g++-16）              | `/opt/homebrew/bin/gcc-16`、`g++-16` | **主战场**（§3.2 首选）             | ✅ 达标，可用                              |
| Homebrew LLVM / Clang             | **23.1.0**（llvm-config 23.1.0）           | `/opt/homebrew/opt/llvm/bin/clang++` | **对标验证**（Clang 24+）           | ⚠️ 可用，但**低于目标 24**（差一个主版本） |
| Apple clang（默认 `gcc` / `g++`） | **21.0.0**（clang-2100.1.1.101，SDK 26.5） | `/usr/bin/gcc`、`/usr/bin/g++`       | 非目标（系统默认，实为 clang 伪装） | ⚠️ 信息项：可用但非目标工具链              |
| MinGW-w64（GCC 16.2+）            | **未安装**                                 | —                                    | Windows 端（§8.2）                  | ⚠️ 本机缺口（由 CI 提供）                  |
| CMake                             | **4.4.3**                                  | `cmake`                              | README 要求 4.3.1+                  | ✅ 达标                                    |

> 注意：macOS 上默认 `gcc`/`g++` 指向 **Apple clang**（`/usr/bin/gcc`）， **不是真实 GCC**。真实 GCC 16.2 由 Homebrew
> 以版本化命名安装（`gcc-16` / `g++-16`），不会覆盖系统命令。

## 2. 冒烟验证结果（`-std=c++26`）

程序：`../../cpp/tests/spike/cpp26_hello.cpp` —— 最小 hello，演示：

- **concepts**：`ecas::Arithmetic`（`std::integral<T> or std::floating_point<T>`），`divide` 模板受约束，含 `static_assert`
  编译期校验；
- **`std::expected` → `Result<T>`**：铁律 #2 合规——对外不抛异常，除零失败以 `std::unexpected{Error::invalid_input}` 返回；
- **铁律 #4 合规**：无 `#define`、无 `printf`、无 C 遗留（输出用 `std::println`）。

编译命令（`-std=c++26 -Wall -Wextra`，零警告）：

| 编译器                               | 编译    | 运行输出                                                                                                      | 退出码 |
|--------------------------------------|---------|---------------------------------------------------------------------------------------------------------------|--------|
| `g++-16`（GCC 16.2.0）               | ✅ 通过 | `hello from ECAS C++26 toolchain smoke test` → `divide(10, 2) -> 5` / `divide(10, 0) -> Error::invalid_input` | 0      |
| Homebrew `clang++`（23.1.0）         | ✅ 通过 | 同上                                                                                                          | 0      |
| `/usr/bin/g++`（Apple clang 21.0.0） | ✅ 通过 | 同上                                                                                                          | 0      |

结论： **concepts + `std::expected` 在本机全部三套编译器上均可用**，主战场 GCC 16.2 完全达标。

## 3. 可用性结论

1. **主战场 GCC 16.2.0 达标可用** ✅ —— 与《全局设计》§3.2 完全一致；项目已有 `../../cmake-build-debug-gcc16` 与链接
   libstdc++ 的 `../../bin/ecas`，构建链路此前已打通。
2. **对标 Clang 已可用但版本落后** ⚠️ —— 实测 23.1.0 可编译运行本冒烟程序；但设计目标为 **Clang 24+**，当前差一个主版本。
3. **Apple clang 21 可用但非目标** —— 系统默认 `gcc` 实为它；本轮实测其 `-std=c++26` 下 concepts/expected/print
   均可用，但按设计文档不承担特性承诺，仅记录。
4. **MinGW-w64 本机未装** —— 设计矩阵中 Windows 腿由 CI（`windows-latest` + MinGW-w64 + GCC 16.2）提供，本机非必需，属预期缺口。
5. **未验证项（后续 spike）** —— 本轮仅冒烟 concepts + `std::expected`； **契约（`-fcontracts`）、静态反射、`std::execution`、
   `std::generator` 协程** 未覆盖，需看板 T2–T4 继续（C++26 spike 剩余项）。

## 4. 缺口与解决方案

| #  | 缺口                                                        | 影响                                                                                  | 解决方案                                                                                                             | 优先级 |
|----|-------------------------------------------------------------|---------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|--------|
| G1 | 默认 `gcc`/`g++` 是 **Apple clang 21** 伪装，非真实 GCC     | 按 README 直接敲 `gcc` 会误用非目标工具链，产生假阴性                                 | 统一用 `/opt/homebrew/bin/g++-16`（或 `gcc-16`）；CI/脚本中显式指定，或把 `/opt/homebrew/bin` 置于 PATH 前部并 alias | 高     |
| G2 | Clang 目标 **24+**，实际 **23.1.0**（Homebrew llvm 23.1.0） | 对标验证低于设计目标一个主版本；反射/execution 支持度按设计走 `if constexpr` 降级分支 | `brew upgrade llvm` 至 24+（等 brew 发布新版）；短期内用 23.1 对标 + 既定降级分支（§3.2 已规定）                     | 中     |
| G3 | **MinGW-w64 未安装**                                        | Windows 腿（§8.2）无法本机交叉验证                                                    | CI 由 `windows-latest` 提供；本地如需验证 `brew install mingw-w64`                                                   | 低     |
| G4 | Apple clang 21 非目标工具链                                 | 特性承诺不覆盖它，避免"能用但降级"混淆                                                | 仅在移植性冒烟中使用；正式验证一律走 GCC 16.2                                                                        | 低     |

## 5. 复现命令

```bash
cd /Users/lijc/source/qt/ecas

# 主战场：GCC 16.2
/opt/homebrew/bin/g++-16 -std=c++26 -Wall -Wextra cpp/tests/spike/cpp26_hello.cpp -o /tmp/cpp26_hello_gcc16
/tmp/cpp26_hello_gcc16

# 对标：Homebrew Clang 23.1
/opt/homebrew/opt/llvm/bin/clang++ -std=c++26 -Wall -Wextra cpp/tests/spike/cpp26_hello.cpp -o /tmp/cpp26_hello_clang23
/tmp/cpp26_hello_clang23

# 探测：系统默认 g++（Apple clang 21）
/usr/bin/g++ -std=c++26 -Wall -Wextra cpp/tests/spike/cpp26_hello.cpp -o /tmp/cpp26_hello_apple
/tmp/cpp26_hello_apple
```

版本核对命令：`/opt/homebrew/bin/gcc-16 --version`、`/opt/homebrew/opt/llvm/bin/clang++ --version`、
`/usr/bin/g++ --version`、`cmake --version`。

## 6. 参照

- 《全局设计》§3.2「工具链策略：GCC 16.2 主战场 / Clang 24 对标 / MSVC 不考虑」
- 《全局设计》§8.2「Windows 工具链：MinGW-w64（GCC 16.2+）」
- 《实现设计》§0「六条铁律」、§7「协程与异步模型」、§9「契约」
- 看板：T1 工具链验证（本报告）→ T2 静态反射 spike / T3 契约 spike / T4 协程 spike / T5 特性矩阵文档
