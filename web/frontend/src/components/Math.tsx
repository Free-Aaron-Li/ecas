import katex from '../vendor/katex/katex.mjs'

/**
 * 一个极薄的 KaTeX 包装：把 TeX 串渲染成 HTML。
 *
 * 先生 2026-11-04：「所有的公式、数学符号应该 latex 渲染方式展示，ρp 其中 p 应该小写在
 * 右下角」——所以页面里凡是带上下标的量（ρp、ρw、C、V、Q）都走这里，不再用裸字符拼。
 *
 * `throwOnError: false` + `errorColor`：写错一个 TeX 命令时页面显示红色原文，
 * 而不是整块崩掉（看板是现场在用的，宁可难看也不能白屏）。
 */
export function Math({ tex, display = false }: { tex: string; display?: boolean }) {
  const html = katex.renderToString(tex, {
    displayMode: display,
    throwOnError: false,
    errorColor: '#ef4444',
    strict: false,
    trust: false,
  })
  // KaTeX 输出的就是可信的静态 HTML（不含用户输入，tex 全部是代码里写死的常量）
  return <span className={display ? 'math math-display' : 'math'} dangerouslySetInnerHTML={{ __html: html }} />
}
