# ECAS (Energy Consumption Acquisition System)

高效的能源消耗数据采集系统项目。

## 1. 项目简介

本项目是一个采用现代 C++（Modern C++）开发的能源消耗数据采集系统。ECAS 旨在提供一个高性能、模块化的数据采集方案。所有代码均附带详尽的
Doxygen 风格注释。

## 2. 环境要求

- **CMake**: 最低版本要求 3.22 (示例中为 4.2，但通常建议 3.22+)
- **Vcpkg**: 用于管理第三方依赖包
- **编译器**: 需要支持 C++23 的编译器（推荐 GCC 15+, Clang 22+）
- **Doxygen**: 用于从源代码注释中生成项目文档
- **Graphviz**: (可选) 用于生成 Doxygen 继承图、调用图等可视化图表

## 3. 项目结构

```txt
.
├── acquisition      # 数据采集模块 (ecas::ac::acquisition)
├── application      # 终端应用模块 (ecas::ac::application)
├── foundation       # 基础核心模块 (ecas::fd)
├── doxygen_tools    # Doxygen 相关辅助工具
├── .clang-format    # Clang-format 格式化配置文件
├── .gitignore       # git 忽略文件配置
├── CMakeLists.txt   # 项目根构建文件
├── LICENSE          # 版权声明
└── README.md        # 项目说明
```

## 4. 如何构建

### 4.1 依赖管理

本项目通过 **Vcpkg** 管理第三方依赖包，并使用清单模式（Manifest Mode），即通过项目根目录下的 `vcpkg.json` 文件自动管理依赖。

在配置 CMake 时，请确保指定 `vcpkg.cmake` 工具链文件。

### 4.2 默认构建方式

1. **克隆项目**:
   ```bash
   git clone https://github.com/Free-Aaron-Li/ecas.git
   cd ecas
   ```

2. **使用 CMake 构建**:
   ```bash
   # 请将 /path/to/vcpkg 替换为你的 vcpkg 实际安装路径
   cmake -B build -S . -DCMAKE_TOOLCHAIN_FILE=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
   cmake --build build -j${nproc}
   ```

3. **运行程序**:
   ```bash
   ./bin/application
   ```

### 4.3 CMake 参数

除去常规参数设置外，本项目还具备以下自定义参数：

- `ENABLE_DOXYGEN`, 是否启用 Doxygen 文档生成功能，默认开启；
- `ENABLE_DOXYGEN_AUTO_UPDATE`，是否在构建时自动更新文档，默认开启；
- `ENABLE_DOXYGEN_GRAPH`，是否启用图表生成功能（需安装 Graphviz），默认关闭。

## 5. 关于文档

运行该项目会自动生成 `Doxygen` 文档，查看文档请通过 `doxygen/html/index.html`。 如果安装了 `Graphviz` 并且启用了
`ENABLE_DOXYGEN_GRAPH`，文档中将包含类继承图、函数调用图等。

**注意**：如果你在构建过程中修改了 `ENABLE_DOXYGEN_GRAPH` 的值，请务必 **重新运行 CMake 配置**以刷新生成的 `Doxyfile`。

如希望手动生成文档，请在顶层目录下使用命令（假设使用 `build` 作为构建目录）：

```bash
doxygen build/Doxyfile
```

**切勿**直接对 `doxygen_tools/Doxyfile.in` 运行 doxygen，因为该模板文件包含 CMake 变量占位符，直接运行会导致配置失效。

如果希望查看 `Doxygen` 与默认配置区别，请通过命令：

```bash
doxygen -x build/Doxyfile
```

## 6. 许可证

本项目遵循 [GNU General Public License v3.0](LICENSE) 开源协议。
