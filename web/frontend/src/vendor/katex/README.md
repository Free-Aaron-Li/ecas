# KaTeX 便携副本（vendored）

来源：npm `katex@0.16.11`（MIT，见同目录 `LICENSE`）。
只保留了 `dist/katex.mjs`、`dist/katex.min.css` 与 `dist/fonts/*.woff2` 三样，供页面里的
公式（干矿量 Q = V / (1/ρp + (1−C)/C) 等）做 LaTeX 渲染。

为什么不走 `pnpm add katex`：本机 pnpm 11 与仓库 node_modules 的 pnpm 12 store 不兼容，
安装会破坏现有依赖树；这份副本是直接从 npm tarball 取的原始文件，未做任何改动。

升级方式：`npm pack katex@<新版本>` → 解开 `package/dist` → 覆盖同名文件 → 重新 `pnpm build`。
