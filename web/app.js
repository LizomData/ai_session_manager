/* 会话管理器 —— 前端
 * 版式参考 cc-switch「会话管理」: 左侧会话列表(按项目 / 按时间分组); 右侧阅读页按「轮」组织:
 * 提问靠右气泡, Agent 一侧把工具调用折叠成「执行过程」摘要, 最终回复按 Markdown 渲染, 右侧对话目录。
 * 「记录」视图保留逐条记录的完整编辑。所有修改都走后端按记录下标的接口, 可撤销 / 重做。 */
"use strict";

// ================= 小工具 =================
const $ = (s, el) => (el || document).querySelector(s);
const enc = encodeURIComponent;
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, props, ...kids) {
  const parts = tag.split(".");
  const el = document.createElement(parts[0] || "div");
  if (parts.length > 1) el.className = parts.slice(1).join(" ");
  if (props) {
    for (const k in props) {
      const v = props[k];
      if (v == null || v === false) continue;
      if (k === "class") el.className += (el.className ? " " : "") + v;
      else if (k === "style") {
        for (const sk in v) { if (sk.startsWith("--")) el.style.setProperty(sk, v[sk]); else el.style[sk] = v[sk]; }
      } else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else if (k === "value" || k === "checked" || k === "disabled" || k === "hidden" || k === "selected") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  return add(el, kids);
}
function add(el, kids) {
  for (const kid of kids) {
    if (kid == null || kid === false) continue;
    if (Array.isArray(kid)) add(el, kid);
    else el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
/** 清空后重新填子节点; 和 h() 一样跳过 null / false、展开数组(replaceChildren 会把 null 写成文字) */
function setKids(el, ...kids) {
  el.replaceChildren();
  return add(el, kids);
}

const ICONS = {
  chevR: '<path d="m9 18 6-6-6-6"/>',
  chevD: '<path d="m6 9 6 6 6-6"/>',
  chevL: '<path d="m15 18-6-6 6-6"/>',
  arrowU: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  arrowD: '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  copyPlus: '<line x1="15" x2="15" y1="12" y2="18"/><line x1="12" x2="18" y1="15" y2="15"/><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  panelR: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18"/>',
  panelL: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  sliders: '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
  redo: '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5A5.5 5.5 0 0 0 4 14.5A5.5 5.5 0 0 0 9.5 20H13"/>',
  pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>',
  fork: '<circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/><path d="M12 12v3"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  save: '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/>',
  share: '<path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" x2="12" y1="2" y2="15"/>',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  ext: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  rotate: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/>',
  list: '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
  hash: '<line x1="4" x2="20" y1="9" y2="9"/><line x1="4" x2="20" y1="15" y2="15"/><line x1="10" x2="8" y1="3" y2="21"/><line x1="16" x2="14" y1="3" y2="21"/>',
};
function icon(name, cls) {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "i" + (cls ? " " + cls : ""));
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = ICONS[name] || "";
  return s;
}
/** 图标按钮: cls 用点分隔(如 "sm" / "xs.hov"), iconCls 为图标尺寸类 */
function ib(name, label, onClick, cls, iconCls) {
  const b = h("button.ib" + (cls ? "." + cls : ""), { type: "button", title: label, "aria-label": label }, icon(name, iconCls));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
function lnk(label, onClick, cls) {
  return h("button.lnk" + (cls ? "." + cls : ""), { type: "button", onclick: onClick }, label);
}

function lsGet(k, d) { try { const v = localStorage.getItem("sm." + k); return v === null ? d : v; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem("sm." + k, v); } catch (e) { /* 存不了就只在本次生效 */ } }

const pad = (n) => String(n).padStart(2, "0");
function fmtRel(ms) {
  if (!ms) return "";
  const diff = Date.now() - ms;
  const m = Math.floor(diff / 60000), hh = Math.floor(diff / 3600000), d = Math.floor(diff / 86400000);
  if (m < 1) return "刚刚";
  if (m < 60) return m + " 分钟前";
  if (hh < 24) return hh + " 小时前";
  if (d < 7) return d + " 天前";
  const dt = new Date(ms);
  return (dt.getFullYear() !== new Date().getFullYear() ? dt.getFullYear() + "年" : "") + (dt.getMonth() + 1) + "月" + dt.getDate() + "日";
}
/** 单条消息的时间: 10月1日 16:29, 不是今年的带上年份 */
function fmtMsgTime(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  const y = d.getFullYear() !== new Date().getFullYear() ? d.getFullYear() + "年" : "";
  return y + (d.getMonth() + 1) + "月" + d.getDate() + "日 " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function fmtClock(ms) { const d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
function fmtDur(ms) {
  if (!ms || ms < 0) return "";
  if (ms < 1000) return Math.round(ms) + "ms";
  const s = ms / 1000;
  if (s < 10) return (Math.round(s * 10) / 10) + "s";
  const t = Math.round(s);
  if (t < 60) return t + "s";
  if (t < 3600) { const m = Math.floor(t / 60), r = t % 60; return r ? m + "m" + r + "s" : m + "m"; }
  const hh = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
  return m ? hh + "h" + m + "m" : hh + "h";
}
function fmtRange(a, b) {
  if (!a && !b) return { range: "", dur: "" };
  if (!a || !b || a === b) return { range: fmtMsgTime(a || b), dur: "" };
  const same = new Date(a).toDateString() === new Date(b).toDateString();
  return { range: fmtMsgTime(a) + " → " + (same ? fmtClock(b) : fmtMsgTime(b)), dur: b > a ? fmtDur(b - a) : "" };
}
const fmtNum = (n) => (n || 0).toLocaleString();
const fmtChars = (n) => (n >= 1000 ? String(Math.round(n / 100) / 10) + "k" : String(n || 0));
function fmtSize(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : n > 1024 ? Math.round(n / 1024) + " KB" : n + " B"; }
const normPath = (p) => (p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
const samePath = (a, b) => !!a && !!b && normPath(a) === normPath(b);
function baseName(p) {
  if (!p) return "";
  const parts = String(p).replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || p;
}
function shortHome(p) {
  return p ? String(p).replace(/^[A-Za-z]:\\Users\\[^\\]+(?=\\|$)/, "~").replace(/^\/(Users|home)\/[^/]+(?=\/|$)/, "~") : "";
}
function oneLine(t, n) {
  const s = String(t || "").replace(/\s+/g, " ").trim();
  return n && s.length > n ? s.slice(0, n) + "…" : s;
}
function debounce(fn, ms) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
/** 搜索词高亮(侧栏用; 正文用 applyFindMarks) */
function hl(text, q) {
  text = text == null ? "" : String(text);
  q = (q || "").trim();
  if (!q) return text;
  const low = text.toLowerCase(), ql = q.toLowerCase();
  let at = low.indexOf(ql);
  if (at < 0) return text;
  const frag = document.createDocumentFragment();
  let pos = 0;
  while (at >= 0) {
    frag.append(text.slice(pos, at), h("mark", null, text.slice(at, at + q.length)));
    pos = at + q.length;
    at = low.indexOf(ql, pos);
  }
  frag.append(text.slice(pos));
  return frag;
}

function toast(msg, err) {
  const t = h("div.toast" + (err ? ".err" : ""), null, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 4600 : 2400);
}
async function copyText(text, msg) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = h("textarea", { style: { position: "fixed", opacity: "0", left: "-9999px" } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
  }
  toast(msg || "已复制");
}
function download(name, text) {
  const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" })), download: name });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}
const safeName = (t) => String(t || "session").replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 60) || "session";

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  let r;
  try { r = await fetch(path, opt); } catch (e) { throw new Error("连不上本地服务，请确认 session_editor.py 还在运行"); }
  let j;
  try { j = await r.json(); } catch (e) { throw new Error("响应解析失败（HTTP " + r.status + "）"); }
  if (!j.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
async function apiText(path) {
  let r;
  try { r = await fetch(path); } catch (e) { throw new Error("连不上本地服务"); }
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.text();
}
/** 包一层: 异步事件里的错误统一 toast */
function guard(fn) {
  return async (...a) => {
    try { return await fn(...a); } catch (e) { console.error(e); toast(e.message || String(e), true); }
  };
}
const isTyping = (t) => !!(t && t.closest && t.closest("input, textarea, select, [contenteditable='true']"));

// ================= Markdown =================
const MD_FENCE = /^( {0,3})(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
const MD_HEAD = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$/;
const MD_HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const MD_LIST = /^( *)([-*+]|\d{1,9}[.)])([ \t]+|$)(.*)$/;
const MD_TSEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function mdInline(src) {
  const keep = [];
  const put = (html) => "\u0000" + (keep.push(html) - 1) + "\u0000";
  let s = String(src);
  s = s.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (_, t, code) => put("<code>" + escHtml(code.replace(/^ ([\s\S]*) $/, "$1")) + "</code>"));
  s = escHtml(s);
  const a = (url, label) => put('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>");
  s = s.replace(/!\[([^\]]*)\]\(\s*((?:https?:\/\/|data:image\/)[^\s)]+)(?:\s+&quot;[^)]*&quot;)?\s*\)/g,
    (_, alt, url) => put('<img alt="' + alt + '" src="' + url + '" loading="lazy">'));
  s = s.replace(/\[([^\]\n]+)\]\(\s*((?:https?:\/\/|mailto:)[^\s)]+)(?:\s+&quot;[^)]*&quot;)?\s*\)/g, (_, txt, url) => a(url, txt));
  // 其余链接多是本地文件路径(src/a.ts#L12): 不跳转, 点击复制路径
  const plink = (url, label) => put('<a class="plink" data-path="' + url + '" title="' + url + '（点击复制路径）">' + label + "</a>");
  s = s.replace(/!\[([^\]]*)\]\(\s*([^\s)]+)(?:\s+&quot;[^)]*&quot;)?\s*\)/g, (_, alt, url) => plink(url, "图片：" + (alt || url)));
  s = s.replace(/\[([^\]\n]+)\]\(\s*([^\s)]+)(?:\s+&quot;[^)]*&quot;)?\s*\)/g, (_, txt, url) => plink(url, txt));
  s = s.replace(/&lt;((?:https?:\/\/|mailto:)[^\s&]+)&gt;/g, (_, url) => a(url, url));
  // 裸网址: 只认网址允许的 ASCII 字符(& 已转义成 &amp;), 末尾标点不算网址
  s = s.replace(/(^|[^\w"=/\u0000])(https?:\/\/(?:[A-Za-z0-9\-._~:/?#[\]@!$*+,;=%]|&amp;)+)/g, (_, pre, url) => {
    const m = /[.,;:!?\]]+$/.exec(url);
    const tail = m ? url.slice(m.index) : "";
    if (m) url = url.slice(0, m.index);
    return pre + a(url, url) + tail;
  });
  s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^\w*\\])\*(?=[^\s*])([^*\n]*?[^\s*\\])\*(?![\w*])/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>");
  s = s.replace(/\n/g, "<br>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, n) => keep[+n]);
}
function codeBlockHtml(code, lang) {
  return '<div class="cb"><div class="cb-head"><span>' + escHtml(lang || "text") +
    '</span><button type="button" data-copy>复制</button></div><pre><code>' + escHtml(code) + "</code></pre></div>";
}
const leadSpaces = (l) => { const m = /^[ \t]*/.exec(l)[0]; return m.replace(/\t/g, "    ").length; };
function startsBlock(l) {
  return MD_FENCE.test(l) || /^ {0,3}#{1,6}(\s|$)/.test(l) || MD_HR.test(l) || /^ {0,3}>/.test(l) || MD_LIST.test(l);
}
function renderMd(src) {
  return mdBlocks(String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n"));
}
function mdBlocks(lines) {
  const out = [];
  const n = lines.length;
  const blank = (l) => !l || !l.trim();
  let i = 0;
  while (i < n) {
    const l = lines[i];
    if (blank(l)) { i++; continue; }
    let m;
    if ((m = MD_FENCE.exec(l))) {
      const ind = m[1].length, fence = m[2], lang = m[3];
      const body = [];
      i++;
      while (i < n) {
        const cm = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(lines[i]);
        if (cm && cm[1][0] === fence[0] && cm[1].length >= fence.length) { i++; break; }
        body.push(ind ? lines[i].replace(new RegExp("^ {0," + ind + "}"), "") : lines[i]);
        i++;
      }
      out.push(codeBlockHtml(body.join("\n"), lang));
      continue;
    }
    if (/^ {0,3}#{1,6}(\s|$)/.test(l) && (m = MD_HEAD.exec(l))) {
      const lv = m[1].length;
      out.push("<h" + lv + ">" + mdInline(m[2] || "") + "</h" + lv + ">");
      i++;
      continue;
    }
    if (MD_HR.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^ {0,3}>/.test(l)) {
      const q = [];
      while (i < n && /^ {0,3}>/.test(lines[i])) { q.push(lines[i].replace(/^ {0,3}> ?/, "")); i++; }
      out.push("<blockquote>" + mdBlocks(q) + "</blockquote>");
      continue;
    }
    if (l.includes("|") && i + 1 < n && MD_TSEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const cells = (r) => {
        let s = r.trim();
        if (s.startsWith("|")) s = s.slice(1);
        if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
        return s.split(/(?<!\\)\|/).map((x) => x.trim().replace(/\\\|/g, "|"));
      };
      const head = cells(l);
      const aligns = cells(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
      i += 2;
      const rows = [];
      while (i < n && !blank(lines[i]) && lines[i].includes("|")) { rows.push(cells(lines[i])); i++; }
      const al = (k) => (aligns[k] ? ' style="text-align:' + aligns[k] + '"' : "");
      out.push("<table><thead><tr>" + head.map((c, k) => "<th" + al(k) + ">" + mdInline(c) + "</th>").join("") + "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + head.map((_, k) => "<td" + al(k) + ">" + mdInline(r[k] || "") + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      continue;
    }
    if (MD_LIST.test(l)) {
      const r = mdList(lines, i);
      out.push(r.html);
      i = r.next;
      continue;
    }
    const para = [l];
    i++;
    while (i < n && !blank(lines[i]) && !startsBlock(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < n && MD_TSEP.test(lines[i + 1]))) {
      para.push(lines[i]);
      i++;
    }
    out.push("<p>" + mdInline(para.map((x) => x.replace(/^[ \t]+/, "")).join("\n")) + "</p>");
  }
  return out.join("");
}
function mdList(lines, start) {
  const n = lines.length;
  const first = MD_LIST.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const startNum = ordered ? parseInt(first[2], 10) : 1;
  const items = [];
  let loose = false;
  let i = start;
  while (i < n) {
    const m = MD_LIST.exec(lines[i]);
    if (!m) break;
    const ind = m[1].length;
    if (ind < base || ind > base + 3 || /\d/.test(m[2]) !== ordered) break;
    const contentInd = ind + m[2].length + Math.max(1, Math.min((m[3] || " ").length, 4));
    const body = [m[4]];
    i++;
    while (i < n) {
      const c = lines[i];
      if (!c.trim()) {
        let k = i + 1;
        while (k < n && !lines[k].trim()) k++;
        if (k < n && leadSpaces(lines[k]) >= contentInd) { for (; i < k; i++) body.push(""); loose = true; continue; }
        break;
      }
      const sp = leadSpaces(c);
      if (sp >= contentInd) { body.push(c.replace(/\t/g, "    ").slice(contentInd)); i++; continue; }
      const lm = MD_LIST.exec(c);
      if (lm && lm[1].length > ind) { body.push(c.slice(Math.min(sp, ind + 2))); i++; continue; }
      if (lm || startsBlock(c)) break;
      body.push(c.trim());   // 段落的懒惰续行
      i++;
    }
    items.push(body);
    if (i < n && !lines[i].trim()) {
      let k = i;
      while (k < n && !lines[k].trim()) k++;
      const nm = k < n ? MD_LIST.exec(lines[k]) : null;
      if (nm && nm[1].length >= base && nm[1].length <= base + 3 && /\d/.test(nm[2]) === ordered) { loose = true; i = k; continue; }
      break;
    }
  }
  const tag = ordered ? "ol" : "ul";
  const lis = items.map((body) => {
    let task = null;
    const tm = /^\[([ xX])\][ \t]+/.exec(body[0]);
    if (tm) { task = tm[1] !== " "; body[0] = body[0].slice(tm[0].length); }
    let html = mdBlocks(body);
    if (!loose) html = html.replace(/<\/?p>/g, "");
    return "<li" + (task !== null ? ' class="task"' : "") + ">" +
      (task !== null ? '<input type="checkbox" disabled' + (task ? " checked" : "") + ">" : "") + html + "</li>";
  });
  return { html: "<" + tag + (ordered && startNum !== 1 ? ' start="' + startNum + '"' : "") + ">" + lis.join("") + "</" + tag + ">", next: i };
}
/** 长内容折叠时的预览: 截到段落边界附近, 补上未闭合的代码围栏 */
function mdPreview(text, n) {
  if (text.length <= n) return text;
  let cut = text.slice(0, n);
  const p = cut.lastIndexOf("\n\n");
  if (p > n * 0.6) cut = cut.slice(0, p);
  const fences = (cut.match(/^ {0,3}(```|~~~)/gm) || []).length;
  return cut.trimEnd() + (fences % 2 ? "\n```" : "") + "\n\n…";
}
document.addEventListener("click", (e) => {
  const t = e.target.closest ? e.target : null;
  const b = t && t.closest("[data-copy]");
  if (b) {
    const code = b.closest(".cb").querySelector("pre code");
    copyText(code ? code.textContent : "", "已复制代码");
    return;
  }
  const p = t && t.closest("a.plink[data-path]");
  if (p) { e.preventDefault(); copyText(p.getAttribute("data-path"), "已复制路径"); }
});

// ================= 菜单 / 对话框 =================
let MENU = null;
function closeMenu() {
  if (!MENU) return;
  const m = MENU;
  MENU = null;
  m.el.remove();
  if (m.onClose) m.onClose();
}
/** items: {label, icon, lead(节点), hint, mark(右侧勾), checked(左侧勾), danger, disabled, onClick} | {sep} | {note} */
function openMenu(anchor, items, opt) {
  opt = opt || {};
  closeMenu();
  const el = h("div.menu", { role: "menu" });
  if (opt.title) el.append(h("div.mtitle", null, opt.title));
  for (const it of items) {
    if (!it) continue;
    if (it.sep) { el.append(h("div.msep")); continue; }
    if (it.note) { el.append(h("div.mnote", null, it.note)); continue; }
    const lead = it.checked !== undefined ? (it.checked ? icon("check", "sm") : null) : it.lead || (it.icon ? icon(it.icon, "sm") : null);
    const b = h("button.mi" + (it.danger ? ".danger" : ""), {
      type: "button", role: it.checked !== undefined ? "menuitemcheckbox" : "menuitem",
      "aria-checked": it.checked !== undefined ? String(!!it.checked) : null, disabled: !!it.disabled, title: it.title || null,
    }, h("span.ic", null, lead), h("span.lbl", null, it.label), it.hint ? h("span.hint.tnum", null, it.hint) : null,
    it.mark !== undefined ? h("span.ic", null, it.mark ? icon("check", "sm") : null) : null);
    b.addEventListener("click", (e) => { e.stopPropagation(); closeMenu(); if (it.onClick) it.onClick(); });
    el.append(b);
  }
  $("#layer").append(el);
  const r = anchor.getBoundingClientRect();
  const mw = el.offsetWidth, mh = el.offsetHeight;
  let left = opt.align === "start" ? r.left : r.right - mw;
  left = Math.max(8, Math.min(left, innerWidth - mw - 8));
  let top = r.bottom + 4;
  if (top + mh > innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  el.style.left = left + "px";
  el.style.top = top + "px";
  MENU = { el, anchor, onClose: opt.onClose };
}
document.addEventListener("mousedown", (e) => {
  if (MENU && !MENU.el.contains(e.target) && !MENU.anchor.contains(e.target)) closeMenu();
}, true);
window.addEventListener("resize", closeMenu);
window.addEventListener("blur", closeMenu);

function dialog({ title, body, actions, wide, focus }) {
  return new Promise((resolve) => {
    const bg = h("div.modal-bg");
    const dlg = h("div.dialog" + (wide ? ".wide" : ""), { role: "dialog", "aria-modal": "true", "aria-label": title });
    const prevFocus = document.activeElement;
    let done = false;
    const close = (v) => {
      if (done) return;
      done = true;
      bg.remove();
      document.removeEventListener("keydown", onKey, true);
      if (prevFocus && prevFocus.focus) prevFocus.focus({ preventScroll: true });
      resolve(v);
    };
    const foot = h("div.dfoot");
    let primary = null;
    for (const a of actions) {
      const b = h("button.btn" + (a.primary ? ".primary" : "") + (a.danger ? ".danger.solid" : ""), { type: "button" }, a.label);
      b.addEventListener("click", async () => {
        if (a.validate && !(await a.validate())) return;
        close(a.value);
      });
      if (a.primary || a.danger) primary = b;
      foot.append(b);
    }
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(null); }
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && primary) { e.preventDefault(); primary.click(); }
    };
    dlg.append(h("div.dh", null, title), h("div.dbody", null, body), foot);
    bg.append(dlg);
    bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(null); });
    document.addEventListener("keydown", onKey, true);
    $("#layer").append(bg);
    const f = focus || dlg.querySelector("input:not([type=radio]):not([type=checkbox]), textarea, select") || primary || foot.lastChild;
    if (f) f.focus();
  });
}
async function confirmDlg(title, msg, okLabel, danger) {
  const v = await dialog({
    title, body: typeof msg === "string" ? h("p", null, msg) : msg,
    actions: [{ label: "取消", value: false }, { label: okLabel || "确定", value: true, primary: !danger, danger: !!danger }],
  });
  return v === true;
}
async function promptDlg(title, label, value, okLabel, hint) {
  const inp = h("input.inp", { value: value || "", autocomplete: "off", spellcheck: "false" });
  const body = h("div", null, h("div.form-row", null, h("span.lb", null, label), h("div.ct", null, inp)), hint ? h("div.form-hint", null, hint) : null);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inp.closest(".dialog").querySelector(".btn.primary").click(); } });
  const ok = await dialog({ title, body, actions: [{ label: "取消", value: false }, { label: okLabel || "确定", value: true, primary: true }], focus: inp });
  return ok ? inp.value : null;
}
function lightbox(src) {
  const bg = h("div.lightbox", { role: "dialog", "aria-label": "图片" }, h("img", { src, alt: "图片" }));
  const close = () => { bg.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  bg.addEventListener("click", close);
  document.addEventListener("keydown", onKey, true);
  $("#layer").append(bg);
}

// ================= 来源 / 版式 =================
const SRC = {
  claude: { name: "Claude Code", glyph: "✻", style: "claude" },
  codex: { name: "Codex", glyph: "◎", style: "codex" },
  cursor: { name: "Cursor", glyph: "◆", style: "cursor" },
  "cursor-chat": { name: "Cursor 会话（store.db）", glyph: "◆", style: "cursor" },
  saved: { name: "本地保存", glyph: "▣", style: "generic" },
  other: { name: "其它", glyph: "•", style: "generic" },
};
const SRC_ORDER = ["claude", "codex", "cursor", "cursor-chat", "saved", "other"];
const STYLE = {
  claude: { accent: "var(--agent-claude)", glyph: "✻", step: "⏺", think: "✻", thinkLabel: "思考中…", call: true, conn: "⎿" },
  codex: { accent: "var(--agent-codex)", glyph: "◎", step: "•", think: "•", thinkLabel: "推理", call: false, conn: "└" },
  cursor: { accent: "var(--agent-cursor)", glyph: "◆", step: "⏺", think: "✻", thinkLabel: "思考中…", call: true, conn: "⎿" },
  generic: { accent: "var(--agent-generic)", glyph: "•", step: "•", think: "•", thinkLabel: "思考", call: true, conn: "└" },
};
const STATUS_TEXT = { success: "成功", error: "失败", interrupted: "已中断", pending: "进行中", unknown: "未知" };
function srcStyle(s) {
  if (!s) return "generic";
  if (s.source === "saved") {
    const o = String(s.from || "");
    return o.startsWith("codex") ? "codex" : o.startsWith("cursor") ? "cursor" : o === "claude" ? "claude" : "generic";
  }
  return (SRC[s.source] || SRC.other).style;
}
function glyphEl(src, style) {
  const meta = SRC[src] || SRC.other;
  const st = STYLE[style || meta.style] || STYLE.generic;
  return h("span.glyph", { style: { "--g": st.accent }, title: meta.name, "aria-hidden": "true" }, meta.glyph);
}

// ================= 状态 =================
const S = {
  sessions: [], listLoading: false, listLoaded: false,
  src: lsGet("src", "all"), q: "", group: lsGet("group", "project"),
  open: new Set((() => { try { return JSON.parse(lsGet("openGroups", "[]")); } catch (e) { return []; } })()),
  sideOn: lsGet("side", "1") === "1",
  path: "", m: {}, cur: null, ol: null,
  view: lsGet("view", "all"),
  expandAll: lsGet("expandAll", "0") === "1", showInj: false,
  outlineOn: lsGet("outline", "1") === "1",
  pages: [], page: 0, turns: [], olCur: null,
  turnOv: new Map(), stepOv: new Map(), details: new Map(),
  textOpen: new Set(), fullText: new Map(), evOpen: new Set(), injOpen: new Map(),
  outAll: new Set(), fullOut: new Map(), thinkFull: new Map(), paramsAll: new Map(),
  find: { open: false, q: "", hits: [], total: 0, pos: 0, busy: false, trunc: false, pend: null, ranQ: "" },
  recs: { page: 0, size: parseInt(lsGet("recSize", "50"), 10) || 50, total: 0, items: [], filter: new Set() },
  editing: 0,
};
const curStyle = () => STYLE[S.m.style] || STYLE.generic;
const agentName = () => S.m.agent || "助手";
function resetReaderState() {
  for (const k of ["turnOv", "stepOv", "details", "fullText", "injOpen", "fullOut", "thinkFull", "paramsAll"]) S[k].clear();
  for (const k of ["textOpen", "evOpen", "outAll"]) S[k].clear();
  S.find = { open: false, q: "", hits: [], total: 0, pos: 0, busy: false, trunc: false, pend: null, ranQ: "" };
  S.recs.page = 0;
  S.recs.filter.clear();
  S.olCur = null;
  S.editing = 0;
  S.showInj = false;
}

// ================= 侧栏: 会话列表 =================
const BUCKET = { today: "今天", yesterday: "昨天", week: "本周", earlier: "更早" };
function timeBucket(ms) {
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  const today = d0.getTime();
  if (!ms) return "earlier";
  if (ms >= today) return "today";
  if (ms >= today - 86400000) return "yesterday";
  const weekStart = today - ((new Date(today).getDay() + 6) % 7) * 86400000;
  return ms >= weekStart ? "week" : "earlier";
}
function sessTitle(s) { return s.title || baseName(s.cwd || s.from_cwd) || String(s.sessionId || s.name || "").slice(0, 8); }
function projectKey(s) {
  const c = s.cwd || s.from_cwd;
  if (c) return "p:" + normPath(c);
  if (s.project) return "c:" + s.project;
  return "u:";
}
function filteredSessions() {
  const q = S.q.trim().toLowerCase();
  return S.sessions.filter((s) => {
    if (S.src !== "all" && s.source !== S.src) return false;
    if (!q) return true;
    return [s.title, s.summary, s.cwd, s.from_cwd, s.dir, s.name, s.sessionId, s.project]
      .some((v) => v && String(v).toLowerCase().includes(q));
  });
}
function groupByProject(items) {
  const map = new Map();
  for (const s of items) {
    const key = projectKey(s);
    let g = map.get(key);
    if (!g) {
      const dir = s.cwd || s.from_cwd || "";
      g = { key, dir, label: dir ? baseName(dir) : s.project || "未知目录", sessions: [], latest: 0 };
      map.set(key, g);
    }
    g.sessions.push(s);
    g.latest = Math.max(g.latest, s.last || 0);
  }
  return [...map.values()].sort((a, b) => (a.key === "u:") - (b.key === "u:") || b.latest - a.latest);
}
function orderedSessions() {
  const items = filteredSessions();
  return S.group === "time" ? items : groupByProject(items).flatMap((g) => g.sessions);
}
function resumeCmd(s) {
  if (!s.resume) return "";
  return s.cwd ? 'cd "' + s.cwd + '"; ' + s.resume : s.resume;
}

async function loadSessions(silent) {
  S.listLoading = true;
  if (!S.listLoaded) renderSideList();
  try {
    const j = await api("/api/list");
    S.sessions = j.sessions || [];
    S.listLoaded = true;
    if (S.path) S.cur = S.sessions.find((s) => samePath(s.path, S.path)) || S.cur;
    if (!silent) toast("已刷新会话列表（" + S.sessions.length + " 个）");
  } catch (e) {
    toast("读取会话列表失败：" + e.message, true);
  } finally {
    S.listLoading = false;
    renderSideTools();
    renderSideList();
    if (S.path && S.ol) renderHead();
  }
}

function themeIcon() { const t = lsGet("theme", "auto"); return t === "light" ? "sun" : t === "dark" ? "moon" : "monitor"; }
function applyTheme(t) { if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
function cycleTheme() {
  const order = ["auto", "light", "dark"];
  const next = order[(order.indexOf(lsGet("theme", "auto")) + 1) % 3];
  lsSet("theme", next);
  applyTheme(next);
  toast("主题：" + { auto: "跟随系统", light: "浅色", dark: "深色" }[next]);
  renderSideHead();
}
function toggleSide(on) {
  const want = on === undefined ? !S.sideOn : on;
  S.sideOn = want;
  $("#app").classList.toggle("side-off", !want);
  if (!narrow()) lsSet("side", want ? "1" : "0");
  if (S.path) renderHead();
}
const narrow = () => window.matchMedia("(max-width: 900px)").matches;

function renderSideHead() {
  $("#brand").replaceChildren(icon("history"), "会话", h("span.count.tnum", null, S.sessions.length ? String(S.sessions.length) : ""));
  const t = lsGet("theme", "auto");
  $("#sideActions").replaceChildren(
    ib("file", "按路径打开会话文件…", guard(openByPath), "sm"),
    ib("refresh", "刷新列表", () => loadSessions(false), "sm"),
    ib(themeIcon(), "主题：" + { auto: "跟随系统", light: "浅色", dark: "深色" }[t] + "（点击切换）", cycleTheme, "sm"),
    ib("panelL", "收起侧栏", () => toggleSide(false), "sm"),
  );
}
function segEl(items, value, onChange, label) {
  return h("div.seg", { role: "group", "aria-label": label },
    items.map(([v, text]) => h("button" + (v === value ? ".on" : ""), { type: "button", "aria-pressed": String(v === value), onclick: () => onChange(v) }, text)));
}
function renderSideTools() {
  const counts = {};
  for (const s of S.sessions) counts[s.source] = (counts[s.source] || 0) + 1;
  if (S.src !== "all" && !counts[S.src] && S.listLoaded) S.src = "all";
  const label = S.src === "all" ? "全部来源" : (SRC[S.src] || SRC.other).name;
  const btn = h("button.src-btn", { type: "button", "aria-haspopup": "menu", title: "按来源筛选" },
    S.src === "all" ? icon("grid", "sm muted") : glyphEl(S.src), h("span.trunc", null, label),
    h("span.n.tnum", null, String(S.src === "all" ? S.sessions.length : counts[S.src] || 0)), h("span.spacer"), icon("chevD", "sm muted"));
  btn.onclick = () => openMenu(btn, [
    { label: "全部来源", icon: "grid", hint: String(S.sessions.length), mark: S.src === "all", onClick: () => setSrc("all") },
    { sep: true },
    ...SRC_ORDER.filter((k) => counts[k] || k === S.src).map((k) => ({
      label: SRC[k].name, lead: glyphEl(k), hint: String(counts[k] || 0), mark: S.src === k, onClick: () => setSrc(k),
    })),
  ], { align: "start" });
  $("#sideTools").replaceChildren(btn, segEl([["project", "按项目"], ["time", "按时间"]], S.group, (v) => {
    S.group = v;
    lsSet("group", v);
    renderSideTools();
    renderSideList();
  }, "分组方式"));
}
function setSrc(k) { S.src = k; lsSet("src", k); renderSideTools(); renderSideList(); }
async function openByPath() {
  const v = await promptDlg("按路径打开", "文件路径", "", "打开",
    "支持 Claude / Cursor 的 .jsonl、Cursor 的 store.db、Codex 的 rollout-*.jsonl，以及子代理 transcript。");
  const p = (v || "").trim().replace(/^"|"$/g, "");
  if (p) await openSession(p, { force: true });
}
function initSideSearch() {
  const inp = h("input", { id: "sideQ", placeholder: "搜索标题、目录、消息或会话 ID", autocomplete: "off", spellcheck: "false", "aria-label": "搜索会话" });
  const clr = ib("x", "清空搜索", () => { inp.value = ""; S.q = ""; clr.hidden = true; renderSideList(); inp.focus(); }, "xxs", "sm");
  clr.hidden = true;
  const rerender = debounce(renderSideList, 120);
  inp.addEventListener("input", () => { S.q = inp.value; clr.hidden = !inp.value; rerender(); });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && inp.value) { e.stopPropagation(); clr.click(); }
    if (e.key === "Enter") { const first = orderedSessions()[0]; if (first) openSession(first.path); }
  });
  $("#sideSearch").replaceChildren(h("label.field", null, icon("search", "sm faint"), inp, clr));
}
function renderSideList() {
  const list = $("#sideList");
  $("#brand .count") && ($("#brand .count").textContent = S.sessions.length ? String(S.sessions.length) : "");
  if (!S.listLoaded) { list.replaceChildren(h("div.side-empty", null, "正在扫描本机会话…")); return; }
  const items = filteredSessions();
  if (!items.length) {
    list.replaceChildren(h("div.side-empty", null,
      S.q.trim() ? "没有标题、目录或消息匹配「" + S.q.trim() + "」的会话" : "这里还没有会话",
      h("div", null, S.q.trim() ? h("button.btn.sm", { type: "button", onclick: () => { $("#sideQ").value = ""; S.q = ""; renderSideList(); } }, "清除搜索")
        : h("button.btn.sm", { type: "button", onclick: () => loadSessions(false) }, "刷新"))));
    return;
  }
  const frag = document.createDocumentFragment();
  const searching = !!S.q.trim();
  if (S.group === "time") {
    let last = null;
    for (const s of items) {
      const b = timeBucket(s.last);
      if (b !== last) { frag.append(h("div.tbucket", null, BUCKET[b])); last = b; }
      frag.append(sessRow(s, true));
    }
  } else {
    for (const g of groupByProject(items)) {
      const hasCur = g.sessions.some((s) => samePath(s.path, S.path));
      const open = searching || S.open.has(g.key) || hasCur;
      frag.append(groupHead(g, open));
      if (open) for (const s of g.sessions) frag.append(sessRow(s, false));
    }
  }
  list.replaceChildren(frag);
}
function groupHead(g, open) {
  const btn = h("button.gbtn", { type: "button", "aria-expanded": String(open), title: g.dir || g.label },
    icon("chevD", "sm chev"), h("span.trunc", null, g.label));
  btn.onclick = () => {
    if (S.open.has(g.key)) S.open.delete(g.key); else S.open.add(g.key);
    lsSet("openGroups", JSON.stringify([...S.open].slice(-300)));
    renderSideList();
  };
  return h("div.ghead" + (open ? "" : ".closed"), null, btn,
    h("span.gmeta.tnum", { title: g.sessions.length + " 个会话 · 最近 " + fmtMsgTime(g.latest) }, g.sessions.length + " · " + fmtRel(g.latest)));
}
function sessRow(s, showDir) {
  const on = samePath(s.path, S.path);
  const title = sessTitle(s);
  const tip = [title, s.summary ? "最后：" + s.summary : "", s.cwd || s.from_cwd || "", (SRC[s.source] || SRC.other).name + " · " + fmtSize(s.size || 0) + " · " + fmtMsgTime(s.last)]
    .filter(Boolean).join("\n");
  const more = ib("more", "更多操作", (e) => {
    e.stopPropagation();
    row.classList.add("menu");
    openMenu(more, [
      { label: "复制会话 ID", icon: "hash", disabled: !s.sessionId, onClick: () => copyText(s.sessionId, "已复制会话 ID") },
      { label: "复制源文件路径", icon: "file", onClick: () => copyText(s.path, "已复制源文件路径") },
      s.resume ? { label: "复制恢复命令", icon: "terminal", hint: s.resume, onClick: () => copyText(resumeCmd(s), "已复制恢复命令") } : null,
    ], { onClose: () => row.classList.remove("menu") });
  }, "xs", "sm");
  const meta = [];
  if (showDir && (s.cwd || s.from_cwd || s.project)) meta.push(icon("folder", "xs"), h("span", { style: { flex: "none" } }, baseName(s.cwd || s.from_cwd) || s.project), s.summary ? h("span.sep", null, "·") : null);
  if (s.summary) meta.push(h("span.trunc", null, "最后：", hl(s.summary, S.q)));
  else if (s.source === "saved" && s.from) meta.push(h("span.trunc", null, "来自 " + ((SRC[s.from] || {}).name || s.from)));
  const row = h("div.srow" + (on ? ".on" : ""), { role: "listitem", tabindex: "0", title: tip, "aria-current": on ? "true" : null },
    S.src === "all" ? glyphEl(s.source, srcStyle(s)) : null,
    h("div.sbody", null, h("div.stitle", null, h("span.trunc" + (s.title ? "" : ".uid"), null, hl(title, S.q))),
      meta.length ? h("div.smeta", null, meta) : null),
    h("div.sright", null, h("span.stime", null, fmtRel(s.last)), h("div.sacts", null, more)));
  row.addEventListener("click", () => openSession(s.path));
  row.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === row) openSession(s.path); });
  return row;
}

// ================= 打开会话 =================
async function confirmLeave() {
  if (S.editing > 0 && !(await confirmDlg("放弃正在编辑的内容？", "有文本框还没保存修改，离开后这些编辑会丢失。", "放弃编辑", true))) return false;
  if (S.m.dirty && !(await confirmDlg("放弃未保存的修改？", "当前会话有未写回文件的修改。打开其它会话会丢弃这些修改（可以先按 Ctrl+S 保存）。", "放弃修改", true))) return false;
  return true;
}
async function openSession(path, opt) {
  opt = opt || {};
  if (!path) return;
  if (samePath(path, S.path) && !opt.force) { if (narrow()) toggleSide(false); return; }
  if (!opt.skipConfirm && !(await confirmLeave())) return;
  $("#welcome").hidden = true;
  $("#reader").hidden = false;
  S.path = path;
  renderSideList();
  $("#rHead").replaceChildren(h("div.r-title", null, h("h1.faint", null, "正在读取…")));
  $("#rInfo") && ($("#rInfo").textContent = "");
  $("#rCol").replaceChildren(h("div.loading", null, "正在读取会话（大文件可能需要一会儿）…"));
  $("#outline").hidden = true;
  $("#latestBtn").hidden = true;
  try {
    const j = await api("/api/open?path=" + enc(path));
    S.m = j;
    S.path = j.path;
    S.cur = S.sessions.find((s) => samePath(s.path, j.path)) || null;
    resetReaderState();
    $("#app").style.setProperty("--accent", curStyle().accent);
    history.replaceState(null, "", "?file=" + enc(j.path));
    if (j.errors && j.errors.length) toast("有 " + j.errors.length + " 行解析失败（已跳过）", true);
    await loadOutline();
    renderChrome();
    if (S.view === "records") await loadRecPage(0); else await loadTurnPage(0);
    renderSideList();
    const row = $("#sideList .srow.on");
    if (row) row.scrollIntoView({ block: "nearest" });
    if (narrow()) toggleSide(false);
  } catch (e) {
    $("#rCol").replaceChildren(h("div.empty-note", null, h("p", null, "无法读取这个会话"), h("p.faint.mono", null, e.message),
      h("button.btn", { type: "button", onclick: () => openSession(path, { force: true, skipConfirm: true }) }, "重试")));
    $("#rHead").replaceChildren(ib("panelL", "侧栏", () => toggleSide(), "sm"), h("div.r-title", null, h("h1", null, baseName(path))));
  }
}
function applyMeta(j) {
  for (const k of ["path", "total", "dirty", "canUndo", "canRedo", "origin", "cwd", "rev", "style", "agent", "readonly"]) {
    if (k in j) S.m[k] = j[k];
  }
  if (S.ol) { renderHead(); renderBarDyn(); }
}
async function loadOutline() {
  const j = await api("/api/outline");
  S.ol = { turns: j.turns, stats: j.stats };
  applyMeta(j);
  buildPages();
}
function eligible(e, v) {
  if (v === "chat") return e.q !== null || e.fin;
  if (v === "changes") return e.chg > 0;
  return true;
}
function buildPages() {
  const v = S.view === "records" ? "all" : S.view;
  const pages = [];
  let cur = [], w = 0;
  const per = v === "all" ? 12 : 30, budget = v === "all" ? 400 : Infinity;
  for (const e of S.ol ? S.ol.turns : []) {
    if (!eligible(e, v)) continue;
    if (cur.length && (cur.length >= per || w + e.n > budget)) { pages.push(cur); cur = []; w = 0; }
    cur.push(e.t);
    w += e.n;
  }
  if (cur.length) pages.push(cur);
  S.pages = pages;
}
const pageOfTurn = (t) => S.pages.findIndex((p) => p.includes(t));
function turnOfIdx(idx) {
  const ts = S.ol ? S.ol.turns : [];
  let lo = 0, hi = ts.length - 1, ans = 0;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (ts[mid].first <= idx) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ts.length ? ts[ans].t : 0;
}

// ================= 阅读页: 页头 / 信息栏 / 工具 =================
function sessionTitle() { return (S.cur && S.cur.title) || (S.ol && S.ol.stats.title) || baseName(S.path); }
function renderChrome() {
  if (!$("#rInfo")) {
    $("#rBar").replaceChildren(h("div.r-info", { id: "rInfo" }), h("div.r-tools", { id: "rTools" }, h("span.row-gap", { id: "rToolsDyn" }), FIND.box));
  }
  renderHead();
  renderBarDyn();
  renderOutline();
}
function renderHead() {
  const ordered = orderedSessions();
  const k = ordered.findIndex((s) => samePath(s.path, S.path));
  const ro = S.m.readonly;
  const title = sessionTitle();
  const src = S.cur ? S.cur.source : S.m.source || "other";
  const prev = ib("chevL", "上一个会话", () => openSession(ordered[k - 1].path), "sm");
  const next = ib("chevR", "下一个会话", () => openSession(ordered[k + 1].path), "sm");
  prev.disabled = k <= 0;
  next.disabled = k < 0 || k >= ordered.length - 1;
  setKids($("#rHead"),
    S.sideOn && !narrow() ? null : ib("panelL", "展开侧栏", () => toggleSide(true), "sm"),
    h("div.r-title", null, glyphEl(src, S.m.style), h("h1", { title }, title),
      ro ? h("span.badge.warn", { title: ro }, "只读") : null,
      S.m.dirty ? h("span.badge.danger", { title: "有修改尚未写回文件" }, "未保存") : null),
    h("span.row-gap", null, prev, next),
    ro ? h("button.btn.sm", { type: "button", title: "另存为可编辑的 jsonl（工具目录 saved_sessions/）", onclick: guard(saveLocal) }, icon("download", "sm"), "存到本地")
      : h("button.btn.sm" + (S.m.dirty ? ".primary" : ""), { type: "button", disabled: !S.m.dirty, title: "写回原文件（Ctrl+S，写前自动备份）", onclick: guard(save) }, icon("save", "sm"), "保存"),
    h("button.btn.sm", { type: "button", title: "导出到 Claude Code / Codex 的会话历史", onclick: guard(openExport) }, icon("share", "sm"), "导出到…"),
    ib("more", "更多操作", (e) => headMenu(e.currentTarget), "sm"),
  );
}
function headMenu(anchor) {
  const sid = (S.ol && S.ol.stats.sessionId) || (S.cur && S.cur.sessionId);
  openMenu(anchor, [
    { label: "存到本地…", icon: "download", onClick: guard(saveLocal) },
    { label: "导出为 Markdown 文件", icon: "file", onClick: guard(() => exportFile("md")) },
    { label: "复制整段为 Markdown", icon: "copy", onClick: guard(async () => copyText(await apiText("/api/export?fmt=md"), "已复制整段对话（Markdown）")) },
    { label: "导出逐条记录（TXT）", icon: "list", onClick: guard(() => exportFile("txt")) },
    { sep: true },
    { label: "复制会话 ID", icon: "hash", disabled: !sid, onClick: () => copyText(sid, "已复制会话 ID") },
    { label: "复制源文件路径", icon: "file", onClick: () => copyText(S.path, "已复制源文件路径") },
    S.cur && S.cur.resume ? { label: "复制恢复命令", icon: "terminal", hint: S.cur.resume, onClick: () => copyText(resumeCmd(S.cur), "已复制恢复命令") } : null,
    { sep: true },
    { label: "备份与回滚…", icon: "rotate", onClick: guard(openBackups) },
    { label: "重新读取", icon: "refresh", onClick: () => openSession(S.path, { force: true }) },
  ]);
}
function pathChip(p) {
  return h("button.pathchip", { type: "button", title: p + "\n点击复制", onclick: () => copyText(p, "已复制工作目录") },
    icon("folder", "xs"), h("span.trunc", null, shortHome(p)));
}
function renderBarDyn() {
  const info = $("#rInfo");
  if (!info) return;
  const st = (S.ol && S.ol.stats) || {};
  const sep = () => h("span.sep", { "aria-hidden": "true" }, "·");
  const r = fmtRange(st.first, st.last);
  setKids(info,
    S.m.cwd ? pathChip(S.m.cwd) : h("span", null, "未知目录"),
    r.range ? [sep(), h("span", null, r.range, r.dur ? h("span.faint", null, "（" + r.dur + "）") : null)] : null,
    sep(), h("span", { title: (st.turns || 0) + " 轮" }, (st.questions || 0) + " 次提问", st.tools ? " · " + fmtNum(st.tools) + " 次工具调用" : ""),
    st.model ? [sep(), h("span.mono.faint.trunc", null, st.model)] : null,
    sep(), h("span.faint", null, fmtNum(S.m.total) + " 条记录"),
  );
  const recView = S.view === "records";
  const outlineBtn = ib("panelR", "对话目录", () => { S.outlineOn = !S.outlineOn; lsSet("outline", S.outlineOn ? "1" : "0"); renderBarDyn(); renderOutline(); }, "sm" + (S.outlineOn ? ".on" : ""));
  outlineBtn.hidden = recView;
  const opts = ib("sliders", "显示选项", (e) => openMenu(e.currentTarget, [
    { label: "展开全部执行过程", checked: S.expandAll, onClick: () => { S.expandAll = !S.expandAll; lsSet("expandAll", S.expandAll ? "1" : "0"); S.turnOv.clear(); renderTurns({ keepScroll: true }); } },
    { label: "显示注入的上下文", checked: S.showInj, onClick: () => { S.showInj = !S.showInj; renderTurns({ keepScroll: true }); } },
    { note: "注入的上下文：系统提示、环境信息、命令输出等不是你亲手输入的内容。" },
  ]), "sm");
  opts.hidden = recView;
  const undo = ib("undo", "撤销（Ctrl+Z）", guard(doUndo), "sm");
  const redo = ib("redo", "重做（Ctrl+Y）", guard(doRedo), "sm");
  undo.disabled = !S.m.canUndo;
  redo.disabled = !S.m.canRedo;
  const findBtn = ib("search", "在会话中查找（Ctrl+F）", openFind, "sm");
  findBtn.hidden = S.find.open;
  $("#rToolsDyn").replaceChildren(outlineBtn,
    segEl([["all", "全部"], ["chat", "对话"], ["changes", "改动"], ["records", "记录"]], S.view, (v) => guard(setView)(v), "视图"),
    opts, undo, redo, findBtn);
  $("#rToolsDyn .seg").style.margin = "0 4px";
}
async function setView(v) {
  if (v === S.view) return;
  if (S.editing > 0 && !(await confirmDlg("放弃正在编辑的内容？", "切换视图会丢弃还没保存的编辑。", "放弃编辑", true))) return;
  S.editing = 0;
  // 尽量停在同一位置: 记录视图 ↔ 阅读视图按当前位置的记录下标换算
  let anchorIdx = null;
  if (S.view === "records") anchorIdx = S.recs.items.length ? S.recs.items[0].idx : null;
  else {
    const t = currentTurn();
    const e = t != null && S.ol ? S.ol.turns[t] : null;
    anchorIdx = e ? e.first : null;
  }
  S.view = v;
  lsSet("view", v);
  buildPages();
  renderBarDyn();
  renderOutline();
  if (v === "records") {
    S.recs.filter.clear();
    await loadRecPage(anchorIdx != null ? Math.floor(anchorIdx / S.recs.size) : 0);
    if (anchorIdx != null) focusIdxEl(anchorIdx, null, true);
  } else {
    let p = 0;
    if (anchorIdx != null) { const pp = pageOfTurn(turnOfIdx(anchorIdx)); if (pp >= 0) p = pp; }
    await loadTurnPage(p);
    if (anchorIdx != null) { const el = $('#rCol .turn[data-t="' + turnOfIdx(anchorIdx) + '"]'); if (el) el.scrollIntoView({ block: "start" }); }
  }
  if (S.find.open && S.find.q.trim()) applyFindMarks();
}

// ================= 查找 =================
const FIND = (() => {
  const inp = h("input", { placeholder: "查找", autocomplete: "off", spellcheck: "false", "aria-label": "在会话中查找" });
  const cnt = h("span.cnt.tnum");
  const prev = ib("arrowU", "上一个匹配（Shift+Enter）", () => stepFind(-1), "sm");
  const next = ib("arrowD", "下一个匹配（Enter）", () => stepFind(1), "sm");
  const close = ib("x", "关闭查找（Esc）", () => closeFind(), "sm");
  const box = h("div.findbar", { role: "search", hidden: true }, h("label.field", null, icon("search", "sm faint"), inp), cnt, prev, next, close);
  return { box, inp, cnt, prev, next };
})();
const runFindSoon = debounce(() => guard(runFind)(false), 350);
FIND.inp.addEventListener("input", () => { S.find.q = FIND.inp.value; updateFindUI(); runFindSoon(); });
FIND.inp.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    if (S.find.q.trim() !== S.find.ranQ) guard(runFind)(false);
    else stepFind(e.shiftKey ? -1 : 1);
  } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFind(); }
});
function openFind() {
  if (!S.path) return;
  S.find.open = true;
  FIND.box.hidden = false;
  renderBarDyn();
  FIND.inp.focus();
  FIND.inp.select();
  updateFindUI();
}
function closeFind() {
  S.find = { open: false, q: "", hits: [], total: 0, pos: 0, busy: false, trunc: false, pend: null, ranQ: "" };
  FIND.inp.value = "";
  FIND.box.hidden = true;
  clearMarks($("#rCol"));
  renderBarDyn();
}
function updateFindUI() {
  const f = S.find;
  FIND.cnt.textContent = f.busy ? "查找中…" : !f.q.trim() ? "" : f.ranQ !== f.q.trim() ? "" : !f.total ? "无结果" : f.pos + " / " + fmtNum(f.total) + (f.trunc ? "+" : "");
  FIND.prev.disabled = FIND.next.disabled = !f.total;
}
async function runFind(silent) {
  const q = S.find.q.trim();
  if (!q) { S.find.hits = []; S.find.total = 0; S.find.pos = 0; S.find.ranQ = ""; updateFindUI(); clearMarks($("#rCol")); return; }
  S.find.busy = true;
  updateFindUI();
  let j;
  try { j = await api("/api/search?q=" + enc(q)); } finally { S.find.busy = false; }
  if (q !== S.find.q.trim()) return;   // 输入已经变了, 这次结果作废
  S.find.hits = j.hits;
  S.find.total = j.total;
  S.find.trunc = j.truncated;
  S.find.ranQ = q;
  S.find.pos = j.total ? (silent ? Math.min(Math.max(S.find.pos, 1), j.total) : 1) : 0;
  updateFindUI();
  applyFindMarks();
  if (!silent && S.find.pos) await gotoHit(S.find.pos);
}
function stepFind(d) {
  const f = S.find;
  if (!f.total) return;
  f.pos = ((f.pos - 1 + d) % f.total + f.total) % f.total + 1;
  updateFindUI();
  guard(gotoHit)(f.pos);
}
function hitAt(pos) {
  let r = pos;
  for (const x of S.find.hits) { if (r <= x.n) return { hit: x, nth: r - 1 }; r -= x.n; }
  return null;
}
async function gotoHit(pos) {
  const x = hitAt(pos);
  if (!x) return;
  const idx = x.hit.idx;
  if (S.view === "records") {
    if (S.recs.filter.size) S.recs.filter.clear();
    const p = Math.floor(idx / S.recs.size);
    if (p !== S.recs.page || !S.recs.items.length) await loadRecPage(p, { noScroll: true });
    focusIdxEl(idx, x.nth);
    return;
  }
  const t = x.hit.t;
  let p = pageOfTurn(t);
  if (p < 0) { S.view = "all"; lsSet("view", "all"); buildPages(); renderBarDyn(); renderOutline(); p = pageOfTurn(t); }
  if (p !== S.page) await loadTurnPage(p, { noScroll: true });
  const tv = S.turns.find((v) => v.t === t);
  if (tv) { revealIdx(tv, idx); rerenderTurn(t); }
  S.find.pend = { idx, nth: x.nth };
  if (focusIdxEl(idx, x.nth)) {
    // 步骤详情是异步取的, 取回后再定位一次(见 afterDetail)
  } else {
    const el = $('#rCol .turn[data-t="' + t + '"]');
    if (el) { el.scrollIntoView({ block: "start" }); flashEl(el); }
  }
}
function revealIdx(tv, idx) {
  if (tv.q && tv.q.idx === idx) { if (tv.q.total > 3000) S.textOpen.add(tv.key + ":q"); return; }
  if (tv.final && tv.final.items.some((x) => x.idx === idx)) { if (tv.final.total > 3000) S.textOpen.add(tv.key + ":final"); return; }
  for (const ev of tv.lead.concat(tv.trail)) if (ev.idx === idx) { if (ev.detail) S.evOpen.add(ev.id); return; }
  if (tv.inj.some((x) => x.idx === idx)) { S.showInj = true; return; }
  for (const s of tv.steps) {
    const ids = [s.idx, s.call && s.call.idx, s.res && s.res.idx];
    if (!ids.includes(idx)) continue;
    S.turnOv.set(tv.key, true);
    if (s.kind === "tool") { S.stepOv.set(s.id, true); if (s.res && s.res.idx === idx) S.outAll.add(s.id); else S.paramsAll.set(s.id, true); }
    else if (s.kind === "thinking") S.stepOv.set(s.id, true);
    else if (s.kind === "note") S.textOpen.add(s.id);
    else if (s.kind === "event" && s.detail) S.evOpen.add(s.id);
    return;
  }
}
function clearMarks(root) {
  if (!root) return;
  root.querySelectorAll("mark.fm").forEach((m) => {
    const p = m.parentNode;
    if (!p) return;
    p.replaceChild(document.createTextNode(m.textContent), m);
    p.normalize();
  });
}
function markText(root, q) {
  const ql = q.toLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.nodeValue.toLowerCase().includes(ql) && !n.parentElement.closest("textarea, script, style, .no-mark")
      ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const text = n.nodeValue, low = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let pos = 0, at = low.indexOf(ql);
    while (at >= 0) {
      frag.append(text.slice(pos, at), h("mark.fm", null, text.slice(at, at + q.length)));
      pos = at + q.length;
      at = low.indexOf(ql, pos);
    }
    frag.append(text.slice(pos));
    n.parentNode.replaceChild(frag, n);
  }
}
function applyFindMarks(root) {
  root = root || $("#rCol");
  clearMarks(root);
  const q = S.find.open ? S.find.ranQ : "";
  if (q) markText(root, q);
}
function flashEl(el) {
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1800);
}
function focusIdxEl(idx, nth, quiet) {
  const el = $("#rCol").querySelector('[data-idxs~="' + idx + '"]');
  if (!el) return false;
  $("#rCol").querySelectorAll("mark.cur").forEach((m) => m.classList.remove("cur"));
  const marks = nth != null ? el.querySelectorAll("mark.fm") : [];
  const m = marks.length ? marks[Math.min(nth, marks.length - 1)] : null;
  if (m) { m.classList.add("cur"); m.scrollIntoView({ block: "center" }); } else el.scrollIntoView({ block: quiet ? "start" : "center" });
  if (!quiet) flashEl(el);
  return true;
}

// ================= 阅读页: 轮 =================
async function loadTurnPage(p, opt) {
  opt = opt || {};
  if (!S.pages.length) { S.turns = []; S.page = 0; renderTurns(opt); return; }
  p = Math.max(0, Math.min(p, S.pages.length - 1));
  const j = await api("/api/turns?mode=" + S.view + "&ts=" + S.pages[p].join(","));
  S.page = p;
  S.turns = j.turns;
  renderTurns(opt);
  renderOutlinePage();
}
function renderTurns(opt) {
  opt = opt || {};
  if (S.view === "records") return;
  const col = $("#rCol"), sc = $("#rScroll");
  const keep = opt.keepScroll ? sc.scrollTop : null;
  const kids = [pagerEl()];
  let any = false, prevMinor = true;
  for (const tv of S.turns) {
    const el = turnEl(tv);
    if (!el) continue;
    // 只有事件(斜杠命令、压缩…)的轮不画分隔线, 免得一串短行被切得很碎
    const minor = el.classList.contains("minor");
    if (any && !minor && !prevMinor) kids.push(h("div.turn-div", { "aria-hidden": "true" }));
    kids.push(el);
    any = true;
    prevMinor = minor;
  }
  if (!any) {
    kids.push(h("div.empty-note", null, S.view === "changes" ? "这个会话没有文件改动"
      : S.view === "chat" ? "这个会话没有可显示的对话" : "这个会话没有可显示的消息",
    S.view !== "all" ? h("div", null, h("button.btn.sm", { type: "button", style: { marginTop: "10px" }, onclick: () => guard(setView)("all") }, "查看全部")) : null));
  }
  kids.push(any ? pagerEl() : null);
  col.replaceChildren(...kids.filter(Boolean));
  if (keep !== null) sc.scrollTop = keep;
  else if (!opt.noScroll) sc.scrollTop = 0;
  applyFindMarks();
  S.olCur = null;
  updateOutlineActive();
  updateLatestBtn();
}
function rerenderTurn(t) {
  const old = $('#rCol .turn[data-t="' + t + '"]');
  const tv = S.turns.find((v) => v.t === t);
  if (!old || !tv) return;
  const el = turnEl(tv) || h("div.turn", { "data-t": t });
  old.replaceWith(el);
  applyFindMarks(el);
}
function pagerEl() {
  if (S.pages.length <= 1) return null;
  const p = S.page, n = S.pages.length, ts = S.pages[p];
  return h("div.pager", null,
    h("button.btn.sm", { type: "button", disabled: p === 0, onclick: guard(() => loadTurnPage(p - 1)) }, icon("chevL", "sm"), "上一页"),
    h("span.pinfo", null, "第 " + (p + 1) + " / " + n + " 页 · 第 " + (ts[0] + 1) + "–" + (ts[ts.length - 1] + 1) + " 轮"),
    h("button.btn.sm", { type: "button", disabled: p >= n - 1, onclick: guard(() => loadTurnPage(p + 1)) }, "下一页", icon("chevR", "sm")));
}
const isFail = (s) => s.kind === "tool" && (s.st === "error" || s.st === "interrupted");

function turnEl(tv) {
  const v = S.view;
  const rows = [];
  if (v === "all") {
    for (const ev of tv.lead) rows.push(eventEl(ev, false));
    if (S.showInj) for (const x of tv.inj) rows.push(injEl(x));
  }
  if (tv.q) rows.push(questionEl(tv));
  const agentRows = [];
  if (v !== "chat") agentRows.push(...timelineEls(tv));
  if (tv.final && v !== "changes") agentRows.push(finalEl(tv));
  if (agentRows.length) rows.push(agentEl(tv, agentRows));
  if (v === "all") for (const ev of tv.trail) rows.push(eventEl(ev, false));
  if (!rows.length) return null;
  const minor = !tv.q && !agentRows.length;
  return h("div.turn" + (minor ? ".minor" : ""), { "data-t": tv.t, "data-key": tv.key }, rows);
}
function agentEl(tv, rows) {
  const ts = tv.ats || (tv.final && tv.final.ts);
  return h("div.agent", null,
    h("span.avatar", { "aria-hidden": "true" }, curStyle().glyph),
    h("div.a-head", null, h("b", null, agentName()),
      ts ? h("time.faint.tnum", { title: new Date(ts).toLocaleString() }, fmtMsgTime(ts)) : null,
      tv.model ? h("span.model.trunc", null, "· " + tv.model) : null),
    rows);
}
function questionEl(tv) {
  const q = tv.q;
  const fetchFull = async () => (await Promise.all(q.blocks.map((x) => api("/api/block?idx=" + q.idx + "&b=" + x.b + "&f=text")))).map((j) => j.text).join("\n\n");
  const bubble = h("div.bubble", { "data-idxs": String(q.idx) });
  if (q.text) bubble.append(mdBox(q.text, q.total, tv.key + ":q", fetchFull));
  if (q.images && q.images.length) bubble.append(h("div.imgs", null, q.images.map(imgThumb)));
  if (!q.text && !(q.images && q.images.length)) bubble.append(h("span.faint", null, "（空）"));
  const refs = q.blocks.map((x) => ({ idx: q.idx, b: x.b, f: "text" }));
  const meta = h("div.q-meta", null, h("span.sr", null, "你"),
    h("span.hov.row-gap", null,
      ib("copy", "复制这条提问", guard(async () => copyText(q.total > q.text.length ? await fetchFull() : q.text, "已复制这条提问")), "xxs", "sm"),
      refs.length ? ib("pencil", "编辑这条提问", () => editBlocks(bubble, refs, { bubble: true }), "xxs", "sm") : null,
      ib("more", "记录操作", (e) => recordMenu(e.currentTarget, q.idx), "xxs", "sm")),
    q.ts ? h("time.tnum", { title: new Date(q.ts).toLocaleString() }, fmtMsgTime(q.ts)) : null);
  return h("div.q-row", null, h("div.q-wrap", null, meta, bubble));
}
function imgThumb(im) {
  const src = "/api/image?idx=" + im.idx + "&b=" + im.b + "&r=" + (S.m.rev || 0);
  const img = h("img", { src, alt: "图片", loading: "lazy" });
  img.addEventListener("click", () => lightbox(src));
  return img;
}
/** Markdown 正文: 超过 3000 字折叠到 1500 字; 后端只下发了一部分时展开再取全文 */
function mdBox(text, total, key, fetchFull) {
  const box = h("div.mdbox");
  const build = () => {
    const long = total > 3000;
    const open = S.textOpen.has(key);
    const src = open ? S.fullText.get(key) || text : long ? mdPreview(text, 1500) : text;
    const kids = [h("div.md", { html: renderMd(src) })];
    if (long) {
      const tg = h("button.lnk.collapse-tg", { type: "button", "aria-expanded": String(open) },
        open ? "收起" : ["展开完整内容 ", h("span.faint", null, "（" + fmtChars(total) + " 字）")]);
      tg.addEventListener("click", guard(async () => {
        if (!open && total > text.length && fetchFull && !S.fullText.has(key)) { tg.textContent = "加载中…"; S.fullText.set(key, await fetchFull()); }
        if (open) S.textOpen.delete(key); else S.textOpen.add(key);
        build();
        applyFindMarks(box);
      }));
      kids.push(tg);
    }
    box.replaceChildren(...kids);
  };
  build();
  return box;
}
function finalEl(tv) {
  const f = tv.final;
  const last = f.items[f.items.length - 1].idx;
  const fetchFull = async () => (await Promise.all(f.items.map((x) => api("/api/block?idx=" + x.idx + "&b=" + x.b + "&f=text")))).map((j) => j.text).join("\n\n");
  const body = mdBox(f.text, f.total, tv.key + ":final", fetchFull);
  const acts = h("div.facts.hov", null,
    ib("copy", "复制这条回复", guard(async () => copyText(f.total > f.text.length ? await fetchFull() : f.text, "已复制这条回复")), "xs", "sm"),
    ib("pencil", "编辑这条回复", () => editBlocks(body, f.items.map((x) => ({ idx: x.idx, b: x.b, f: "text" })), {}), "xs", "sm"),
    ib("more", "记录操作", (e) => recordMenu(e.currentTarget, last), "xs", "sm"));
  return h("div.final", { "data-idxs": f.items.map((x) => x.idx).join(" ") }, acts, body);
}
function timelineEls(tv) {
  const steps = tv.steps;
  if (!steps.length) return [];
  if (S.view === "changes") return [h("div.steps", null, steps.map((s) => stepEl(tv, s)))];
  if (steps.length === 1 && !isFail(steps[0])) return [stepEl(tv, steps[0])];
  const fails = steps.filter(isFail);
  const ov = S.turnOv.get(tv.key);
  const open = ov !== undefined ? ov : S.expandAll || tv.aborted || !tv.final;
  const out = [timelineRow(tv, open, fails)];
  const shown = open ? steps : fails.slice(0, 5);
  if (shown.length) out.push(h("div.steps", null, shown.map((s) => stepEl(tv, s))));
  return out;
}
function timelineRow(tv, open, fails) {
  const sm = tv.sum || {};
  const parts = [];
  if (sm.cmds) parts.push([sm.cmds + " 个命令"]);
  if (sm.files) parts.push(["改了 " + sm.files + " 个文件"]);
  if (sm.errors) parts.push([sm.errors + " 个失败", "st-error"]);
  if (sm.dur) parts.push(["⏱ " + fmtDur(sm.dur)]);
  const steps = sm.steps || tv.steps.length;
  const hidden = open ? 0 : Math.max(0, fails.length - 5);
  const btn = h("button.tl-btn" + (open ? ".open" : ""), { type: "button", "aria-expanded": String(open) },
    icon("chevR", "chev"),
    h("span.trunc.tnum", null, h("strong", null, "执行过程 · " + steps + " 步"),
      parts.map(([t, c]) => [h("span.sep", null, " · "), h("span" + (c ? "." + c : ""), null, t)]),
      hidden ? h("span.faint", null, "　还有 " + hidden + " 个失败") : null,
      !tv.final && !tv.aborted ? h("span.faint", null, "　这一轮没有最终回复") : null));
  btn.addEventListener("click", () => { S.turnOv.set(tv.key, !open); rerenderTurn(tv.t); });
  return h("div.tl", null, btn, ib("copy", "复制这一轮（Markdown）", guard(() => copyTurn(tv)), "xs.hov-copy", "sm"));
}
function stepEl(tv, s) {
  if (s.kind === "tool") return toolStepEl(tv, s);
  if (s.kind === "thinking") return thinkEl(tv, s);
  if (s.kind === "note") return noteEl(tv, s);
  if (s.kind === "event") return eventEl(s, true);
  if (s.kind === "image") return h("div.note", { "data-idxs": String(s.idx) }, h("span.gl", null, "▣"), h("div.imgs", null, imgThumb(s)));
  return h("span");
}
function rerenderStep(tv, s) {
  const old = $('#rCol [data-step="' + CSS.escape(s.id) + '"]');
  if (!old) return;
  const el = stepEl(tv, s);
  old.replaceWith(el);
  applyFindMarks(el);
}
function stepTitleEl(s) {
  const st = curStyle();
  const el = h("span.step-title" + (st.call ? ".call" : ""));
  el.append(h("span.verb", null, s.verb || s.name || "工具"));
  if (s.target) {
    if (st.call) el.append(h("span.paren", null, "("), h("span.tgt", null, s.target), h("span.paren", null, ")"));
    else el.append(" ", h("span.tgt.mono", null, s.target));
  }
  return el;
}
function toolStepEl(tv, s) {
  const ov = S.stepOv.get(s.id);
  const open = ov !== undefined ? ov : isFail(s);
  const ids = [s.call && s.call.idx, s.res && s.res.idx].filter((x) => x != null);
  const meta = [];
  if (isFail(s)) meta.push(h("span.st-" + s.st, { style: { fontWeight: 500 } }, STATUS_TEXT[s.st]));
  if (s.exit != null && s.exit !== 0) meta.push(h("span.st-error", null, "exit " + s.exit));
  if (s.add || s.del) meta.push(h("span", null, h("span.add", null, "+" + (s.add || 0)), " ", h("span.del", null, "−" + (s.del || 0))));
  if (s.dur) meta.push(h("span", null, fmtDur(s.dur)));
  if (s.lines && !(s.add || s.del)) meta.push(h("span", null, s.lines + " 行"));
  const metaEl = h("span.step-meta");
  meta.forEach((m, k) => { if (k) metaEl.append(h("span.sep", null, "·")); metaEl.append(m); });
  metaEl.append(icon("chevR", "sm chev"));
  const full = [s.verb, s.target].filter(Boolean).join(" ") + (s.detail ? " · " + s.detail : "") + "（" + STATUS_TEXT[s.st] + "）";
  const btn = h("button.step-btn" + (open ? ".open" : ""), { type: "button", "aria-expanded": String(open), title: full },
    h("span.gl.st-" + s.st, null, curStyle().step), stepTitleEl(s), s.detail ? h("span.step-detail-txt", null, s.detail) : null, metaEl);
  btn.addEventListener("click", () => { S.stepOv.set(s.id, !open); rerenderStep(tv, s); });
  const wrap = h("div.step", { "data-step": s.id, "data-idxs": ids.join(" ") }, btn);
  if (open) wrap.append(stepDetailEl(tv, s));
  return wrap;
}
async function loadDetail(s) {
  const q = [];
  if (s.call) q.push("call=" + s.call.idx + ":" + s.call.b);
  if (s.res) q.push("res=" + s.res.idx + ":" + s.res.b);
  const j = await api("/api/step?" + q.join("&"));
  S.details.set(s.id, j);
  return j;
}
function afterDetail(s) {
  const p = S.find.pend;
  applyFindMarks();
  if (p && [s.call && s.call.idx, s.res && s.res.idx].includes(p.idx)) { focusIdxEl(p.idx, p.nth); S.find.pend = null; }
}
function stepDetailEl(tv, s) {
  const box = h("div.sd");
  const d = S.details.get(s.id);
  const st = curStyle();
  if (!d) {
    if (s.pv) box.append(outEl(tv, s, { out: s.pv, ototal: s.pv.length, olines: s.pv.split("\n").length }, true));
    box.append(h("div.faint", { style: { fontSize: "12px" } }, "正在读取详情…"));
    loadDetail(s).then(() => { rerenderStep(tv, s); afterDetail(s); }).catch((e) => toast(e.message, true));
    return box;
  }
  const refs = h("div.refs");
  if (s.call) refs.append(lnk("调用 #" + s.call.idx, (e) => recordMenu(e.currentTarget, s.call.idx)));
  if (s.res) {
    refs.append(lnk("结果 #" + s.res.idx, (e) => recordMenu(e.currentTarget, s.res.idx)));
    refs.append(lnk("编辑输出", () => {
      const host = box.querySelector(".outbox > div") || box;
      editBlocks(host, [{ idx: s.res.idx, b: s.res.b, f: "content" }], { code: true });
    }));
  }
  if (d.out) refs.append(lnk("复制输出", guard(async () => copyText(d.ototal > d.out.length ? (await api("/api/block?idx=" + s.res.idx + "&b=" + s.res.b + "&f=content")).text : d.out, "已复制输出"))));
  box.append(refs);
  const shell = s.tk === "shell" || s.tk === "search";
  if (d.cmd && shell && (d.cmd.includes("\n") || d.cmd.length > 200)) box.append(h("div.md", { html: codeBlockHtml(d.cmd, "sh") }));
  if (d.params && !(shell && d.cmd)) box.append(paramsEl(s, d));
  if (d.diff && d.diff.length) box.append(h("section", null, h("h4", null, "改动"), diffEl(d.diff, d.dtrunc)));
  if (d.out !== undefined && (d.out || d.ototal)) box.append(outEl(tv, s, d, false));
  else if (s.res && !d.out) box.append(h("div.faint", { style: { fontSize: "12px" } }, st.conn + "（无输出）"));
  return box;
}
function paramsEl(s, d) {
  const key = s.id;
  const st = S.paramsAll.get(key);
  const open = st !== undefined ? !!st : isFail(s) || !s.res;
  const tg = h("button.lnk", { type: "button", "aria-expanded": String(open) }, open ? "参数 ▾" : "参数 ▸");
  const sec = h("section", null, tg);
  tg.addEventListener("click", () => { S.paramsAll.set(key, !open); sec.replaceWith(paramsEl(s, d)); });
  if (open) {
    const text = d.params;
    sec.append(h("pre.params" + (text.length > 4000 ? "" : ".wrap"), { tabindex: "0", style: { marginTop: "4px" } }, text));
    if (d.ptotal > d.params.length) {
      sec.append(h("div.more-line", null, "参数共 " + fmtChars(d.ptotal) + " 字，只显示了开头 ",
        lnk("显示全部", guard(async () => { const j = await api("/api/block?idx=" + s.call.idx + "&b=" + s.call.b + "&f=input"); d.params = j.text; d.ptotal = j.text.length; sec.replaceWith(paramsEl(s, d)); }))));
    }
  }
  return sec;
}
function diffEl(lines, trunc) {
  const box = h("div.diff", { tabindex: "0" });
  for (const l of lines) {
    if (l.t === "file") box.append(h("div.dfile", null, l.s));
    else if (l.t === "gap") box.append(h("div.dgap", null, "⋮"));
    else box.append(h("div.dl." + l.t, null, h("span.sg", null, l.t === "add" ? "+" : l.t === "del" ? "−" : " "), h("span.tx", null, l.s)));
  }
  if (trunc) box.append(h("div.dgap", null, "…（改动较长，只显示了前面一部分）"));
  return box;
}
function outEl(tv, s, d, preview) {
  const st = curStyle();
  const all = S.outAll.has(s.id);
  const fail = isFail(s);
  const N = fail ? 6 : 12;
  const lines = d.out.split("\n");
  const fullText = S.fullOut.get(s.id);
  let shown, hidden;
  if (all) { shown = fullText || d.out; hidden = 0; } else { shown = lines.slice(0, N).join("\n"); hidden = Math.max(0, (d.olines || lines.length) - Math.min(N, lines.length)); }
  const pre = h("pre.out" + (fail ? ".err" : ""), { tabindex: "0" }, shown);
  const more = h("div.more-line");
  if (!preview) {
    if (!all && (hidden > 0 || d.ototal > d.out.length)) {
      if (hidden > 0) more.append(h("span.tnum", null, "…还有 " + fmtNum(hidden) + " 行"));
      more.append(lnk("显示全部", guard(async () => {
        if (d.ototal > d.out.length && !S.fullOut.has(s.id)) S.fullOut.set(s.id, (await api("/api/block?idx=" + s.res.idx + "&b=" + s.res.b + "&f=content")).text);
        S.outAll.add(s.id);
        rerenderStep(tv, s);
      })));
    } else if (all) {
      if (!fullText && d.ototal > d.out.length) {
        more.append(h("span", null, "输出共 " + fmtChars(d.ototal) + " 字 "), lnk("加载完整输出", guard(async () => { S.fullOut.set(s.id, (await api("/api/block?idx=" + s.res.idx + "&b=" + s.res.b + "&f=content")).text); rerenderStep(tv, s); })));
      }
      if (lines.length > N || fullText) more.append(lnk("收起", () => { S.outAll.delete(s.id); rerenderStep(tv, s); }));
    }
  }
  return h("div.outbox", null, h("span.conn", { "aria-hidden": "true" }, st.conn), h("div", null, pre, more.childNodes.length ? more : null));
}
function thinkEl(tv, s) {
  const st = curStyle();
  const dur = s.dur ? fmtDur(s.dur) : "";
  if (s.red) {
    // 思考正文被加密(只有签名): 和 cc-switch 一样只显示一行, 不可展开
    return h("div.step", { "data-step": s.id, "data-idxs": String(s.idx) },
      h("div.step-static.think-row", { title: "这段思考的内容已加密，记录里没有明文" + (dur ? "（用时 " + dur + "）" : "") },
        h("span.gl.faint", null, st.think), h("span.lbl", null, st.thinkLabel),
        h("span.faint.trunc", null, "思考内容不可见"),
        dur ? h("span.step-meta", null, dur) : null));
  }
  const open = !!S.stepOv.get(s.id);
  const btn = h("button.step-btn.think-row" + (open ? ".open" : ""), { type: "button", "aria-expanded": String(open) },
    h("span.gl.faint", null, st.think), h("span.lbl", null, st.thinkLabel),
    !open ? h("span.faint.trunc", { style: { fontStyle: "italic" } }, oneLine(s.pv, 160)) : null,
    h("span.step-meta", null, fmtChars(s.chars) + " 字" + (dur ? " · " + dur : ""), icon("chevR", "sm chev")));
  btn.addEventListener("click", () => { S.stepOv.set(s.id, !open); rerenderStep(tv, s); });
  const wrap = h("div.step", { "data-step": s.id, "data-idxs": String(s.idx) }, btn);
  if (open) {
    const full = S.thinkFull.get(s.id);
    const body = h("div.think-body", null, full || (s.pv + (s.chars > (s.pv || "").length ? " …" : "")));
    if (!full && s.chars > (s.pv || "").length) {
      api("/api/block?idx=" + s.idx + "&b=" + s.b + "&f=thinking").then((j) => { S.thinkFull.set(s.id, j.text); rerenderStep(tv, s); afterDetail({ call: { idx: s.idx } }); })
        .catch((e) => toast(e.message, true));
    }
    wrap.append(body, h("div.more-line", { style: { padding: "0 6px 8px 30px", marginTop: "-4px" } },
      lnk("编辑", () => editBlocks(body, [{ idx: s.idx, b: s.b, f: "thinking" }], {})),
      lnk("复制", guard(async () => copyText(S.thinkFull.get(s.id) || (await api("/api/block?idx=" + s.idx + "&b=" + s.b + "&f=thinking")).text, "已复制思考内容"))),
      lnk("记录 #" + s.idx, (e) => recordMenu(e.currentTarget, s.idx))));
  }
  return wrap;
}
function noteEl(tv, s) {
  const text = s.text || "";
  const long = s.total > 240 || text.split("\n").length > 3;
  const open = S.textOpen.has(s.id);
  const src = open ? S.fullText.get(s.id) || text : text;
  const nb = h("div.nb" + (long && !open ? ".clamp" : ""), null, h("div.md", { html: renderMd(src) }));
  const body = h("div", { style: { flex: "1", minWidth: "0" } }, nb);
  if (long) {
    body.append(lnk(open ? "收起" : "展开说明", guard(async () => {
      if (!open && s.total > text.length && !S.fullText.has(s.id)) S.fullText.set(s.id, (await api("/api/block?idx=" + s.idx + "&b=" + s.b + "&f=text")).text);
      if (open) S.textOpen.delete(s.id); else S.textOpen.add(s.id);
      rerenderStep(tv, s);
    }), "collapse-tg"));
  }
  const acts = h("span.nacts.hov", null,
    ib("pencil", "编辑这段说明", () => editBlocks(nb, [{ idx: s.idx, b: s.b, f: "text" }], {}), "xxs", "sm"),
    ib("more", "记录操作", (e) => recordMenu(e.currentTarget, s.idx), "xxs", "sm"));
  return h("div.note", { "data-step": s.id, "data-idxs": String(s.idx) }, h("span.gl", null, "◦"), body, acts);
}
function eventEl(ev, inline) {
  const danger = ev.ek === "hook";
  const open = S.evOpen.has(ev.id);
  let label;
  if (ev.url) {
    label = h("a.ev-btn", { href: ev.url, target: "_blank", rel: "noopener noreferrer" }, ev.label, icon("ext", "xs"));
  } else if (ev.detail) {
    label = h("button.ev-btn", { type: "button", "aria-expanded": String(open) }, h("span.trunc", null, ev.label), icon("chevR", "xs" + (open ? " rot" : "")));
    label.addEventListener("click", () => {
      if (open) S.evOpen.delete(ev.id); else S.evOpen.add(ev.id);
      const el = eventEl(ev, inline);
      wrap.replaceWith(el);
      applyFindMarks(el);
    });
  } else label = h("span.trunc", null, ev.label);
  const wrap = h("div.ev-block", { "data-idxs": String(ev.idx), "data-step": inline ? ev.id : null },
    h("div.ev" + (inline ? ".inline" : "") + (danger ? ".danger" : ""), { role: "note" },
      inline ? h("span.gl", null, "·") : null, label,
      h("span.hov", null, ib("more", "记录操作", (e) => recordMenu(e.currentTarget, ev.idx), "xxs", "sm"))),
    open && ev.detail ? h("div.ev-detail", null, ev.detail + (ev.dtotal > ev.detail.length ? "\n\n…（共 " + fmtChars(ev.dtotal) + " 字，完整内容见「记录」视图）" : "")) : null);
  return wrap;
}
function injEl(x) {
  const open = S.injOpen.has(x.idx);
  const btn = h("button.inj-btn", { type: "button", "aria-expanded": String(open) },
    icon("chevR", "sm" + (open ? " rot" : "")), h("span.trunc", null, "上下文 · " + (x.label || "注入内容") + " · " + fmtNum(x.chars) + " 字"));
  const el = h("div.inj", { "data-idxs": String(x.idx) }, btn);
  btn.addEventListener("click", guard(async () => {
    if (open) S.injOpen.delete(x.idx);
    else S.injOpen.set(x.idx, (await api("/api/block?idx=" + x.idx + "&b=-2&f=all")).text);
    const nel = injEl(x);
    el.replaceWith(nel);
    applyFindMarks(nel);
  }));
  if (open) el.append(h("div.inj-body", null, S.injOpen.get(x.idx)));
  return el;
}
async function copyTurn(tv) {
  const parts = [];
  if (tv.q) {
    const t = tv.q.total > tv.q.text.length ? (await Promise.all(tv.q.blocks.map((x) => api("/api/block?idx=" + tv.q.idx + "&b=" + x.b + "&f=text")))).map((j) => j.text).join("\n\n") : tv.q.text;
    parts.push("### 你\n\n" + t.trim());
  }
  const tools = tv.steps.filter((s) => s.kind === "tool");
  if (tools.length) {
    parts.push("<details><summary>执行过程 · " + tools.length + " 步</summary>\n\n" +
      tools.map((s) => "- `" + [s.verb, s.target].filter(Boolean).join(" ").replace(/`/g, "'") + "`" + (isFail(s) ? " ✗" : "")).join("\n") + "\n\n</details>");
  }
  if (tv.final) {
    const f = tv.final;
    const t = f.total > f.text.length ? (await Promise.all(f.items.map((x) => api("/api/block?idx=" + x.idx + "&b=" + x.b + "&f=text")))).map((j) => j.text).join("\n\n") : f.text;
    parts.push("### " + agentName() + "\n\n" + t.trim());
  }
  await copyText(parts.join("\n\n"), "已复制这一轮");
}

// ================= 就地编辑 =================
async function editBlocks(host, refs, opt) {
  opt = opt || {};
  if (host.classList.contains("editing")) return;
  if (S.m.readonly) toast("提示：该来源只读，修改后请用「存到本地」另存", false);
  let texts;
  try {
    texts = await Promise.all(refs.map((r) => api("/api/block?idx=" + r.idx + "&b=" + r.b + "&f=" + r.f).then((j) => j.text)));
  } catch (e) { toast("读取原文失败：" + e.message, true); return; }
  S.editing++;
  const prev = [...host.childNodes];
  const tas = refs.map((r, k) => {
    const ta = h("textarea" + (opt.code || r.f === "content" ? ".code" : ""), { spellcheck: "false" });
    ta.value = texts[k];
    const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight + 4, window.innerHeight * 0.7) + "px"; };
    ta.addEventListener("input", grow);
    requestAnimationFrame(grow);
    return ta;
  });
  let busy = false;
  const finish = () => { S.editing = Math.max(0, S.editing - 1); host.classList.remove("editing"); };
  const cancel = () => { if (busy) return; finish(); host.replaceChildren(...prev); };
  const saveIt = guard(async () => {
    if (busy) return;
    const changed = refs.map((r, k) => Object.assign({}, r, { value: tas[k].value })).filter((r, k) => r.value !== texts[k]);
    if (!changed.length) { cancel(); return; }
    busy = true;
    try {
      for (const c of changed) applyMeta(await api("/api/patch", { idx: c.idx, b: c.b, f: c.f, value: c.value }));
    } finally { busy = false; }
    finish();
    toast(changed.length + " 处修改已应用（尚未写回文件）");
    await refreshAfterMutation({ keepScroll: true });
  });
  const editor = h("div.editor.no-mark",
    null,
    refs.length > 1 ? refs.map((r, k) => [h("div.ed-lbl", null, "第 " + (k + 1) + " / " + refs.length + " 段 · 记录 #" + r.idx), tas[k]]) : tas[0],
    h("div.ed-acts", null, h("span.hint", null, "Ctrl+Enter 保存 · Esc 取消"),
      h("button.btn.sm", { type: "button", onclick: cancel }, "取消"),
      h("button.btn.sm.primary", { type: "button", onclick: saveIt }, "应用修改")));
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); }
    else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveIt(); }
  });
  host.classList.add("editing");
  host.replaceChildren(editor);
  tas[0].focus();
}

// ================= 记录操作 =================
function recordMenu(anchor, idx) {
  openMenu(anchor, [
    { label: "在上方插入消息…", icon: "plus", onClick: () => guard(insertDialog)(idx, "above") },
    { label: "在下方插入消息…", icon: "plus", onClick: () => guard(insertDialog)(idx, "below") },
    { label: "复制此条", icon: "copyPlus", onClick: () => guard(mutate)("/api/duplicate", { idx }, idx + 1) },
    { label: "上移", icon: "arrowU", disabled: idx <= 0, onClick: () => guard(mutate)("/api/move", { from: idx, to: idx - 1 }, idx - 1) },
    { label: "下移", icon: "arrowD", disabled: idx >= (S.m.total || 0) - 1, onClick: () => guard(mutate)("/api/move", { from: idx, to: idx + 1 }, idx + 1) },
    { sep: true },
    { label: "从这里 Fork 新会话…", icon: "fork", onClick: () => guard(forkFrom)(idx) },
    S.view !== "records" ? { label: "在「记录」视图中查看", icon: "list", onClick: () => guard(gotoRecord)(idx) } : null,
    { sep: true },
    { label: "截断此条之后的全部…", icon: "scissors", danger: true, onClick: () => guard(truncateAfter)(idx) },
    { label: "删除此条…", icon: "trash", danger: true, onClick: () => guard(deleteRec)(idx) },
  ], { title: "记录 #" + idx });
}
async function mutate(url, body, focusIdx) {
  const j = await api(url, body);
  applyMeta(j);
  await refreshAfterMutation({ focusIdx: focusIdx != null ? focusIdx : j.idx });
  return j;
}
async function refreshAfterMutation(opt) {
  opt = opt || {};
  const focusIdx = opt.focusIdx != null ? opt.focusIdx : null;
  await loadOutline();
  renderHead();
  renderBarDyn();
  renderOutline();
  if (S.view === "records") {
    const p = focusIdx != null && !S.recs.filter.size ? Math.floor(focusIdx / S.recs.size) : S.recs.page;
    await loadRecPage(Math.min(p, Math.max(0, Math.ceil((S.m.total || 0) / S.recs.size) - 1)), { keepScroll: focusIdx == null || opt.keepScroll });
  } else {
    let p = S.page;
    if (focusIdx != null) { const pp = pageOfTurn(turnOfIdx(focusIdx)); if (pp >= 0) p = pp; }
    await loadTurnPage(Math.min(p, Math.max(0, S.pages.length - 1)), { keepScroll: focusIdx == null || opt.keepScroll, noScroll: true });
  }
  if (focusIdx != null && !opt.keepScroll) focusIdxEl(focusIdx);
  if (S.find.open && S.find.ranQ) guard(runFind)(true);
}
async function doUndo() { if (!S.path || !S.m.canUndo) return; const j = await api("/api/undo", {}); applyMeta(j); if (!j.noop) { await refreshAfterMutation({ keepScroll: true }); toast("已撤销"); } }
async function doRedo() { if (!S.path || !S.m.canRedo) return; const j = await api("/api/redo", {}); applyMeta(j); if (!j.noop) { await refreshAfterMutation({ keepScroll: true }); toast("已重做"); } }
async function deleteRec(idx) {
  const ok = await confirmDlg("删除记录 #" + idx + "？", "子记录会自动接到它的父记录上，保持链表完整。删除后可以撤销。", "删除", true);
  if (ok) await mutate("/api/delete", { idx }, Math.max(0, idx - 1));
}
async function truncateAfter(idx) {
  const n = (S.m.total || 0) - idx - 1;
  if (n <= 0) { toast("这已经是最后一条"); return; }
  const ok = await confirmDlg("截断记录 #" + idx + " 之后的内容？", "将删除这之后的 " + fmtNum(n) + " 条记录。可以撤销。", "截断", true);
  if (ok) await mutate("/api/truncate", { idx }, idx);
}
async function insertDialog(idx, where) {
  let role = "user";
  const ta = h("textarea.inp", { rows: "7", placeholder: "消息内容（支持 Markdown）" });
  const radios = [["user", "用户"], ["assistant", "助手"], ["system", "系统"]].map(([v, t]) =>
    h("label.radio-card", null, h("input", { type: "radio", name: "irole", value: v, checked: v === "user", onchange: () => { role = v; } }), t));
  const body = h("div", null,
    h("div.form-row", null, h("span.lb", null, "角色"), h("div.ct", null, radios)),
    h("div.form-row.top", null, h("span.lb", null, "内容"), h("div.ct", null, ta)),
    h("div.form-hint", null, "插入到记录 #" + idx + (where === "above" ? " 的上方" : " 的下方") + "。Claude 会话会自动接好 parentUuid 链。"));
  const ok = await dialog({ title: "插入消息", body, actions: [{ label: "取消", value: false }, { label: "插入", value: true, primary: true }], focus: ta });
  if (!ok) return;
  const j = await mutate("/api/insert-text", { idx, where, role, text: ta.value }, where === "below" ? idx + 1 : idx);
  toast("已插入到 #" + j.idx);
}
async function gotoRecord(idx) {
  S.recs.filter.clear();
  if (S.view !== "records") {
    S.view = "records";
    lsSet("view", "records");
    buildPages();
    renderBarDyn();
    renderOutline();
  }
  await loadRecPage(Math.floor(idx / S.recs.size), { noScroll: true });
  focusIdxEl(idx);
}
async function forkFrom(idx) {
  const a = await api("/api/fork-analysis?idx=" + idx);
  const body = h("div", null, h("p", null, "把开头到记录 #" + idx + " 的 " + fmtNum(idx + 1) + " 条记录复制成一个新 sessionId 的独立会话文件（写在同一目录），当前会话不受影响。"));
  if (a.edited.length || a.bash.length) {
    const box = h("div.warnbox", null, h("b", null, "分叉点之后有文件改动。"), " Fork 只分叉对话，不会把工作区文件还原到当时的状态。");
    body.append(box);
    if (a.edited.length) {
      body.append(h("div.form-hint", null, "编辑类改动（可以用 git 还原）· " + a.edited.length + " 个文件："),
        h("div.flist", null, a.edited.slice(0, 30).map((e) => e.file + "  [" + e.tools.join("/") + " ×" + e.count + "]").join("\n") + (a.edited.length > 30 ? "\n…另 " + (a.edited.length - 30) + " 个" : "")));
    }
    if (a.bash.length) {
      body.append(h("div.form-hint", null, a.bash.length + " 条 Shell 命令（无法从记录还原，可能改过文件或产物）："),
        h("div.flist", null, a.bash.slice(0, 8).map((c) => "$ " + c.split("\n")[0].slice(0, 100)).join("\n") + (a.bash.length > 8 ? "\n…另 " + (a.bash.length - 8) + " 条" : "")));
    }
  }
  const ok = await dialog({ title: "从记录 #" + idx + " Fork 新会话", body, wide: true, actions: [{ label: "取消", value: false }, { label: "Fork", value: true, primary: true }] });
  if (!ok) return;
  const j = await api("/api/fork", { idx });
  await loadSessions(true);
  const open = await confirmDlg("已生成新会话", h("div", null, h("p", null, fmtNum(j.count) + " 条记录 · sessionId " + j.sessionId), h("div.flist", null, j.path)), "立即打开");
  if (open) await openSession(j.path, { force: true });
}

// ================= 保存 / 导出 / 备份 =================
async function save() {
  if (!S.path || !S.m.dirty) return;
  if (S.m.readonly) { toast(S.m.readonly, true); return; }
  const j = await api("/api/save", {});
  S.m.dirty = false;
  renderHead();
  toast("已保存" + (j.backup ? " · 备份 " + baseName(j.backup) : ""));
  loadSessions(true);
}
async function saveLocal() {
  const dft = S.path.toLowerCase().endsWith("store.db") ? baseName(S.path.replace(/[\\/]store\.db$/i, "")) : baseName(S.path).replace(/\.jsonl$/i, "");
  const name = await promptDlg("存到本地", "文件名", dft, "保存", "保存到工具目录的 saved_sessions/，首行记下来源（源文件、工作目录、时间），之后可以再编辑或导出。");
  if (name === null) return;
  const j = await api("/api/save-local", { name });
  await loadSessions(true);
  toast("已存到本地：" + baseName(j.path) + "（" + fmtNum(j.count) + " 条）");
}
async function exportFile(fmt) {
  const text = await apiText("/api/export?fmt=" + fmt);
  download(safeName(sessionTitle()) + (fmt === "md" ? ".md" : ".txt"), text);
}
async function openExport() {
  let target = "claude";
  const cwd = h("input.inp", { value: S.m.cwd || "", placeholder: "决定会话归属的工作目录", spellcheck: "false" });
  const title = h("input.inp", { placeholder: "留空则取第一条提问" });
  const lean = h("input", { type: "checkbox", checked: true });
  const max = h("input.inp", { type: "number", min: "0", value: "8000", style: { width: "110px" } });
  const lastN = h("input.inp", { type: "number", min: "0", value: "0", style: { width: "110px" } });
  const hint = h("div.form-hint");
  const upd = () => {
    hint.textContent = target === "claude"
      ? "写到 ~/.claude/projects/<目录编码>/<sessionId>.jsonl；在该工作目录下运行 claude --resume 即可看到（工作目录必填）。"
      : "写到 ~/.codex/sessions/… 并登记到 Codex 的会话索引；codex resume 即可看到。若 Codex 正开着，请重启后查看。";
  };
  const radios = [["claude", "Claude Code"], ["codex", "Codex"]].map(([v, t]) =>
    h("label.radio-card", null, h("input", { type: "radio", name: "etarget", value: v, checked: v === "claude", onchange: () => { target = v; upd(); } }), t));
  upd();
  const row = (lb, ct) => h("div.form-row", null, h("span.lb", null, lb), h("div.ct", null, ct));
  const body = h("div", null, row("目标", radios), row("工作目录", cwd), row("标题", title),
    row("内容", h("label", { style: { display: "inline-flex", gap: "6px", alignItems: "center" } }, lean, "精简：只留对话正文，去掉工具调用 / 结果和思考")),
    row("每条上限", [max, h("span.form-hint", null, "字符，0 = 不限")]),
    row("最近条数", [lastN, h("span.form-hint", null, "条，0 = 全部")]),
    h("div.form-hint", { style: { margin: "8px 0 4px" } }, "超长会话导出后可能超出对方的上下文上限，建议保持精简并限制每条长度。"), hint);
  const ok = await dialog({
    title: "导出到其它 CLI", body, wide: true,
    actions: [{ label: "取消", value: false }, {
      label: "导出", value: true, primary: true,
      validate: () => { if (target === "claude" && !cwd.value.trim()) { toast("导出到 Claude Code 需要填写工作目录", true); cwd.focus(); return false; } return true; },
    }],
  });
  if (!ok) return;
  const req = { target, options: { lean: lean.checked, maxChars: parseInt(max.value, 10) || 0, lastN: parseInt(lastN.value, 10) || 0 } };
  if (cwd.value.trim()) req.cwd = cwd.value.trim();
  if (title.value.trim()) req.title = title.value.trim();
  const j = await api("/api/export-to", req);
  const lines = [h("p", null, "已写入 " + fmtNum(j.count) + " 条消息。"), h("div.flist", null, j.path + "\nsession_id: " + j.session_id + "\ncwd: " + (j.cwd || "（空）"))];
  if (target === "claude") lines.push(h("p", null, "在该工作目录下运行 claude --resume 即可看到。"));
  else if (j.registered) lines.push(h("p", null, "已登记到 Codex 会话索引（state_5.sqlite）。若 Codex 正开着，请重启后在 codex resume 里查看。"));
  else lines.push(h("div.warnbox", null, "未能登记到 Codex 索引：" + (j.register_error || "未知") + "。rollout 已写入，但可能不显示；若数据库被占用，请关闭 Codex 后重试。"));
  await dialog({ title: "已导出到 " + (target === "claude" ? "Claude Code" : "Codex"), body: h("div", null, lines), wide: true, actions: [{ label: "好", value: true, primary: true }] });
  loadSessions(true);
}
async function openBackups() {
  const j = await api("/api/backups");
  const list = h("div");
  if (!j.backups.length) list.append(h("p", null, "还没有备份。每次保存或回滚前会自动生成 <文件名>.bak.<时间戳>。"));
  let picked = null;
  for (const b of j.backups) {
    list.append(h("div.bak-row", null, h("span.bn", null, b.name, h("small", null, fmtMsgTime(b.mtime * 1000) + " · " + fmtSize(b.size))),
      h("button.btn.sm.danger", { type: "button", onclick: (e) => { picked = b; e.currentTarget.closest(".dialog").querySelector(".dfoot .btn").click(); } }, "回滚到此")));
  }
  await dialog({ title: "备份与回滚", body: list, wide: true, actions: [{ label: "关闭", value: null }] });
  if (!picked) return;
  const ok = await confirmDlg("回滚到这个备份？", "当前内容会被替换为 " + picked.name + " 的内容。回滚后需要再点保存才会写回文件（保存时会先备份当前文件）。", "回滚", true);
  if (!ok) return;
  applyMeta(await api("/api/rollback", { backup: picked.path }));
  resetReaderState();
  await refreshAfterMutation({ keepScroll: false });
  toast("已回滚（未写盘，请检查后保存）");
}

// ================= 记录视图 =================
const REC_FILTERS = [["user", "user"], ["assistant", "assistant"], ["tool_use", "工具调用"], ["tool_result", "工具结果"], ["thinking", "思考"], ["system", "system"]];
async function loadRecPage(p, opt) {
  opt = opt || {};
  const f = [...S.recs.filter].join(",");
  const j = await api("/api/page?start=" + p * S.recs.size + "&count=" + S.recs.size + "&filter=" + enc(f));
  S.recs.page = p;
  S.recs.total = j.total;
  S.recs.items = j.items;
  renderRecs(opt);
}
function renderRecs(opt) {
  opt = opt || {};
  const col = $("#rCol"), sc = $("#rScroll");
  const keep = opt.keepScroll ? sc.scrollTop : null;
  const chips = h("div.chips", { style: { border: "0", padding: "0 0 4px" } }, h("span.faint", { style: { fontSize: "12px", lineHeight: "24px" } }, "只看："),
    REC_FILTERS.map(([tok, label]) => h("button.chip" + (S.recs.filter.has(tok) ? ".on" : ""), {
      type: "button", "aria-pressed": String(S.recs.filter.has(tok)),
      onclick: guard(async () => { if (S.recs.filter.has(tok)) S.recs.filter.delete(tok); else S.recs.filter.add(tok); await loadRecPage(0); }),
    }, label)),
    S.recs.filter.size ? lnk("清除", guard(async () => { S.recs.filter.clear(); await loadRecPage(0); })) : null);
  const kids = [chips, recPagerEl()];
  if (!S.recs.items.length) kids.push(h("div.empty-note", null, S.recs.filter.size ? "没有符合筛选的记录" : "没有记录"));
  for (const it of S.recs.items) kids.push(recordCard(it.rec, it.idx));
  kids.push(S.recs.items.length > 5 ? recPagerEl() : null);
  col.replaceChildren(...kids.filter(Boolean));
  if (keep !== null) sc.scrollTop = keep; else if (!opt.noScroll) sc.scrollTop = 0;
  applyFindMarks();
  updateLatestBtn();
}
function recPagerEl() {
  const n = Math.max(1, Math.ceil(S.recs.total / S.recs.size)), p = S.recs.page;
  const start = p * S.recs.size;
  const sizeSel = h("select.inp", { style: { width: "auto", height: "28px" }, title: "每页条数" },
    [25, 50, 100, 200].map((k) => h("option", { value: String(k), selected: k === S.recs.size }, k + " 条/页")));
  sizeSel.addEventListener("change", guard(async () => { S.recs.size = parseInt(sizeSel.value, 10); lsSet("recSize", String(S.recs.size)); await loadRecPage(0); }));
  const jump = h("input.inp", { type: "number", min: "1", max: String(n), placeholder: "页", style: { width: "64px", height: "28px" } });
  jump.addEventListener("keydown", (e) => { if (e.key === "Enter") { const k = parseInt(jump.value, 10); if (k >= 1 && k <= n) guard(loadRecPage)(k - 1); } });
  const info = "第 " + (p + 1) + " / " + n + " 页 · " + (S.recs.filter.size
    ? "筛选出 " + fmtNum(S.recs.total) + " 条"
    : "#" + start + "–" + Math.max(start, Math.min(start + S.recs.size, S.recs.total) - 1) + "，共 " + fmtNum(S.recs.total) + " 条");
  return h("div.pager", null,
    h("button.btn.sm", { type: "button", disabled: p === 0, onclick: guard(() => loadRecPage(p - 1)) }, icon("chevL", "sm"), "上一页"),
    h("span.pinfo", null, info),
    h("button.btn.sm", { type: "button", disabled: p >= n - 1, onclick: guard(() => loadRecPage(p + 1)) }, "下一页", icon("chevR", "sm")),
    n > 2 ? jump : null, sizeSel);
}
function recRole(rec) {
  if (rec.type === "summary") return "summary";
  if (rec.type === "turn_ended") return "turn_ended";
  if (rec.type === "cc_meta") return "来源信息";
  return rec.role || (rec.message && rec.message.role) || rec.type || "?";
}
function resultTextOf(p) {
  const c = p.content !== undefined ? p.content : p.output;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.filter((x) => x && x.type === "text").map((x) => x.text || "").join("\n");
  return c == null ? "" : JSON.stringify(c, null, 2);
}
function recBlocks(rec) {
  const out = [];
  if (rec.type === "summary") { out.push({ kind: "text", b: -1, f: "text", text: rec.summary || "", label: "summary" }); return out; }
  if (rec.type === "cc_meta") { out.push({ kind: "meta" }); return out; }
  const m = rec.message;
  if (!m || typeof m !== "object") { out.push({ kind: "raw", label: rec.type || "记录", text: JSON.stringify(rec, null, 2) }); return out; }
  const c = m.content;
  if (typeof c === "string") { out.push({ kind: "text", b: -1, f: "text", text: c, label: "text" }); return out; }
  if (Array.isArray(c)) {
    c.forEach((p, b) => {
      if (!p || typeof p !== "object") return;
      if (p.type === "text") out.push({ kind: "text", b, f: "text", text: p.text || "", label: "text" });
      else if (p.type === "thinking" || p.type === "redacted_thinking" || p.type === "reasoning") out.push({ kind: "thinking", b, f: "thinking", text: p.thinking || p.text || "", label: p.type });
      else if (p.type === "tool_use" || p.type === "server_tool_use") out.push({ kind: "tool_use", b, label: "tool_use · " + (p.name || "?"), text: typeof p.input === "string" ? p.input : JSON.stringify(p.input, null, 2) });
      else if (p.type === "tool_result") {
        const editable = typeof p.content === "string" || (Array.isArray(p.content) && p.content.every((x) => x && x.type === "text"));
        out.push({ kind: "tool_result", b, f: "content", text: resultTextOf(p), label: "tool_result" + (p.is_error ? "（错误）" : ""), editable });
      } else if (p.type === "image") out.push({ kind: "image", b, label: "image" });
      else out.push({ kind: "raw", label: p.type || "?", text: JSON.stringify(p, null, 2) });
    });
  }
  if (!out.length) out.push({ kind: "raw", label: "（空）", text: JSON.stringify(rec, null, 2) });
  return out;
}
function longPre(text, cls) {
  const LIM = 4000;
  const pre = h("pre" + (cls ? "." + cls : ""));
  if (text.length <= LIM) { pre.textContent = text; return pre; }
  let all = false;
  const tg = lnk("", () => { all = !all; upd(); });
  const upd = () => { pre.textContent = all ? text : text.slice(0, LIM) + " …"; tg.textContent = all ? "收起" : "展开全部（" + fmtChars(text.length) + " 字）"; };
  upd();
  return h("div", null, pre, h("div.more-line", null, tg));
}
function recordCard(rec, idx) {
  const role = recRole(rec);
  const head = h("div.rec-head", null, h("span.idx", null, "#" + idx), h("span.role." + String(role).replace(/[^\w]/g, ""), null, role),
    rec.timestamp ? h("span.faint", null, fmtMsgTime(Date.parse(rec.timestamp))) : null,
    rec.isSidechain ? h("span.badge", null, "sidechain") : null,
    rec.isMeta ? h("span.badge", null, "meta") : null,
    rec.uuid ? h("span.faint.mono", { title: rec.uuid }, String(rec.uuid).slice(0, 8)) : null,
    h("div.acts", null,
      ib("fork", "从这里 Fork 新会话", () => guard(forkFrom)(idx), "xs", "sm"),
      ib("trash", "删除此条", () => guard(deleteRec)(idx), "xs", "sm"),
      ib("more", "更多操作", (e) => recordMenu(e.currentTarget, idx), "xs", "sm")));
  const body = h("div.rec-body");
  for (const b of recBlocks(rec)) {
    if (b.kind === "meta") {
      body.append(h("div.blk", null, h("div.blk-lbl", null, "来源信息"),
        h("div.kv", null, [["来源", (SRC[rec.origin] || {}).name || rec.origin || "?"], ["工作目录", rec.cwd || "（未知）"], ["源文件", rec.source_path || "（未知）"], ["保存时间", rec.saved_at || ""]]
          .map(([k, v]) => [h("span.k", null, k), h("span.v", null, v)]))));
      continue;
    }
    const editable = b.kind === "text" || b.kind === "thinking" || (b.kind === "tool_result" && b.editable);
    const content = h("div");
    if (b.kind === "text") content.append(h("div.md", { html: renderMd(b.text) }));
    else if (b.kind === "thinking") content.append(h("div.think-body", { style: { padding: "0" } }, b.text || "（思考内容不可见）"));
    else if (b.kind === "image") content.append(h("div.imgs", { style: { marginTop: "0" } }, imgThumb({ idx, b: b.b })));
    else content.append(longPre(b.text || ""));
    const lbl = h("div.blk-lbl", null, h("span", null, b.label),
      editable ? lnk("编辑", () => editBlocks(content, [{ idx, b: b.b, f: b.f }], { code: b.kind !== "text" })) : null,
      b.text ? lnk("复制", () => copyText(b.text, "已复制")) : null);
    body.append(h("div.blk." + b.kind, null, lbl, content));
  }
  return h("div.rec", { "data-idxs": String(idx) }, head, body);
}

// ================= 对话目录 =================
function renderOutline() {
  const nav = $("#outline");
  const on = S.outlineOn && S.view !== "records" && S.ol && S.ol.turns.length > 0;
  nav.hidden = !on;
  $(".r-body").classList.toggle("no-outline", !on);
  if (!on) return;
  const entries = S.ol.turns.filter((e) => e.q !== null || e.fin || e.steps > 0 || e.ab);
  const list = h("ol.ol-list", { style: { position: "relative" } });
  for (const e of entries) {
    const it = h("li.ol-item", { "data-t": e.t });
    if (e.q !== null) {
      it.append(h("button.ol-q", { type: "button", onclick: guard(() => jumpTo(e.t, "q")) },
        h("span.who", null, "你" + (e.qts ? " · " + fmtMsgTime(e.qts) : "")), h("span.bub", null, e.q || "（只有图片）")));
    } else if (e.slash) {
      it.append(h("button.ol-q", { type: "button", onclick: guard(() => jumpTo(e.t, "q")) }, h("span.who", null, e.slash)));
    }
    if (e.fin || e.steps || e.ab) {
      it.append(h("button.ol-a", { type: "button", onclick: guard(() => jumpTo(e.t, "r")) },
        h("span.av", { "aria-hidden": "true" }, curStyle().glyph),
        h("span.txt", null,
          h("span.st", null, e.steps ? e.steps + " 步" : agentName(), e.fail ? h("span.dot", { title: "有失败步骤" }) : null),
          h("span.rp" + (e.r ? "" : ".none"), null, e.r || (e.ab ? "已中断" : "没有回复")))));
    }
    list.append(it);
  }
  nav.replaceChildren(h("div.ol-head", null, "对话目录", h("span.n.tnum", null, String(entries.length)), h("span.spacer"),
    ib("x", "关闭目录", () => { S.outlineOn = false; lsSet("outline", "0"); renderBarDyn(); renderOutline(); }, "xs", "sm")), list);
  renderOutlinePage();
}
function renderOutlinePage() {
  const nav = $("#outline");
  if (nav.hidden) return;
  const on = new Set(S.pages[S.page] || []);
  nav.querySelectorAll(".ol-item").forEach((li) => li.classList.toggle("offpage", !on.has(+li.dataset.t)));
  S.olCur = null;
  updateOutlineActive();
}
async function jumpTo(t, part) {
  let p = pageOfTurn(t);
  if (p < 0) { S.view = "all"; lsSet("view", "all"); buildPages(); renderBarDyn(); p = pageOfTurn(t); }
  if (p < 0) return;
  if (p !== S.page) await loadTurnPage(p, { noScroll: true });
  const el = $('#rCol .turn[data-t="' + t + '"]');
  if (!el) return;
  const target = part === "q" ? el.querySelector(".q-row") || el : el.querySelector(".final") || el.querySelector(".agent") || el;
  target.scrollIntoView({ block: "start" });
  flashEl(part === "q" ? target.querySelector(".bubble") || target : target);
}
function currentTurn() {
  const sc = $("#rScroll");
  const top = sc.getBoundingClientRect().top + 12;
  const turns = $("#rCol").querySelectorAll(".turn");
  if (!turns.length) return null;
  if (sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 24) return +turns[turns.length - 1].dataset.t;
  for (const el of turns) if (el.getBoundingClientRect().bottom > top) return +el.dataset.t;
  return +turns[turns.length - 1].dataset.t;
}
function updateOutlineActive() {
  const nav = $("#outline");
  if (nav.hidden) return;
  const t = currentTurn();
  if (t === S.olCur) return;
  S.olCur = t;
  const list = nav.querySelector(".ol-list");
  if (!list) return;
  list.querySelectorAll(".ol-item.cur").forEach((x) => x.classList.remove("cur"));
  const it = t != null ? list.querySelector('.ol-item[data-t="' + t + '"]') : null;
  if (!it) return;
  it.classList.add("cur");
  if (it.offsetTop < list.scrollTop + 8) list.scrollTop = it.offsetTop - 8;
  else if (it.offsetTop + it.offsetHeight > list.scrollTop + list.clientHeight - 8) list.scrollTop = it.offsetTop + it.offsetHeight - list.clientHeight + 8;
}
function updateLatestBtn() {
  const b = $("#latestBtn");
  if (!S.path || S.view === "records" || !S.pages.length) { b.hidden = true; return; }
  const sc = $("#rScroll");
  const atBottom = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 40;
  b.hidden = S.page >= S.pages.length - 1 && atBottom;
}
async function stepTurn(d) {
  const cur = currentTurn();
  const els = [...$("#rCol").querySelectorAll(".turn")];
  const k = els.findIndex((el) => +el.dataset.t === cur);
  const target = els[k + d];
  if (target && k >= 0) { target.scrollIntoView({ block: "start" }); return; }
  if (d > 0 && S.page < S.pages.length - 1) await loadTurnPage(S.page + 1);
  else if (d < 0 && S.page > 0) {
    await loadTurnPage(S.page - 1, { noScroll: true });
    const all = $("#rCol").querySelectorAll(".turn");
    if (all.length) all[all.length - 1].scrollIntoView({ block: "start" });
  }
}

// ================= 欢迎页 / 初始化 =================
function renderWelcome() {
  const inp = h("input", { placeholder: "或粘贴 .jsonl / store.db / rollout 的完整路径", autocomplete: "off", spellcheck: "false", "aria-label": "会话文件路径" });
  const go = () => { const v = inp.value.trim().replace(/^"|"$/g, ""); if (v) openSession(v, { force: true }); };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  const hero = icon("history");
  hero.style.width = hero.style.height = "40px";
  $("#welcome").replaceChildren(h("div.card", null, h("div.hero", null, hero),
    h("h2", null, "从左侧选择一个会话"),
    h("p", null, "读取本机 Claude Code、Codex、Cursor 的会话记录，以及工具目录 saved_sessions/ 里存到本地的会话。"),
    h("p.faint", null, "阅读视图按「提问 → 执行过程 → 最终回复」整理；需要逐条修改时切到「记录」视图。"),
    h("div.open-row", null, h("label.field", null, icon("file", "sm faint"), inp), h("button.btn", { type: "button", onclick: go }, "打开"))));
}
function initEvents() {
  $("#rScroll").addEventListener("scroll", () => {
    if (MENU) closeMenu();
    if (initEvents.raf) return;
    initEvents.raf = requestAnimationFrame(() => { initEvents.raf = 0; updateOutlineActive(); updateLatestBtn(); });
  }, { passive: true });
  $("#sideList").addEventListener("scroll", () => { if (MENU) closeMenu(); }, { passive: true });
  $("#sideScrim").addEventListener("click", () => toggleSide(false));
  const latest = $("#latestBtn");
  latest.replaceChildren(icon("arrowD", "sm"), "最新");
  latest.addEventListener("click", guard(async () => {
    if (S.page < S.pages.length - 1) await loadTurnPage(S.pages.length - 1, { noScroll: true });
    const sc = $("#rScroll");
    sc.scrollTop = sc.scrollHeight;
    updateLatestBtn();
  }));
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    const key = (e.key || "").toLowerCase();
    if (mod && key === "s") { e.preventDefault(); if (S.path && S.m.dirty && !S.m.readonly) guard(save)(); return; }
    if (mod && key === "f" && S.path) { e.preventDefault(); openFind(); return; }
    if (e.key === "Escape" && MENU) { closeMenu(); return; }
    if (isTyping(e.target)) return;
    if (mod && !e.shiftKey && key === "z") { e.preventDefault(); guard(doUndo)(); return; }
    if (mod && (key === "y" || (e.shiftKey && key === "z"))) { e.preventDefault(); guard(doRedo)(); return; }
    if (e.key === "Escape" && S.find.open) { closeFind(); return; }
    if (!mod && !e.altKey && (key === "j" || key === "k") && S.path && S.view !== "records" && !$(".modal-bg")) {
      e.preventDefault();
      guard(stepTurn)(key === "j" ? 1 : -1);
    }
  });
  window.addEventListener("beforeunload", (e) => { if (S.m.dirty || S.editing > 0) { e.preventDefault(); e.returnValue = ""; } });
  window.matchMedia("(max-width: 900px)").addEventListener("change", (m) => toggleSide(!m.matches && lsGet("side", "1") === "1"));
}
(function init() {
  applyTheme(lsGet("theme", "auto"));
  const params = new URLSearchParams(location.search);
  const file = params.get("file");
  if (["all", "chat", "changes", "records"].includes(params.get("view"))) S.view = params.get("view");
  if (narrow()) S.sideOn = !file;
  $("#app").classList.toggle("side-off", !S.sideOn);
  renderSideHead();
  renderSideTools();
  initSideSearch();
  renderSideList();
  renderWelcome();
  initEvents();
  loadSessions(true);
  if (file) openSession(file, { force: true, skipConfirm: true });
})();
