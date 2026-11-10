"use strict";
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const katexHTML = (tex, display) =>
  katex.renderToString(tex, { displayMode: display, throwOnError: false, output: "html" });
marked.use({
  gfm: true,
  breaks: true,
  extensions: [
    {
      // 块级公式：$$…$$ 或 \[…\] 独占段落
      name: "mathBlock",
      level: "block",
      start: (src) => {
        const i = src.search(/\$\$|\\\[/);
        return i < 0 ? undefined : i;
      },
      tokenizer(src) {
        const m = /^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])[ \t]*(?:\n|$)/.exec(src);
        if (m) return { type: "mathBlock", raw: m[0], text: (m[1] ?? m[2]).trim() };
      },
      renderer: (t) => katexHTML(t.text, true),
    },
    {
      // 行内公式：$…$（首尾非空白、结尾 $ 后不是数字，避免把 "$5 和 $10" 当公式）、\(…\)、行内 $$…$$ / \[…\]
      name: "mathInline",
      level: "inline",
      start: (src) => {
        const i = src.search(/\$|\\[([]/);
        return i < 0 ? undefined : i;
      },
      tokenizer(src) {
        let m;
        if ((m = /^\$\$([\s\S]+?)\$\$/.exec(src) || /^\\\[([\s\S]+?)\\\]/.exec(src)))
          return { type: "mathInline", raw: m[0], text: m[1], display: true };
        if (
          (m =
            /^\\\(([\s\S]+?)\\\)/.exec(src) ||
            /^\$(?!\s)((?:\\.|[^\\$\n])+?)(?<!\s)\$(?!\d)/.exec(src))
        )
          return { type: "mathInline", raw: m[0], text: m[1], display: false };
      },
      renderer: (t) => katexHTML(t.text, t.display),
    },
  ],
  renderer: {
    html: (t) => esc(t.text),
    image: (t) => `<a href="${esc(t.href)}">[图片] ${esc(t.text || t.href)}</a>`,
    code(t) {
      const lang = (t.lang || "").trim().split(/\s+/)[0];
      const body =
        lang && hljs.getLanguage(lang)
          ? hljs.highlight(t.text, { language: lang, ignoreIllegals: true }).value
          : esc(t.text);
      return (
        `<div class="codeblk"><div class="cbh"><span>${esc(lang || "text")}</span>` +
        `<button class="mbtn" data-act="copycode">复制</button></div>` +
        `<pre><code class="hljs">${body}</code></pre></div>`
      );
    },
  },
});
DOMPurify.addHook("afterSanitizeAttributes", (n) => {
  if (n.tagName === "A") {
    n.setAttribute("target", "_blank");
    n.setAttribute("rel", "noopener noreferrer");
  }
});
function renderMarkdown(text) {
  try {
    return DOMPurify.sanitize(marked.parse(text));
  } catch (e) {
    return esc(text);
  } // 解析异常时退回纯文本，不影响其他消息
}
