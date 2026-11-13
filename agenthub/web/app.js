"use strict";
const safe =
  (fn) =>
  async (...args) => {
    try {
      return await fn(...args);
    } catch (e) {
      toast(e.message);
    }
  };
const $ = (s) => document.querySelector(s);
const TOOLNAMES = {
  claude: "CLAUDE",
  codex: "CODEX",
  dsh: "DSH",
  hermes: "HERMES",
  opencode: "OPENCODE",
  workbuddy: "WORKBUDDY",
};
const TOOLCOLOR = {
  claude: "var(--c-claude)",
  codex: "var(--c-codex)",
  dsh: "var(--c-dsh)",
  hermes: "var(--c-hermes)",
  opencode: "var(--c-opencode)",
  workbuddy: "var(--c-wb)",
};
const state = {
  children: [], // 当前会话的直接子任务（DSH 子代理 / 派生会话）
  tool: "",
  q: "",
  cwd: "",
  offset: 0,
  total: 0,
  view: "sessions",
  current: null,
  msgOffset: 0,
  msgTotal: 0,
  msgMode: "chat",
  msgOrder: "asc",
  listSeq: 0,
  msgSeq: 0,
  sel: new Map(),
  lastSel: null, // sel：稳定消息 ID → 消息，跨视图 / 分页保留
  sessSel: new Map(),
  models: null,
}; // sessSel："tool/id" → 会话，跨过滤 / 搜索保留
const sessPath = (s) => `${encodeURIComponent(s.tool)}/${encodeURIComponent(s.session_id)}`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2200);
}
function busy(on, text) {
  $("#loading").classList.toggle("show", !!on);
  if (text) $("#loadingText").textContent = text;
}
const fmtN = (n) => (n >= 10000 ? (n / 10000).toFixed(1) + " 万" : String(n));

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg || "已复制");
  } catch (e) {
    toast("复制失败：" + e.message);
  }
}

/* 保存文本文件。
   桌面版（pywebview）里 a.download 落不到用户想要的位置，改调原生「另存为」；
   浏览器里仍旧用 Blob 下载，行为不变。 */
async function saveText(filename, text, okMsg) {
  const bridge = window.pywebview && window.pywebview.api;
  if (bridge && bridge.save_text) {
    try {
      const res = await bridge.save_text(filename, text);
      if (res && res.ok) toast("已保存到 " + res.path);
      else if (res && res.cancelled) toast("已取消");
      else toast("保存失败：" + ((res && res.error) || "未知错误"));
    } catch (e) {
      toast("保存失败：" + e.message);
    }
    return;
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
  toast(okMsg || "已下载");
}

/* ---------- Markdown 渲染：marked + KaTeX + highlight.js，DOMPurify 清洗 ----------
   对话内容不可信：原始 HTML 一律转义显示（也让 <user_query> 这类标签原样可见），
   图片不加载（避免外链请求），只显示为链接。 */
/* ---------- 顶栏统计 + 过滤 chips ---------- */
/* ---------- 会话列表 ---------- */
/* ---------- 工作目录聚合 ---------- */
function switchView(v) {
  state.view = v;
  document.querySelectorAll(".vt").forEach((t) => t.classList.toggle("on", t.dataset.view === v));
  if (v === "cwd") loadCwd();
  else loadList(true);
}

/* ---------- 会话详情 ---------- */
function updateCount() {
  const ord = state.msgOrder === "asc" ? "最早在前" : "最新在前";
  const label = { chat: "对话视图", user: "用户输入", ai: "AI 回答", full: "全程视图" }[state.msgMode];
  const unit = state.msgMode === "full" ? "条" : "条正文";
  $("#msgcount").textContent = `${label} · ${state.msgTotal} ${unit} · ${ord}`;
}
/* DSH 等工具的协作事件：按来源给出可读标签 */
const EVENT_LABELS = [
  ["agent-message", "子任务回报"],
  ["subagent-report", "子任务回报"],
  ["subagent-settled", "子任务结束"],
  ["coordinator", "主任务来信"],
  [":goal", "目标轮次"],
  ["subagent/catalog", "创建子任务"],
  ["goal/change", "目标"],
  ["tool-workflow", "工作流"],
  ["subagent-activity", "子代理"],
  ["context-compaction", "上下文压缩"],
  ["claude:bash", "终端命令"],
];
const eventLabel = (m) => (EVENT_LABELS.find(([k]) => (m.source || "").includes(k)) || [0, "协作"])[1];
async function openById(tool, id) {
  const d = await api("/api/session/" + encodeURIComponent(tool) + "/" + encodeURIComponent(id));
  await openSession(d.session);
}
function renderEvent(m) {
  const el = document.createElement("div");
  el.className = "proc event";
  const short = m.text.length <= 160 && !m.text.includes("\n");
  const first = (m.text.split("\n").find((l) => l.trim()) || "").slice(0, 120);
  const target = (m.source || "").includes("coordinator") ? "打开主任务 →" : "打开子任务 →";
  el.innerHTML =
    '<input type="checkbox" class="selbox"><div class="pt"><div class="evh"><b>' +
    esc(eventLabel(m)) +
    "</b><span>" +
    esc(short ? m.text : first) +
    "</span>" +
    (m.link ? '<button class="mbtn" data-open>' + target + "</button>" : "") +
    "</div>" +
    (short
      ? ""
      : "<details><summary>展开全文 · " +
        m.text.length +
        ' 字</summary><div class="md">' +
        renderMarkdown(m.text) +
        "</div></details>") +
    "</div>";
  if (m.link) el.querySelector("[data-open]").onclick = safe(() => openById(state.current.tool, m.link));
  return withSel(el, m);
}
function renderMsg(m) {
  if (m.kind === "event") return renderEvent(m);
  if (["tool_call", "tool_result"].includes(m.kind)) {
    const el = document.createElement("div");
    el.className = "proc";
    const raw =
      m.kind === "tool_call" ? m.tool_args : m.kind === "tool_result" ? m.tool_output : m.text;
    const label =
      m.kind === "event"
        ? "协作 · " + (m.source || "系统")
        : (m.kind === "tool_call" ? "调用 · " : "结果 · ") + (m.tool_name || m.call_id || "工具");
    el.innerHTML =
      '<input type="checkbox" class="selbox"><details><summary>' +
      esc(label) +
      " · " +
      raw.length +
      ' 字</summary><button class="mbtn" data-copy-process>复制全文</button><pre>' +
      esc(raw) +
      "</pre></details>";
    el.querySelector("button").onclick = () => copyText(raw);
    return withSel(el, m);
  }

  if (m.kind === "thinking") {
    const el = document.createElement("div");
    el.className = "think";
    el.innerHTML = `<input type="checkbox" class="selbox"><details>
      <summary>思考 · ${m.text.length} 字${m.ts ? " · " + esc(m.ts.slice(11)) : ""}</summary>
      <div class="tb">${esc(m.text)}</div></details>`;
    return withSel(el, m);
  }
  const el = document.createElement("div");
  const user = m.role === "user";
  el.className = "msg " + (user ? "user" : "asst" + (m.final ? "" : " interim"));
  el._raw = m.text;
  el.innerHTML = `
    <div class="hd"><input type="checkbox" class="selbox" title="选择（Shift 连选）">
      <span class="who">${user ? "用户" : m.final ? "助手" : "助手 · 过程"}</span>
      <span class="ts">${esc(m.ts)}</span>
      <span class="acts">
        <button class="mbtn" data-act="raw" title="切换 Markdown 渲染 / 原始文本">源码</button>
        <button class="mbtn" data-act="copy" title="复制这条消息的原文">复制</button>
      </span></div>
    <div class="bd md">${renderMarkdown(m.text)}</div>
    <button class="btn ghost foldbtn" data-act="fold">展开全文</button>`;
  return withSel(el, m);
}
function withSel(el, m) {
  el.querySelector(".selbox").setAttribute("aria-label", "选择第 " + (m.i + 1) + " 条记录");
  el._m = m;
  el.dataset.i = m.i;
  el.dataset.mid = m.id;
  if (state.sel.has(m.id)) {
    el.classList.add("sel");
    el.querySelector(".selbox").checked = true;
  }
  return el;
}
/* 高度超过阈值的消息默认折叠（需在插入 DOM 后测量） */
function applyFold(el) {
  const tall = el.querySelector(".bd").scrollHeight > 680;
  el.classList.toggle("foldable", tall);
  el.classList.toggle("folded", tall);
  el.querySelector(".foldbtn").textContent = "展开全文";
}
/* 宽度变化（窗口缩放、面板从隐藏到显示）后重新判断折叠；用户手动展开的保持展开 */
let foldTimer;
new ResizeObserver(() => {
  clearTimeout(foldTimer);
  foldTimer = setTimeout(
    () =>
      $("#msgs")
        .querySelectorAll(".msg")
        .forEach((el) => {
          if (!el._expanded) applyFold(el);
        }),
    150,
  );
}).observe($("#msgs"));
/* 消息区按钮统一用事件委托 */
$("#meta").addEventListener("click", (e) => {
  const t = e.target.closest("[data-meta]");
  if (!t || !state.current) return;
  if (t.dataset.meta === "id") copyText(state.current.session_id, "已复制会话 ID");
  else {
    state.cwd = state.current.cwd;
    state.tool = "";
    renderDirFilter();
    switchView("sessions");
    loadStats();
    toast("已按此目录过滤会话列表");
  }
});

/* ---------- 多选：按稳定消息 ID 记录，Shift 连选 ---------- */
function clearSel() {
  state.sel.clear();
  state.lastSel = null;
  document.querySelectorAll("#msgs .sel").forEach((el) => {
    el.classList.remove("sel");
    el.querySelector(".selbox").checked = false;
  });
  updateSelBar();
}
function setSel(el, on) {
  const i = el._m.id;
  if (on) state.sel.set(i, el._m);
  else state.sel.delete(i);
  el.classList.toggle("sel", on);
  el.querySelector(".selbox").checked = on;
}
function updateSelBar() {
  $("#selN").textContent = state.sel.size;
  $("#msgcard").classList.toggle("has-sel", state.sel.size > 0);
}
function onSelClick(box, shift) {
  const el = box.closest("[data-i]");
  const items = [...$("#msgs").querySelectorAll("[data-i]")];
  const cur = items.indexOf(el),
    last = items.indexOf(state.lastSel);
  if (shift && last >= 0) {
    // 连选：把上次点击到本次之间的条目都设成本次的状态
    const [a, b] = cur < last ? [cur, last] : [last, cur];
    items.slice(a, b + 1).forEach((x) => setSel(x, box.checked));
  } else setSel(el, box.checked);
  state.lastSel = el;
  updateSelBar();
}
/* 选中内容的纯文本（Markdown）：与交接文档中的消息格式一致 */
function selText() {
  return [...state.sel.values()]
    .sort((a, b) => a.i - b.i)
    .map((m) => {
      if (m.kind === "tool_call") return `> 工具调用 \`${m.tool_name}\` ${m.tool_args}`;
      if (m.kind === "tool_result")
        return `> 工具结果 ${m.tool_name ? m.tool_name + " → " : ""}${m.tool_output}`;
      if (m.kind === "thinking")
        return `<details><summary>思考</summary>\n\n${m.text.trim()}\n\n</details>`;
      return `### ${m.role === "user" ? "用户" : "助手"}${m.ts ? " · " + m.ts : ""}\n\n${m.text.trim()}`;
    })
    .join("\n\n");
}
$("#selAll").onclick = () => {
  $("#msgs")
    .querySelectorAll("[data-i]")
    .forEach((el) => setSel(el, true));
  updateSelBar();
};
$("#selClear").onclick = clearSel;
$("#selCopy").onclick = () => copyText(selText(), `已复制 ${state.sel.size} 条`);
$("#selExport").onclick = async () => {
  const s = state.current;
  if (!s || !state.sel.size) return;
  try {
    showExport(
      await api(`/api/excerpt/${sessPath(s)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message_ids: [...state.sel.keys()] }),
      }),
    );
  } catch (e) {
    toast("导出失败：" + e.message);
  }
};

$("#msgs").addEventListener("change", (e) => {
  if (e.target.classList.contains("selbox")) onSelClick(e.target, e.target._shift);
});
$("#msgs").addEventListener("click", (e) => {
  if (e.target.classList.contains("selbox")) {
    e.target._shift = e.shiftKey;
    return;
  }
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const el = b.closest(".msg");
  const act = b.dataset.act;
  if (act === "copy") copyText(el._raw, "已复制这条消息");
  else if (act === "copycode")
    copyText(b.closest(".codeblk").querySelector("pre").innerText, "已复制代码");
  else if (act === "raw") {
    const bd = el.querySelector(".bd"),
      raw = !bd.classList.contains("raw");
    bd.classList.toggle("raw", raw);
    bd.classList.toggle("md", !raw);
    if (raw) bd.textContent = el._raw;
    else bd.innerHTML = renderMarkdown(el._raw);
    b.classList.toggle("on", raw);
    applyFold(el);
  } else if (act === "fold") {
    const folded = el.classList.toggle("folded");
    el._expanded = !folded;
    b.textContent = folded ? "展开全文" : "收起";
    if (folded) el.scrollIntoView({ block: "nearest" });
  }
});
/* 顶部 / 底部跳转 */
$("#jTop").onclick = () => $("#msgs").scrollTo({ top: 0, behavior: "smooth" });

/* ---------- 导出 ---------- */
$("#btnExport").onclick = async () => {
  const s = state.current;
  if (!s) return;
  const btn = $("#btnExport");
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    // 导出跟随当前视图：对话 → 仅最终输出；只看用户 / 只看 AI / 全程 → 同名模式
    const mode = state.msgMode === "chat" ? "final" : state.msgMode;
    showExport(await api(`/api/export/${sessPath(s)}?mode=${mode}`));
  } catch (e) {
    toast("导出失败：" + e.message);
  }
  btn.disabled = false;
  btn.textContent = "导出交接文档";
};
function showExport(d) {
  window._lastExport = d;
  $("#mdName").textContent = d.filename;
  $("#mdMode").textContent =
    { full: "完整过程", final: "仅最终输出", user: "只看用户", ai: "只看 AI", multi: `合并 ${d.count} 个会话` }[d.mode] ||
    `节选 ${d.count} 条`;
  $("#mdpre").textContent = d.markdown;
  if ($("#box").classList.contains("preview")) $("#mdview").innerHTML = renderMarkdown(d.markdown);
  showDialog($("#modal"));
}
$("#mdseg")
  .querySelectorAll(".s")
  .forEach(
    (b) =>
      (b.onclick = () => {
        const pv = b.dataset.md === "preview";
        $("#mdseg")
          .querySelectorAll(".s")
          .forEach((x) => x.classList.toggle("on", x === b));
        $("#box").classList.toggle("preview", pv);
        if (pv && window._lastExport)
          $("#mdview").innerHTML = renderMarkdown(window._lastExport.markdown);
      }),
  );
$("#closeMd").onclick = () => closeDlg($("#modal"));
$("#modal").onclick = (e) => {
  if (e.target.id === "modal") closeDlg($("#modal"));
};
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const dlg =
    document.querySelector('.show[aria-modal="true"]') || document.querySelector(".dlg.show");
  if (dlg) closeDlg(dlg);
  else if ($("#modal").classList.contains("show")) closeDlg($("#modal"));
  else if ($("#msgcard").classList.contains("finding")) closeFind();
  else if (state.sel.size) clearSel();
});
/* 「/」聚焦左栏搜索（正在输入时不拦截） */
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.target.closest?.("input, textarea, select")) return;
  e.preventDefault();
  document.body.classList.remove("sidebar-off");
  $("#search").focus();
});
$("#copyMd").onclick = () => {
  const d = window._lastExport;
  if (!d) return;
  copyText(d.markdown, "已复制全文 — 粘贴给任意 agent 即可交接");
};
$("#dlMd").onclick = () => {
  const d = window._lastExport;
  if (!d) return;
  saveText(
    d.filename,
    d.markdown,
    d.mode === "excerpt" || d.mode === "multi" ? "已下载" : "已下载，~/.agenthub/exports/ 同存一份",
  );
};

/* ---------- 左侧会话多选（可跨工具）---------- */
function updateLSel() {
  const n = state.sessSel.size,
    by = {};
  for (const s of state.sessSel.values()) by[s.tool] = (by[s.tool] || 0) + 1;
  $("#lselN").textContent = n;
  $("#lselTools").textContent = Object.entries(by)
    .map(([t, k]) => `${TOOLNAMES[t] || t} ${k}`)
    .join(" · ");
  $("#lsel").classList.toggle("show", n > 0);
  document.body.classList.toggle("picking", n > 0); // 多选中：所有会话勾选框常显
}
const sessItems = () =>
  [...state.sessSel.values()].map((s) => ({ tool: s.tool, id: s.session_id }));
$("#lselClear").onclick = () => {
  state.sessSel.clear();
  document.querySelectorAll("#list .row.pick").forEach((r) => {
    r.classList.remove("pick");
    r.querySelector(".rsel").checked = false;
  });
  updateLSel();
};
$("#lselExport").onclick = async () => {
  try {
    showExport(
      await api("/api/export-multi", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: sessItems() }),
      }),
    );
  } catch (e) {
    toast("导出失败：" + e.message);
  }
};
$("#lselSum").onclick = safe(() =>
  openSummary(sessItems(), `${state.sessSel.size} 个会话（${$("#lselTools").textContent}）`));
$("#btnSummary").onclick = safe(() =>
  state.current &&
  openSummary(
    [{ tool: state.current.tool, id: state.current.session_id }],
    `整个会话：${state.current.title}`,
  ));
$("#selSum").onclick = safe(() =>
  state.current &&
  state.sel.size &&
  openSummary(
    [
      {
        tool: state.current.tool,
        id: state.current.session_id,
        message_ids: [...state.sel.keys()],
      },
    ],
    `选中的 ${state.sel.size} 条：${state.current.title}`,
  ));

/* ---------- 通用对话框 ---------- */

const postJSON = (url, body) =>
  api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/* ---------- 设置：模型接口 ---------- */
function cfgForm() {
  // Key 留空表示沿用已保存的
  return {
    base_url: $("#cfgUrl").value.trim(),
    model: $("#cfgModel").value.trim(),
    context_window: $("#cfgContext").value.trim(),
    api_key: $("#cfgClearKey").checked ? "" : $("#cfgKey").value.trim() || null,
  };
}
function cfgMsg(text, cls) {
  const m = $("#cfgMsg");
  m.textContent = text;
  m.className = "msgline " + (cls || "");
}
function fillModels(list) {
  state.models = list;
  $("#modelList").innerHTML = list
    .map(
      (m) =>
        `<option value="${esc(m.id)}">${m.max_context ? Math.round(m.max_context / 1000) + "K 上下文" : ""}</option>`,
    )
    .join("");
  $("#cfgModelNote").textContent = `共 ${list.length} 个可用模型，输入关键字筛选`;
}
$("#btnSettings").onclick = safe(async () => {
  cfgMsg("");
  const c = await api("/api/llm/config");
  $("#cfgUrl").value = c.base_url;
  $("#cfgModel").value = c.model;
  $("#cfgKey").value = "";
  $("#cfgKey").placeholder = c.has_key
    ? `已保存（${c.key_hint}），留空则不修改`
    : "不需要鉴权的服务可留空";
  $("#cfgContext").value = c.context_window || "";
  $("#cfgClearKey").checked = false;
  showDialog($("#setDlg"));
});
$("#cfgFetch").onclick = async () => {
  cfgMsg("正在获取模型列表…");
  try {
    fillModels((await postJSON("/api/llm/models", cfgForm())).models);
    cfgMsg("已获取模型列表", "ok");
  } catch (e) {
    cfgMsg(e.message, "bad");
  }
};
$("#cfgTest").onclick = async () => {
  cfgMsg("正在测试，推理模型可能需要十几秒…");
  try {
    const r = await postJSON("/api/llm/test", cfgForm());
    cfgMsg(`连接成功 · ${r.ms} ms · 回复：${r.reply || "（空）"}`, "ok");
  } catch (e) {
    cfgMsg(e.message, "bad");
  }
};
$("#cfgSave").onclick = async () => {
  try {
    const c = await postJSON("/api/llm/config", cfgForm());
    $("#cfgKey").value = "";
    $("#cfgKey").placeholder = c.has_key
      ? `已保存（${c.key_hint}），留空则不修改`
      : "不需要鉴权的服务可留空";
    $("#cfgClearKey").checked = false;
    state.models = null;
    cfgMsg("已保存", "ok");
    toast("模型设置已保存");
  } catch (e) {
    cfgMsg(e.message, "bad");
  }
};

/* ---------- AI 总结：流式输出 ---------- */
const sum = { items: null, text: "", ctrl: null, raf: 0 };
const stripThink = (t) => t.replace(/<(think|thinking)>[\s\S]*?(<\/\1>|$)/g, "").trim();
function setSumStatus(t) {
  $("#sumStatusText").textContent = t;
}
$("#sumTpl").onchange = () =>
  $("#sumPane").classList.toggle("custom", $("#sumTpl").value === "custom");
$("#sumCopy").onclick = () => {
  const t = stripThink(sum.text);
  if (t) copyText(t, "已复制总结");
};
$("#sumDl").onclick = () => {
  const t = stripThink(sum.text);
  if (!t) return;
  saveText(
    `AI总结__${$("#sumTitle")
      .textContent.replace(/^AI 总结 · /, "")
      .slice(0, 40)
      .replace(/[\\/:*?"<>|\s]+/g, "_")}.md`,
    t,
  );
};

/* ---------- 搜索 / 重扫 ---------- */
let searchTimer;
$("#search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    state.view = "sessions";
    active(".vt", "view", state.view);
    safe(loadList)(true);
  }, 250);
};
$("#rescan").onclick = async () => {
  const btn = $("#rescan");
  btn.disabled = true;
  toast("正在扫描各工具的本地会话…");
  try {
    const d = await api("/api/scan", { method: "POST" });
    toast(`重建完成，共 ${d.total} 个会话`);
    await loadStats();
    if (state.view === "cwd") loadCwd();
    else loadList(true);
  } catch (e) {
    toast("扫描失败：" + e.message);
  }
  btn.disabled = false;
};

/* ---------- 工作区导航与总结 ---------- */
Object.assign(state, {
  cwd: null,
  group: "roots",
  expanded: new Set(),
  positions: new Map(),
  query: "",
  matches: [],
  matchIndex: 0,
});
const keyOf = (s) => s.tool + "/" + s.session_id;

function action(el, fn) {
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.onclick = safe(fn);
  el.onkeydown = (e) => {
    if (e.target === el && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      el.click();
    }
  };
}
function active(selector, attr, value) {
  document.querySelectorAll(selector).forEach((el) => {
    const on = el.dataset[attr] === value;
    el.classList.toggle("on", on);
    el.setAttribute("aria-pressed", String(on));
  });
}
function rowNode(s) {
  const wrap = document.createElement("div"),
    el = document.createElement("div"),
    key = keyOf(s);
  el.className = "row";
  el.style.setProperty("--tc", TOOLCOLOR[s.tool]); // 勾选框隐藏时，左侧留白显示工具色点
  el.dataset.key = key;
  el._sid = s.session_id;
  el.classList.toggle("on", state.current && key === keyOf(state.current));
  el.classList.toggle("pick", state.sessSel.has(key));
  const name = (s.cwd || "").split(/[\\/]/).filter(Boolean).pop() || "未知目录";
  el.innerHTML =
    '<input type="checkbox" class="selbox rsel" aria-label="选择会话 ' +
    esc(s.title) +
    '"><div class="l1"><span class="ttl" title="' +
    esc(s.title) +
    '">' +
    esc(s.title) +
    '</span><span class="dt">' +
    esc(s.iso || "") +
    '</span></div><div class="l2"><span class="tool" style="color:' +
    TOOLCOLOR[s.tool] +
    '">' +
    esc(TOOLNAMES[s.tool] || s.tool) +
    '</span><span class="dir" title="' +
    esc(s.cwd) +
    '">' +
    esc(name) +
    '</span><span class="n">' +
    s.n_messages +
    " 条</span></div>";
  action(el, () => openSession(s, el));
  const cb = el.querySelector("input");
  cb.checked = state.sessSel.has(key);
  cb.onclick = (e) => e.stopPropagation();
  cb.onkeydown = (e) => e.stopPropagation();
  cb.onchange = () => {
    if (cb.checked) state.sessSel.set(key, s);
    else state.sessSel.delete(key);
    el.classList.toggle("pick", cb.checked);
    updateLSel();
  };
  wrap.append(el);
  if (s.extra?.origin === "subagent" || s.extra?.parent_session_id) {
    const label = document.createElement("div");
    label.className = "treeMeta";
    label.textContent = s.extra.origin === "subagent" ? "子任务" : "派生会话";
    el.append(label);
  }
  if (s.children?.length) {
    const toggle = document.createElement("button"),
      branch = document.createElement("div");
    toggle.className = "treeToggle";
    branch.className = "branch";
    branch.hidden = !(state.expanded.has(key) || state.q);
    const update = () => {
      toggle.textContent = (branch.hidden ? "▸ " : "▾ ") + s.children.length + " 个子任务";
      toggle.setAttribute("aria-expanded", String(!branch.hidden));
    };
    update();
    toggle.onclick = (e) => {
      e.stopPropagation();
      branch.hidden = !branch.hidden;
      if (branch.hidden) state.expanded.delete(key);
      else state.expanded.add(key);
      update();
    };
    el.append(toggle);
    s.children.forEach((c) => branch.append(rowNode(c)));
    wrap.append(branch);
  }
  return wrap;
}
async function loadList(reset = true) {
  if (reset) state.offset = 0;
  const offset = state.offset,
    seq = ++state.listSeq;
  const p = new URLSearchParams({
    tool: state.tool,
    q: state.q,
    offset,
    limit: 100,
    group: state.group,
  });
  if (state.cwd !== null) p.set("cwd", state.cwd);
  const d = await api("/api/sessions?" + p);
  if (seq !== state.listSeq || state.view !== "sessions") return;
  const box = $("#list");
  if (reset) box.innerHTML = "";
  box.querySelectorAll(".loadm").forEach((x) => x.remove());
  if (!d.sessions.length && !offset)
    box.innerHTML = '<div class="empty">没有匹配任务<br>调整筛选或搜索词</div>';
  d.sessions.forEach((s) => box.append(rowNode(s)));
  $("#lhL").textContent = state.group === "roots" ? "根任务 · 可展开子任务" : "全部会话";
  $("#lhR").textContent = offset + d.sessions.length + " / " + d.total;
  if (offset + d.sessions.length < d.total) {
    const b = document.createElement("button");
    b.className = "btn ghost loadm";
    b.textContent = "加载更多任务";
    b.onclick = safe(async () => {
      b.disabled = true;
      state.offset = offset + d.sessions.length;
      try {
        await loadList(false);
      } catch (e) {
        state.offset = offset;
        b.disabled = false;
        throw e;
      }
    });
    box.append(b);
  }
}
async function loadCwd() {
  const seq = ++state.listSeq,
    d = await api("/api/cwd");
  if (seq !== state.listSeq || state.view !== "cwd") return;
  const box = $("#list");
  box.innerHTML = "";
  $("#lhL").textContent = "工作目录";
  $("#lhR").textContent = d.groups.length + " 组";
  for (const g of d.groups) {
    const el = document.createElement("div");
    el.className = "cwdrow";
    el.innerHTML =
      '<div class="l1"><span class="p">' +
      esc(g.cwd.split(/[\\/]/).filter(Boolean).pop() || "未知目录") +
      '</span><span class="n">' +
      g.n_sessions +
      '</span></div><div class="treeMeta">' +
      esc(g.cwd || "未知目录") +
      '</div><div class="l2">' +
      esc(g.tools.join(" · ")) +
      "</div>";
    action(el, async () => {
      state.cwd = g.cwd;
      state.tool = "";
      state.view = "sessions";
      renderDirFilter();
      active(".vt", "view", state.view);
      await Promise.all([loadStats(), loadList()]);
    });
    box.append(el);
  }
}
function renderDirFilter() {
  const df = $("#dirFilter");
  df.style.display = state.cwd === null ? "none" : "flex";
  df.querySelector(".p").textContent = state.cwd || "未知目录";
  df.querySelector("button").onclick = safe(async () => {
    state.cwd = null;
    renderDirFilter();
    await loadList();
  });
}
async function loadStats() {
  const s = await api("/api/stats");
  $("#topstats").textContent = s.total + " 个会话 · " + fmtN(s.total_messages) + " 条消息";
  if (s.problems?.length) {
    // 扫描有问题时索引可能不是最新的：明确提示，点开看原因
    const b = document.createElement("button");
    b.className = "probs";
    b.textContent = `⚠ ${s.problems.length} 个扫描问题`;
    b.onclick = () => {
      $("#probList").innerHTML = s.problems.map((p) => `<div class="chosen">${esc(p)}</div>`).join("");
      showDialog($("#probDlg"));
    };
    $("#topstats").append(" ", b);
  }
  const box = $("#chips");
  box.innerHTML = "";
  const filterTool = safe(async (tool) => {
    state.tool = tool;
    state.view = "sessions";
    active(".vt", "view", state.view);
    await Promise.all([loadStats(), loadList()]);
  });
  // 没有会话的工具不占位置（当前选中的除外）
  const tools = Object.keys(TOOLNAMES).filter((t) => s.by_tool[t]?.n || state.tool === t);
  for (const tool of ["", ...tools]) {
    const b = document.createElement("button");
    b.className = "fchip" + (state.tool === tool ? " on" : "");
    b.innerHTML =
      (tool ? `<span class="dot" style="background:${TOOLCOLOR[tool]}"></span>` : "") +
      esc(tool ? TOOLNAMES[tool] : "全部") +
      ` <b>${tool ? s.by_tool[tool]?.n || 0 : s.total}</b>`;
    b.setAttribute("aria-pressed", String(state.tool === tool));
    b.onclick = () => filterTool(tool);
    box.append(b);
  }
  renderWelcome(s, tools, filterTool);
}
/* 开始页：各工具会话数（点击即筛选）+ 最近的会话 */
async function renderWelcome(s, tools, filterTool) {
  if ($("#empty").style.display === "none") return;
  $("#toolCards").innerHTML = "";
  for (const t of tools) {
    const c = document.createElement("button");
    c.className = "toolcard";
    c.innerHTML = `<span><i style="background:${TOOLCOLOR[t]}"></i>${esc(TOOLNAMES[t])}</span><b>${s.by_tool[t].n}</b>`;
    c.onclick = () => filterTool(t);
    $("#toolCards").append(c);
  }
  const d = await api("/api/sessions?" + new URLSearchParams({ limit: 6, group: "all" }));
  $("#recent").innerHTML = "";
  for (const r of d.sessions) {
    const row = document.createElement("button");
    row.className = "recentRow";
    row.innerHTML =
      `<em style="color:${TOOLCOLOR[r.tool]}">${esc(TOOLNAMES[r.tool] || r.tool)}</em>` +
      `<span class="t">${esc(r.title)}</span><small>${esc(r.iso || "")}</small>`;
    row.onclick = safe(() => openSession(r));
    $("#recent").append(row);
  }
}
function remember() {
  if (state.current)
    state.positions.set(keyOf(state.current), {
      offset: state.msgOffset,
      top: $("#msgs").scrollTop,
      mode: state.msgMode,
      order: state.msgOrder,
    });
}
async function openSession(s, el, anchor = "") {
  remember();
  clearTimeout(findTimer);
  const changed = !state.current || keyOf(state.current) !== keyOf(s);
  const pos = state.positions.get(keyOf(s));
  state.query = "";
  $("#findText").value = "";
  $("#msgcard").classList.remove("finding");
  $("#findToggle").classList.remove("on");
  state.matches = [];
  state.matchIndex = 0;
  document.body.classList.add("reading");
  if (pos) {
    state.msgMode = pos.mode;
    state.msgOrder = pos.order;
  }
  if (anchor) state.msgMode = "full";
  active("[data-mode]", "mode", state.msgMode);
  active("[data-order]", "order", state.msgOrder);

  state.current = s;
  state.msgOffset = 0;
  document.querySelectorAll(".row").forEach((x) => x.classList.remove("on"));
  if (el) el.classList.add("on");
  history.replaceState(null, "", "#" + sessPath(s) + (anchor ? "?message=" + encodeURIComponent(anchor) : "")); // 刷新 / 分享链接可直接回到该会话
  $("#empty").style.display = "none";
  $("#headcard").style.display = "block";
  $("#msgcard").style.display = "flex";
  $("#jump").style.display = "flex";
  $("#hTitle").innerHTML =
    `<span class="tooltag" style="background:${TOOLCOLOR[s.tool]}">${TOOLNAMES[s.tool] || esc(s.tool)}</span>${esc(s.title)}`;
  $("#hTitle").title = s.title;
  if (changed) clearSel();
  // 元信息压成一行；没有值的项不显示
  const same = (s.iso2 || "").slice(0, 10) === (s.iso || "").slice(0, 10);
  const span =
    s.iso2 && s.iso && s.iso2 !== s.iso
      ? `${s.iso2} → ${same ? s.iso.slice(11) : s.iso.slice(5)}`
      : s.iso || s.iso2;
  const counts = [
    s.n_messages && `${s.n_messages} 条消息`,
    s.n_tool_calls && `${s.n_tool_calls} 次调用`,
  ]
    .filter(Boolean)
    .join(" · ");
  const model = s.extra && s.extra.model;
  $("#meta").innerHTML = [
    s.cwd &&
      `<span class="mi" style="flex:0 1 auto"><span class="k">目录</span>
      <span class="mono link" data-meta="cwd" title="点击：只看这个目录的会话\n${esc(s.cwd)}">${esc(s.cwd)}</span></span>`,
    span && `<span class="mi mono">${esc(span)}</span>`,
    counts && `<span class="mi">${counts}</span>`,
    model && `<span class="mi mono">${esc(model)}</span>`,
    `<span class="mi"><span class="mono link" data-meta="id" title="点击复制完整会话 ID\n${esc(s.session_id)}">ID ${esc(s.session_id.replace(/^session-/, "").slice(0, 8))}</span></span>`,
    s.extra &&
      s.extra.trace_rebuild &&
      `<span class="badge" title="WorkBuddy 的消息正文在云端，这里由本地 traces 重建；按原生会话 ID 归属；旧版无 ID 的 traces 按工作目录和创建时间归属">trace 重建</span>`,
  ]
    .filter(Boolean)
    .join("");

  if (pos && !anchor) state.msgOffset = pos.offset;
  await loadMessages(true, anchor);
  if (state.current !== s) return;
  if (pos && !anchor) $("#msgs").scrollTop = pos.top;
  if (s.extra?.parent_session_id) {
    const b = document.createElement("button");
    b.className = "mbtn";
    b.textContent = s.extra.origin === "subagent" ? "↑ 父任务" : "↑ 派生自";
    b.onclick = safe(() => openById(s.tool, s.extra.parent_session_id));
    $("#meta").append(b);
  }
  if (state.children.length) {
    const b = document.createElement("button");
    b.className = "mbtn";
    b.textContent = `子任务 ${state.children.length} ▾`;
    b.onclick = showChildren;
    $("#meta").append(b);
  }
}
async function loadMessages(reset = true, anchor = "") {
  const s = state.current;
  if (!s) return;
  const seq = ++state.msgSeq;
  busy(true, "读取对话…");
  const p = new URLSearchParams({
    offset: state.msgOffset,
    order: state.msgOrder,
    view: state.msgMode,
    q: state.query,
    anchor,
  });
  try {
    const d = await api("/api/session/" + sessPath(s) + "?" + p);
    if (seq !== state.msgSeq) return;
    state.msgTotal = d.total;
    state.msgOffset = d.offset;
    state.children = d.session.children || [];
    state.matches = d.matches || [];
    const box = $("#msgs");
    if (reset) box.innerHTML = "";
    box.querySelectorAll(".loadm").forEach((x) => x.remove());
    for (const m of d.messages) {
      const el = renderMsg(m);
      box.append(el);
      if (el.classList.contains("msg")) applyFold(el);
    }
    updateCount();
    $("#findCount").textContent = state.query ? state.matches.length + " 处匹配" : "";
    const next = d.offset + d.messages.length;
    if (next < d.total) {
      const b = document.createElement("button");
      b.className = "btn ghost loadm";
      b.textContent = "加载后续 · " + next + "/" + d.total;
      b.onclick = safe(async () => {
        b.disabled = true;
        const old = state.msgOffset;
        state.msgOffset = next;
        try {
          await loadMessages(false);
        } catch (e) {
          state.msgOffset = old;
          b.disabled = false;
          throw e;
        }
      });
      box.append(b);
    }
    if (reset) box.scrollTop = 0;
    const target = anchor || (reset ? state.matches[0] : "");
    if (target) {
      const el = [...box.querySelectorAll("[data-mid]")].find((x) => x.dataset.mid === target);
      if (el) {
        el.classList.add("focus-msg");
        el.scrollIntoView({ block: "center" });
      }
    }
  } catch (e) {
    if (seq === state.msgSeq) {
      if (reset)
        $("#msgs").innerHTML =
          '<div class="empty">' + esc(e.message) + "<br>请刷新索引或重新选择会话</div>";
      throw e;
    }
  } finally {
    if (seq === state.msgSeq) busy(false);
  }
}
function showChildren() {
  const list = $("#childList");
  list.innerHTML = "";
  for (const c of state.children) {
    const row = document.createElement("button");
    row.className = "chosen childRow";
    row.innerHTML =
      "<span>" +
      esc(c.title) +
      "</span><small>" +
      esc([c.origin === "subagent" ? "子代理" : "派生", c.iso, c.n_messages + " 条"].join(" · ")) +
      "</small>";
    row.onclick = safe(async () => {
      closeDlg($("#childDlg"));
      await openById(c.tool, c.session_id);
    });
    list.append(row);
  }
  showDialog($("#childDlg"));
}
$("#lselManage").onclick = () => {
  const list = $("#selectionList");
  list.innerHTML = "";
  for (const [key, s] of state.sessSel) {
    const row = document.createElement("div");
    row.className = "chosen";
    row.innerHTML =
      "<span>" +
      esc(TOOLNAMES[s.tool]) +
      " · " +
      esc(s.title) +
      '</span><button class="btn ghost sm">移除</button>';
    row.querySelector("button").onclick = () => {
      state.sessSel.delete(key);
      row.remove();
      updateLSel();
      document.querySelectorAll(".row").forEach((el) => {
        if (el.dataset.key === key) {
          el.classList.remove("pick");
          el.querySelector("input").checked = false;
        }
      });
    };
    list.append(row);
  }
  showDialog($("#selectionDlg"));
};
function showDialog(d) {
  d._focus = document.activeElement;
  d.classList.add("show");
  if (d.id !== "sumDlg") {
    d.setAttribute("role", "dialog");
    d.setAttribute("aria-modal", "true");
    d.setAttribute("aria-label", d.querySelector(".t")?.textContent || "导出预览");
  }
  d.querySelector("button,input,select")?.focus();
}
function closeDlg(d) {
  d.classList.remove("show");
  if (d.id === "sumDlg") {
    document.body.classList.remove("summary-open");
    $("#reopenSummary").hidden = false;
  }
  d._focus?.focus();
}
document
  .querySelectorAll("[data-close]")
  .forEach((b) => (b.onclick = () => closeDlg(b.closest(".dlg"))));
document.addEventListener("keydown", (e) => {
  const modal = document.querySelector('.show[aria-modal="true"]');
  if (e.key === "Tab" && modal) {
    const items = [...modal.querySelectorAll('button,input,textarea,select,[tabindex="0"]')].filter(
      (x) => !x.disabled && x.getClientRects().length,
    );
    if (!items.length) return;
    const first = items[0],
      last = items.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
  if ((e.ctrlKey || e.metaKey) && e.key === "k") {
    e.preventDefault();
    $("#search").focus();
  }
});
document.addEventListener(
  "click",
  safe(async (e) => {
    const b = e.target.closest('[data-act="copycode"]');
    if (b && !b.closest("#msgs"))
      await copyText(b.closest(".codeblk").querySelector("pre").textContent);
    const a = e.target.closest('a[href^="#"]');
    if (a && a.closest("#sumOut")) {
      e.preventDefault();
      await openHash(a.getAttribute("href"));
    }
  }),
);
Object.assign(sum, { seq: 0, requestId: null });
function showSummary() {
  showDialog($("#sumDlg"));
  document.body.classList.add("summary-open");
  $("#reopenSummary").hidden = true;
}
async function openSummary(items, desc) {
  if (sum.ctrl) {
    showSummary();
    toast("正在总结，停止后可更换范围");
    return;
  }
  sum.items = items;
  $("#sumTitle").textContent = "AI 总结 · " + desc;
  $("#sumChildren").disabled = items.some((i) => i.message_ids);
  $("#sumChildren").checked = false;
  showSummary();
  const c = await api("/api/llm/config");
  $("#sumModel").value = c.model;
  $("#sumScope").textContent = sum.text
    ? "已有结果仍保留；开始总结将替换。请检查新范围。"
    : "检查范围可查看消息数与分段估算。";
  if (!c.base_url) setSumStatus("请先在设置中配置模型接口");
}
function summaryBody() {
  return {
    items: sum.items,
    include_children: $("#sumChildren").checked,
    template: $("#sumTpl").value,
    prompt: $("#sumPromptText").value,
    llm: $("#sumModel").value.trim() ? { model: $("#sumModel").value.trim() } : {},
  };
}
async function previewSummary() {
  if (!sum.items) throw Error("请先选择会话");
  const d = await postJSON("/api/summary/preview", summaryBody());
  $("#sumScope").textContent =
    d.sessions +
    " 个会话 · " +
    d.messages +
    " 条消息 · " +
    fmtN(d.characters) +
    " 字 · 预计 " +
    d.parts +
    " 段 · " +
    d.model +
    " · " +
    d.budget.context_window +
    " token" +
    (d.budget.estimated ? "（估算窗口，可在设置中调整）" : "") +
    "。内容将发送到设置中的模型接口。";
  return d;
}
async function stopSummary() {
  if (!sum.ctrl) return;
  setSumStatus("正在停止…");
  try {
    await postJSON("/api/summary/cancel", { request_id: sum.requestId });
  } finally {
    sum.ctrl?.abort();
  }
}
function renderSum() {
  sum.raf = 0;
  $("#sumOut").innerHTML = renderMarkdown(stripThink(sum.text));
}
$("#sumRun").onclick = safe(async () => {
  if (sum.ctrl) {
    await stopSummary();
    return;
  }
  const b = $("#sumRun");
  b.disabled = true;
  try {
    await previewSummary();
  } finally {
    b.disabled = false;
  }
  const seq = ++sum.seq,
    ctrl = new AbortController();
  sum.ctrl = ctrl;
  sum.requestId = crypto.randomUUID();
  sum.text = "";
  renderSum();
  $("#sumPane").classList.add("running");
  b.textContent = "停止";
  setSumStatus("准备中…");
  try {
    const r = await fetch("/api/summary", {
      method: "POST",
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...summaryBody(), request_id: sum.requestId }),
    });
    if (!r.ok) throw Error((await r.json()).error || r.status);
    const reader = r.body.getReader(),
      dec = new TextDecoder();
    let buf = "",
      completed = false;
    for (;;) {
      const { value, done } = await reader.read();
      if (seq !== sum.seq) return;
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, k);
        buf = buf.slice(k + 2);
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === "error") throw Error(ev.text);
        if (ev.type === "status") setSumStatus(ev.text);
        if (ev.type === "delta") {
          sum.text += ev.text;
          if (!sum.raf) sum.raf = setTimeout(renderSum, 120);
        }
        if (ev.type === "done") completed = true;
      }
    }
    if (!completed) throw Error("连接断开，结果未完成");
    if (!stripThink(sum.text)) throw Error("模型未返回正文");
    setSumStatus("完成 · " + stripThink(sum.text).length + " 字");
  } catch (e) {
    if (seq === sum.seq)
      setSumStatus(
        e.name === "AbortError"
          ? "已停止接收，保留部分结果；无法确认供应商是否停止计算"
          : "未完成：" + e.message,
      );
  } finally {
    clearTimeout(sum.raf);
    renderSum();
    if (seq === sum.seq) {
      sum.ctrl = null;
      $("#sumPane").classList.remove("running");
      b.textContent = "重新生成";
    }
  }
});
$("#sumPreview").onclick = safe(previewSummary);
$("#reopenSummary").onclick = showSummary;
$("#sumNew").onclick = () => $("#btnSummary").click();
$("#toggleSidebar").onclick = () => {
  if (innerWidth <= 720) document.body.classList.remove("reading");
  else document.body.classList.toggle("sidebar-off");
};
$("#backList").onclick = () => {
  remember();
  document.body.classList.remove("reading");
};
document.querySelectorAll(".vt").forEach((b) =>
  action(b, async () => {
    state.view = b.dataset.view;
    active(".vt", "view", state.view);
    await (state.view === "cwd" ? loadCwd() : loadList());
  }),
);
$("#groupMode").onchange = safe(async (e) => {
  state.group = e.target.value;
  state.view = "sessions";
  active(".vt", "view", state.view);
  await loadList();
});
for (const [attr, key] of [
  ["mode", "msgMode"],
  ["order", "msgOrder"],
])
  document.querySelectorAll("[data-" + attr + "]").forEach((b) =>
    action(b, async () => {
      state[key] = b.dataset[attr];
      active("[data-" + attr + "]", attr, state[key]);
      state.msgOffset = 0;
      await loadMessages();
    }),
  );
document.querySelectorAll("[data-md]").forEach((b) => {
  const fn = b.onclick;
  action(b, fn);
});
let findTimer;
$("#findText").oninput = (e) => {
  clearTimeout(findTimer);
  const text = e.target.value;
  findTimer = setTimeout(
    safe(async () => {
      state.query = text.trim();
      state.matchIndex = 0;
      state.msgOffset = 0;
      await loadMessages();
    }),
    250,
  );
};
async function moveMatch(delta) {
  if (!state.matches.length) return;
  state.matchIndex = (state.matchIndex + delta + state.matches.length) % state.matches.length;
  await loadMessages(true, state.matches[state.matchIndex]);
  $("#findCount").textContent = state.matchIndex + 1 + "/" + state.matches.length;
}
/* 会话内搜索：点开才出现，关闭时清空查询 */
function closeFind() {
  $("#msgcard").classList.remove("finding");
  $("#findToggle").classList.remove("on");
  if ($("#findText").value) {
    $("#findText").value = "";
    $("#findText").dispatchEvent(new Event("input"));
  }
}
$("#findToggle").onclick = () => {
  if ($("#msgcard").classList.contains("finding")) return closeFind();
  $("#msgcard").classList.add("finding");
  $("#findToggle").classList.add("on");
  $("#findText").focus();
};
$("#findClose").onclick = closeFind;
$("#findPrev").onclick = safe(() => moveMatch(-1));
$("#findNext").onclick = safe(() => moveMatch(1));
$("#latest").onclick = safe(async () => {
  state.msgOrder = "desc";
  state.msgOffset = 0;
  state.query = "";
  $("#findText").value = "";
  active("[data-order]", "order", "desc");
  await loadMessages();
});
$("#jBot").onclick = () => $("#latest").click();
async function openHash(hash) {
  const [path, q = ""] = hash.replace(/^#/, "").split("?"),
    [tool, id] = path.split("/").map(decodeURIComponent);
  if (!tool || !id) return;
  const d = await api(
    "/api/session/" + encodeURIComponent(tool) + "/" + encodeURIComponent(id) + "?view=chat",
  );
  await openSession(d.session, null, new URLSearchParams(q).get("message") || "");
}
window.addEventListener(
  "hashchange",
  safe(() => openHash(location.hash)),
);
window.addEventListener("beforeunload", () => {
  if (sum.ctrl)
    fetch("/api/summary/cancel", {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: sum.requestId }),
    });
});
(async () => {
  try {
    await Promise.all([loadStats(), loadList()]);
    await openHash(location.hash);
  } catch (e) {
    $("#list").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>";
  }
})();
