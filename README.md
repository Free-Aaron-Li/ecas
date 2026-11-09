# ECAS — Edge-Cloud Acquisition System

**面向工业物联网的高可靠、高实时、可扩展数据采集与远程控制系统**

[![C++26](https://img.shields.io/badge/C%2B%2B-26-blue.svg)](https://en.cppreference.com/w/cpp/compiler_support)
[![CMake](https://img.shields.io/badge/CMake-4.3.1+-green.svg)](https://cmake.org)
[![Conan](https://img.shields.io/badge/Conan-2.x-orange.svg)](https://conan.io)
[![Qt](https://img.shields.io/badge/Qt-6.11+-brightgreen.svg)](https://qt.io)
[![IoTDB](https://img.shields.io/badge/Apache%20IoTDB-2.0+-orange.svg)](https://iotdb.apache.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18+-blue.svg)](https://www.postgresql.org)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

> ⚠️ **重构进行中**：本项目正处于开发计划的 **阶段 0（地基与验证）**。当前仅完成 CMake / Doxygen / README 等基础表述，**目录组织结构与全部代码将从零搭建**，既有旧代码将完全重构、不作为基线。详见「当前状态」。

---

## 1. 项目简介

ECAS（Edge-Cloud Acquisition System，边缘-云端数据采集系统）覆盖电力、制造、能源、环保、冶金、化工等行业，支持 **100+ 设备并发采集**，提供 **设备接入 → 协议解析 → 加密传输 → 安全存储 → SCADA 控制** 的全链路能力。

以下为设计目标，随 52 周开发计划逐步落地：

- **L1–L8 八层闭环**：设备接入 → 协议解析 → 采集控制 → 加密传输 → 双库持久 → 服务接口 → 业务应用 → Qt 6 展示
- **双库架构**：Apache IoTDB 2.0（时序测点，树形 schema）+ PostgreSQL 18（业务关系，ACID），禁止跨库 JOIN
- **安全合规**：MQTT over TLS 1.3 + mTLS、AES-256-GCM 透明加密、防篡改审计签名、许可证 / SBOM 门禁
- **插件化**：协议（Modbus / OPC UA / …）、驱动、存储后端均以动态库热替换
- **现代 C++26**：协程（`std::generator`）、`std::expected`、静态反射、契约、Concepts —— 拒绝宏黑魔法与过时特性

### 六条铁律（不可推翻，优先级高于性能与进度）

1. **RAII 全生命周期** —— 资源自诞生即被析构语义确定的对象拥有
2. **异常不跨模块边界** —— 对外接口一律返回 `Result<T>`（`std::expected`）
3. **错误 / 警告 / 日志向上管理** —— 底层只产生结构化错误，上层决定记录与处置
4. **C++26 现代化，拒绝宏黑魔法** —— 禁 `#define`、裸指针、C 遗留
5. **协程即异步范式** —— 流式 / 异步场景优先 `std::generator` / `task`
6. **契约即接口的一部分** —— 前置 / 后置条件与不变量全程校验

---

## 2. 整体架构

分层 + 微服务化，层间依赖单向向下，强制 TLS 1.3 加密通信。

| 层级 | 名称         | 核心职责                                                      |
|------|--------------|---------------------------------------------------------------|
| L1   | 设备接入层   | 物理设备与通信硬件（串口、以太网、现场总线）                  |
| L2   | 协议解析层   | 工业协议解析，统一数据模型抽象（反射辅助编解码）              |
| L3   | 采集控制层   | 任务调度、设备驱动、数据缓存、控制指令下发（协程驱动）        |
| L4   | 数据传输层   | 加密通信（MQTT over TLS 1.3）、断线缓冲、服务发现             |
| L5   | 数据持久层   | 时序库（IoTDB）、关系库（PostgreSQL）、透明加密               |
| L6   | 服务接口层   | RESTful API、WebSocket、消息订阅                              |
| L7   | 业务应用层   | 告警引擎、事件处理、控制逻辑编排、权限、工单 / 库存 / BOM     |
| L8   | 展示交互层   | Qt 6 桌面客户端（实时监控、图表、控制面板、报表导出）         |

数据流：`设备 → 协议解析 → 协程采集管线 →（MQTT over TLS 1.3）→ IoTDB / PostgreSQL → 服务接口层聚合 → Qt 客户端`；断线时边缘持久缓存，恢复后重传，数据零丢失。

---

## 3. 技术栈

| 组件           | 技术选择                                                                                     |
|----------------|----------------------------------------------------------------------------------------------|
| 语言标准       | C++26（Modules / Concepts / `expected` / `generator` / 静态反射 / 契约 / `execution`）        |
| 编译器策略     | **GCC 16.2+（首选·主战场）** / Clang 24+（对标验证） / **MSVC 暂不考虑**                      |
| 构建系统       | CMake 4.3.1+                                                                                 |
| 包管理         | Conan 2.x（`conandata.yml` 精确锁版本，禁止系统级依赖漂移）                                   |
| GUI 框架       | Qt 6.11+（Core, Widgets, Charts, Network）                                                   |
| 时序数据库     | Apache IoTDB 2.0+ LTS                                                                        |
| 关系型数据库   | PostgreSQL 18+                                                                               |
| MQTT 客户端    | redboltz-mqtt_cpp 13.2.1（基于 Asio）                                                         |
| 消息中间件     | EMQX / Mosquitto                                                                             |
| REST API       | Crow 或 Pistache                                                                             |
| XML 解析       | pugixml 1.16                                                                                 |
| 加密库         | OpenSSL 3.5.7                                                                                |
| 无锁队列       | concurrentqueue 1.0.5                                                                        |
| 日期时间       | date 3.0.5                                                                                   |
| 序列化         | Protocol Buffers                                                                             |
| 密钥管理       | 自研 KMS + HSM/TPM 支持                                                                      |
| 单元测试       | Catch2 3.15.3                                                                                |
| 文档 / SBOM    | Doxygen / CycloneDX / SPDX                                                                   |

---

## 4. 目录结构（目标，搭建中）

```text
ecas/
├── CMakeLists.txt          # 顶层构建配置
├── README.md
├── conandata.yml           # 依赖版本单一事实源（Conan）
├── conanfile.py            # Conan 配方（CMakeDeps + CMakeToolchain）
├── cmake/                  # CMake 辅助脚本
├── src/                    # 核心源码（命名空间分层：ecas::<layer>::<module>）
│   ├── foundation/         # 基础库：error / log / raii / config / coroutine /
│   │                       #   reflect / contracts / crypto / audit / concepts
│   ├── acquisition/        # L1–L3 采集：driver / protocol / scheduler / buffer / control
│   ├── communication/      # L4 传输：mqtt / gateway / discovery / channel
│   ├── storage/            # L5 持久：iotdb / postgresql / cache / encryption
│   ├── application/        # L6–L7 业务：settings / config_watcher / node_manager /
│   │                       #   alert_engine / event_bus / control_engine / audit_manager
│   └── ui/                 # L8 展示（Qt 6）：main / dashboard / device_panel /
│                           #   history / alarm / control / audit / config
├── plugins/                # 插件（动态库）：protocols / drivers / storage / control
├── tests/                  # 单元 / 集成 / 协程·反射·契约测试
├── doc/                    # 设计与计划文档（见「文档地图」）
├── deploy/                 # 部署：Docker Compose / K8s / 配置样例
├── scripts/                # 构建与运维脚本
└── resources/              # 图标、翻译、样式
```

> 采用「命名空间 + 清晰头文件分层」为主（Modules 列为后续可选优化，不阻塞主干）；每模块仅导出必要接口，依赖通过抽象接口注入，禁止循环依赖。

---

## 5. 文档地图

| 文档                                                                        | 回答的问题                                        |
|-----------------------------------------------------------------------------|---------------------------------------------------|
| [docs/design/全局设计.md](docs/design/全局设计.md)                             | 系统是什么、架构分层、数据流、技术选型、部署、CI  |
| [docs/design/实现设计.md](docs/design/实现设计.md)                             | 代码怎么写：RAII / 错误处理 / 协程 / 反射 / 契约  |
| [docs/plan/ECAS单人全功能开发计划.html](docs/plan/ECAS单人全功能开发计划.html) | 52 周路线图（6 阶段 / 里程碑 / DoD / 取舍）       |
| [docs/plan/ECAS单人全功能开发计划.xlsx](docs/plan/ECAS单人全功能开发计划.xlsx) | 逐日 / 逐周执行表（唯一执行载体，含状态列）       |
| [docs/cpp26_support.md](docs/ai/cpp26_support.md)                               | C++26 工具链支持报告（T1 完成；T2–T5 追加）      |
| [docs/VSCode远程编辑教程.md](docs/VSCode远程编辑教程.md)                         | 怎么用 VS Code 远程看/改现网源码（`/opt/ecas`）并生效 |
| [docs/开发到上线-分步操作手册.md](docs/开发到上线-分步操作手册.md)                 | 手把手：打开项目 → 本地跑 → 热加载 → 推服务器 → 回传 |

---

## 6. 当前状态（阶段 0 · 地基与验证）

| 项                                   | 状态                                   |
|--------------------------------------|----------------------------------------|
| CMake 构建骨架 / Doxygen / README    | ✅ 已完成（重构基础表述）              |
| 目录组织结构                         | ✅ 骨架已搭建（49 个模块目录 + 占位说明）|
| C++26 特性验证（反射 / 契约 / 协程） | ⏳ 待开始                              |
| Conan 依赖解析与锁版本               | ⏳ 待开始                              |
| CI（GCC 16.2 + Clang 24）            | ⏳ 待开始                              |
| 错误 / 日志 / RAII 基础设施          | ⏳ 待开始                              |

里程碑：M1 工具链与基础设施就绪（2026-10-07）→ M2 采集链路闭环（2026-12-23）→ M3 传输持久闭环（2027-02-17）→ M4 全栈可演示（2027-04-14）→ M5 插件与部署（2027-07-01）→ M6 交付（2027-08-18），详见开发计划。

---

## 7. 构建与测试

前置要求：CMake 4.3.1+、Conan 2.x、**GCC 16.2+**（首选）或 Clang 24+（对标）。

```bash
# 1. 安装依赖（版本由 conandata.yml 锁定）
conan install . --output-folder=build --build=missing

# 2. 配置（使用 Conan 生成的 toolchain）
cmake -B build -S . -DCMAKE_TOOLCHAIN_FILE=build/conan_toolchain.cmake \
      -DCMAKE_BUILD_TYPE=Release

# 3. 构建
cmake --build build --parallel

# 4. 测试
ctest --test-dir build --output-on-failure
```

> 编译要求 `-std=c++26`，警告全开并视为错误（`-Wall -Wextra -Wpedantic -Werror`）。可执行组件（`ecas-edge` / `ecas-central` / `ecas-ui`）随开发阶段逐步产出，当前仓库尚无。

---

## 8. 开发与贡献

- 分支策略：Git Flow（main / develop / feature/*）
- 代码风格：clang-format + clang-tidy（`cppcoreguidelines-*` / `bugprone-*` / `modernize-*` / `*-raii` / `*-expected` / `readability-avoid-macros`）
- 提交信息：遵循 Conventional Commits
- 测试要求：新功能必须带单元测试（Catch2），且遵守六条铁律；CI 门禁含 sanitizer（ASan/UBSan/TSan）、许可证扫描、SBOM

---

## 9. 许可证

本项目使用 **Apache License 2.0**（见 [LICENSE](LICENSE)），符合《全局设计》§1.2「仅商业友好许可证」的合规约束。

---

ECAS — 为工业 4.0 构建安全、可控、合规、RAII、协程驱动、C++26 现代化的数据基座。
