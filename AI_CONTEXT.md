# AI_CONTEXT.txt - ECAS 项目核心约束

本文件为 AI 辅助编程的强制上下文。在编写任何代码前，**必须**完整阅读并遵守以下规则。

---

## 项目架构
- 采用 C++23 模块化设计（`.cppm` 文件）。
- 目录结构：
    - `foundation/`：公共基础库（类型、日志、工具）
    - `acquisition/`：数据采集相关（MBP 解析、通信）
    - `application/`：应用层（配置加载、主程序）
- 构建系统：CMake 3.28+，使用 vcpkg 管理依赖。

---

## 核心类型定义（foundation/types.cppm）
- 所有公共数据结构、错误码、`Error` 链必须在 `ecas::foundation::types` 命名空间内定义。
- **错误处理规范**：
    - 使用 `std::expected<T, ecas::foundation::types::Error>` 作为返回值。
    - 自定义错误消息通过 `Error` 的 `message` 字段传递，并始终包含错误码（`ErrorCode`）。
    - 错误链通过 `Error::cause` 嵌套，使用 `format_error_chain()` 输出。
    - 新增错误码或修改 `Error` 结构必须同步更新 `error_code_description` 和 `format_error_chain`。
- **Modbus 数据类型**：
    - `DataType` 枚举必须与 MBP 文件中的格式代码映射一致（见 `map_format_code_to_datatype`）。
    - `RegisterMeta` 包含 `name`、`unit`、`dataType`、`scale`、`offset`、`description`。
- **配置结构**：`MbpConfig` 包含 `slaveId`、`function`、`address`、`quantity` 和 `registerMap`（`std::map<int32_t, RegisterMeta>`，保证有序）。

---

## 日志系统（foundation/logger.cppm）
- 日志接口位于 `ecas::foundation::logger` 命名空间。
- **必须使用** `logger::info()`、`logger::warning()`、`logger::error()`、`logger::debug()` 记录日志。
- **禁止**直接使用 `std::cout`、`std::cerr` 或 `std::println`（除 main 中早期错误处理外）。
- **所有 IO 必须异步**：日志已通过 `AsyncDispatcher` 实现异步写入，新增任何文件或网络 IO 也需设计为异步（如使用 `std::jthread` + 队列）。
- 日志初始化通过 `logger::init(path)`，关闭通过 `logger::shutdown()`。推荐使用 `LoggerGuard` RAII 管理生命周期。
- 时间缓存已优化：后台线程每秒更新一次时间字符串，日志格式化直接引用缓存。不得在每行日志中重复调用 `std::chrono::zoned_time`。

---

## 配置加载（application/config_loader_interface.cppm）
- 配置加载器接口为 `IConfigLoader`，定义 `load(const std::string& path)` 返回 `std::expected<MbpConfig, Error>`。
- 具体实现通过 `ConfigLoaderRegistry` 注册扩展名和工厂函数（使用 `std::move_only_function`）。
- 所有配置加载（MBP、JSON、YAML 等）**必须**实现此接口，并自动注册到注册表。
- `run` 函数**禁止**直接导入具体解析器（如 `mbp_parser`），只能依赖 `IConfigLoader` 接口。

---

## 编码约束
- **必须使用 C++23 特性**：`std::expected`、`std::move_only_function`、`std::jthread`、`std::atomic_shared_ptr`、`std::filesystem::path` 等。
- **禁止使用 C 风格 API**（如 `printf`、`fopen`），除非在极底层实现中且已封装。
- **命名空间**：所有代码位于 `ecas::` 下，子模块对应 `ecas::foundation`、`ecas::acquisition`、`ecas::application`。
- **模块导入**：必须使用 `import` 语句，不要使用 `#include` 导入模块接口（头文件仅用于非模块代码，如 `main.cpp` 中的标准库包含）。

---

## 关键文件位置（避免重复定义）
- 核心类型：`foundation/src/types.cppm`
- 日志实现：`foundation/src/logger.cppm`
- MBP 解析器：`acquisition/src/parser/mbp_parser.cppm`
- 应用设置：`application/src/settings.cppm`
- 配置加载接口：`application/src/config_loader_interface.cppm`
- 配置加载实现：`application/src/config_loader.cppm`
- 运行主逻辑：`application/src/run.cppm`
- 入口：`application/src/main.cpp`

---

## 测试与维护
- 单元测试使用 Catch2，测试文件放在各子模块的 `test/` 目录下。
- 所有解析函数（如 `parse_stoi`、`map_format_code_to_datatype`）应移至可测试的命名空间，并暴露给单元测试。
- 新增功能必须同步更新 Doxygen 文档（`doxygen_tools/`）。

---

## 修改规则
- 修改以上任何约束时，必须在 `AI_CONTEXT.txt` 中同步更新，并注明变更原因。
- AI 在生成代码前，必须先读取此文件，确保设计决策符合项目架构。