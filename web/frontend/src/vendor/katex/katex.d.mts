export interface KatexOptions {
  displayMode?: boolean
  throwOnError?: boolean
  errorColor?: string
  strict?: boolean | string
  trust?: boolean
}

export function renderToString(tex: string, options?: KatexOptions): string

declare const katex: { renderToString: typeof renderToString }
export default katex
