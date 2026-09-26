# doxygen_tools

Doxygen API 文档生成工具包与配置资源。

- **层**：基础设施
- **职责**：提供 Doxygen 文档构建模板、现代样式主题、自定义布局、预处理过滤与外部符号索引
- **状态**：✅ 已就绪

## 文件结构

- `Doxyfile.in`：Doxygen 主配置文件模板，由 CMake 的 `configure_file` 处理后生成构建目录下的 `Doxyfile`
- `DoxygenLayout.xml`：自定义 Doxygen 页面与导航栏布局配置
- `doxygen-awesome.css`：Doxygen Awesome CSS 现代扁平主题样式
- `filter.sh`：Markdown 过滤器脚本，用于处理自定义标记语法（如 `==高亮==`、跨文件引用等）
- `footer.html`：自定义 HTML 页面底部模板
- `cppreference-doxygen-web.tag.xml`：C++ 标准库 Doxygen Tagfile，提供 cppreference 外部链接索引
- `docs/`：文档源文件目录
    - `mainpage.dox`：Doxygen 文档首页内容
    - `namespaces.dox`：全局与各子系统命名空间说明

## 使用方式

在顶层 CMakeLists.txt 中已集成 Doxygen 目标：

```bash
# 1. 确保系统已安装 doxygen（可选安装 graphviz 以提供 dot 图表支持）
# 2. 生成构建配置（默认开启 ENABLE_DOXYGEN=ON）
cmake -B build -S .

# 3. 运行文档生成
cmake --build build --target docs
```

生成后的文档将输出至项目根目录下的 `doxygen/html` 目录，通过浏览器打开 `doxygen/html/index.html` 即可浏览。
