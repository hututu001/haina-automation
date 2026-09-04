/* ============================================================
   海纳百川控制台 · 前端逻辑
   依赖后端 API：/api/state /api/run /api/logs /api/logfile
                /api/usage /api/login /api/settings /api/notify_test
   ============================================================ */
"use strict";

const $ = id => document.getElementById(id);

/* ---------- 通用 ---------- */
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(p, o) {
  const r = await fetch(p, o);
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch (e) { throw new Error(t); }
  if (!r.ok) throw new Error(d.error || t);
  return d;
}

let toastTimer = null;
function showToast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.className = bad ? "show bad" : "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.className = "", 2800);
}

function fmtCode(c) {
  if (c === null || c === undefined) return '<span class="chip run">运行中</span>';
  return c === 0 ? '<span class="chip ok">成功</span>' : `<span class="chip bad">失败 ${esc(c)}</span>`;
}
function markToday(v) {
  if (!v) return null;
  const t = new Date().toLocaleDateString("sv-SE");
  if (v === t) return '<span class="chip ok">今日已跑</span>';
  if (v.startsWith(t + " ")) return `<span class="chip ok">今日 ${esc(v.split(" ")[1])} 已跑</span>`;
  return null;
}
function fmtT(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) : "";
}

/* ---------- 主题 ---------- */
function applyThemeIcon() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("iconMoon").classList.toggle("hidden", !dark);
  $("iconSun").classList.toggle("hidden", dark);
}
$("themeBtn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("haina-theme", next);
  applyThemeIcon();
});
applyThemeIcon();

/* ---------- 全局状态 ---------- */
let state = null, followId = null, lastBusy = false, fileView = false;

/* ---------- 仪表盘 ---------- */
const S_ICON = {
  wallet: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="14" rx="3"/><path d="M2 10h20M16 15h2"/></svg>',
  box:    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8 12 3 3 8v8l9 5 9-5zM3 8l9 5 9-5M12 13v8"/></svg>',
  gift:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M5 12v9h14v-9M12 8s-1.5-5-4.5-5S5 8 8 8M12 8s1.5-5 4.5-5S19 8 16 8"/></svg>',
  grid:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/></svg>',
  bolt:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/></svg>'
};
function statCard(icon, k, v, sub) {
  return `<div class="stat"><div class="k">${icon}${esc(k)}</div>
    <div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : "<div class='s'></div>"}</div>`;
}
function renderStats() {
  const sm = state.summary || {}, w = sm.www || {}, f = sm.farm || {};
  const bal = f.balance ?? w.balance;
  $("stats").innerHTML = [
    statCard(S_ICON.wallet, "主站余额", bal ?? "—", `更新 ${fmtT(f.updated_at || w.updated_at) || "—"}`),
    statCard(S_ICON.box, "待兑换额度", f.pending ?? "—", f.weekly_level != null ? `周消费 Lv.${esc(f.weekly_level)}` : ""),
    statCard(S_ICON.gift, "可抽次数", w.draws_remaining ?? "—", w.today_spend != null ? `今日消耗 ${esc(w.today_spend)}` : ""),
    statCard(S_ICON.grid, "地块", f.plots ?? "—", f.locked != null ? `另锁定 ${esc(f.locked)} 块` : ""),
    statCard(S_ICON.bolt, "体力", f.stamina ?? "—", "偷菜 3 点/次")
  ].join("");
  $("stats").style.display = Object.keys(sm).length ? "" : "none";
}

/* ---------- 定时 ---------- */
function nextChips(next) {
  if (!next || !next.length) return "";
  const now = new Date();
  return next.map(s => {
    const m = String(s).match(/^(\d{2})-(\d{2}) (\d{2}):(\d{2}) (.+)$/);
    if (!m) return `<span class="nr-chip">${esc(s)}</span>`;
    let target = new Date(now.getFullYear(), +m[1] - 1, +m[2], +m[3], +m[4]);
    if (target.getTime() < now.getTime() - 864e5) target.setFullYear(target.getFullYear() + 1);
    const diff = target - now;
    const rel = diff <= 0 ? "即将" :
      diff < 36e5 ? Math.max(1, Math.round(diff / 6e4)) + " 分钟后" :
      diff < 864e5 ? Math.round(diff / 36e5) + " 小时后" :
      Math.round(diff / 864e5) + " 天后";
    return `<span class="nr-chip">${esc(m[5])} · ${rel}</span>`;
  }).join("");
}
function renderSched() {
  const sc = state.schedule, lm = state.last_marks || {};
  if (!sc.enabled) {
    $("schedinfo").innerHTML = '<span class="mut" style="font-size:13px">内置定时已关闭（仍可手动运行）</span>';
    $("nextrun").innerHTML = "";
    return;
  }
  const row = (name, rule, mark, manual) =>
    `<tr><td>${name}</td><td class="mut">${rule}</td><td style="text-align:right">${
      manual ? '<span class="chip mut">按需点击</span>' :
      (markToday(mark) || '<span class="chip mut">今日待跑</span>')}</td></tr>`;
  // 兑换：配置了独立时间则显示规则与今日状态；否则随农场任务
  const redeemRow = sc.redeem_times && sc.redeem_times.length
    ? row("兑换", `每天 ${esc(sc.redeem_times.join(" / "))}`, lm.last_redeem)
    : `<tr><td>兑换</td><td class="mut">随农场任务</td><td style="text-align:right"><span class="chip mut">跟随农场</span></td></tr>`;
  // 偷菜：与农场完全分开，未配置时间 = 仅手动
  const stealRow = sc.steal_times && sc.steal_times.length
    ? row("偷菜", `每天 ${esc(sc.steal_times.join(" / "))}`, lm.last_steal)
    : row("偷菜", "—", null, true);
  $("schedinfo").innerHTML = `<table class="sched-tb">` + [
    row("签到", `每天 ${esc(sc.signin_time)}`, lm.last_signin),
    row("农场", `每天 ${esc(sc.farm_times.join(" / ") || "—")}`, lm.last_farm),
    redeemRow,
    stealRow,
    row("抽奖", sc.draw_times.length ? `每天 ${esc(sc.draw_times.join(" / "))}` : "—", lm.last_draw, !sc.draw_times.length)
  ].join("") + `</table>`;
  $("nextrun").innerHTML = sc.next.length
    ? '<span class="mut">接下来</span>' + nextChips(sc.next) : "";
}

/* ---------- 运行历史 / 日志文件 ---------- */
function renderHistory() {
  $("history").innerHTML = state.runs.map(r =>
    `<li data-run="${r.id}"><span class="li-main">${esc(r.start)}　${esc(r.label)}
      <span class="mut">(${esc(r.trigger)})</span></span><span class="li-side">${fmtCode(r.code)}</span></li>`
  ).join("") || '<li class="static">还没有运行记录</li>';
  $("histCount").textContent = state.runs.length;
}
async function loadLogs() {
  try {
    const d = await api("/api/logs");
    $("logfiles").innerHTML = d.files.map(f =>
      `<li data-file="${esc(f.name)}"><span class="li-main">${esc(f.name)}</span>
       <span class="li-side">${esc(f.size)} · ${esc(f.mtime)}</span></li>`
    ).join("") || '<li class="static">暂无日志文件</li>';
  } catch (e) { /* 静默 */ }
}
/* 事件委托：历史 + 日志文件点击 */
$("history").addEventListener("click", e => {
  const li = e.target.closest("li[data-run]");
  if (!li) return;
  followId = +li.dataset.run; fileView = false;
  tick();
});
$("logfiles").addEventListener("click", e => {
  const li = e.target.closest("li[data-file]");
  if (!li) return;
  viewLogFile(li.dataset.file);
});
async function viewLogFile(name) {
  followId = null; fileView = true;
  try {
    const d = await api("/api/logfile?name=" + encodeURIComponent(name));
    renderLog(d.content);
    $("log").scrollTop = 1e9;
    $("runmeta").innerHTML = `文件 <b style="color:var(--tx)">${esc(d.name)}</b> · ${esc(d.size)}（只读，正在浏览文件，新任务输出不会打断）`;
  } catch (e) { showToast(e.message, true); }
}

/* ---------- 用量表 ---------- */
async function loadUsage() {
  const el = $("usagebody");
  try {
    const d = await api("/api/usage");
    if (d.needs_refresh) {
      el.innerHTML = `<div class="tip" style="margin-top:0">会话已过期，<a href="javascript:void(0)" onclick="run('status')" style="color:var(--acc)">跑一次状态总览刷新会话</a> 成功后再看。</div>`;
      return;
    }
    if (d.error) { el.textContent = "加载失败：" + d.error; return; }
    if (!d.items.length) { el.textContent = "暂无调用记录"; return; }
    let total = 0;
    const rows = d.items.map(it => {
      total += it.spend;
      const dt = new Date(it.ts * 1000);
      const today = new Date().toDateString() === dt.toDateString();
      const hm = dt.toTimeString().slice(0, 8);
      const time = today ? hm : (dt.getMonth() + 1) + "-" + dt.getDate() + " " + hm;
      return `<tr><td>${time}</td><td>${esc(it.model)}</td>
        <td class="num">${it.prompt.toLocaleString()}</td>
        <td class="num">${it.completion.toLocaleString()}</td>
        <td class="num">${it.spend.toFixed(3)}</td><td>${it.use_time}s</td></tr>`;
    }).join("");
    el.innerHTML = `<table class="utable"><tr><th>时间</th><th>模型</th><th>输入</th><th>输出</th><th>额度</th><th>耗时</th></tr>
      ${rows}<tfoot><tr><td colspan="4">合计</td><td class="num">${total.toFixed(3)}</td><td></td></tr></tfoot></table>`;
  } catch (e) { el.textContent = "加载失败：" + String(e.message || e); }
}

/* ---------- 日志渲染（按前缀着色） ---------- */
function logLineClass(l) {
  if (l.startsWith("[OK]")) return "l-ok";
  if (l.startsWith("[FAIL]")) return "l-bad";
  if (l.startsWith("[!]")) return "l-warn";
  if (l.startsWith("[*]")) return "l-info";
  if (l.startsWith("[i]") || l.startsWith("[控制台]")) return "l-dim";
  return "";
}
function renderLog(text) {
  $("log").innerHTML = text.split("\n").map(l => {
    const c = logLineClass(l);
    return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
  }).join("\n");
}
async function copyLog() {
  try { await navigator.clipboard.writeText($("log").textContent); showToast("日志已复制"); }
  catch (e) { showToast("复制失败：" + e.message, true); }
}

/* ---------- 主轮询 ---------- */
function renderTopbar() {
  const busy = state.busy;
  const pill = $("runpill");
  if (busy && state.current) {
    pill.classList.remove("hidden");
    $("runpillTxt").textContent = `${state.current.label} · ${state.current.elapsed ?? 0}s`;
    document.title = `▶ ${state.current.label} · 海纳百川控制台`;
  } else {
    pill.classList.add("hidden");
    document.title = "海纳百川控制台";
  }
}
function renderRunMeta(d) {
  const bits = [`任务 <b style="color:var(--tx)">${esc(d.label)}</b>`];
  if (d.running) bits.push(`<span class="spin"></span>运行中 · ${d.elapsed ?? 0}s`);
  else bits.push(fmtCode(d.code));
  if (d.start) bits.push(esc(d.start) + (d.end ? " → " + esc(d.end) : ""));
  $("runmeta").innerHTML = bits.join('<span class="mut">　·　</span>');
}
async function tick() {
  try { state = await api("/api/state"); } catch (e) { return; }
  const busy = state.busy;
  renderTopbar(); renderStats(); renderSched(); renderHistory();
  document.querySelectorAll(".tbtn").forEach(b => b.disabled = busy);
  // 登录卡：凭证未配齐时显示
  const s = state.settings;
  const noCred = !(s.username && s.has_password);
  $("loginCard").classList.toggle("hidden", !noCred);
  $("loginBanner").classList.toggle("hidden", !(!s.username && !s.has_password));
  // 按钮文字随行为设置变化
  $("btndrawTxt").textContent = s.draw_all ? "抽奖 · 抽光" : "抽奖 ×1";
  $("btnsigninTxt").textContent = s.signin_draw ? "签到 + 抽奖" : "签到";
  // 配置了兑换定时后，农场任务不再顺手兑换
  $("farmDesc").textContent = (s.redeem_times || []).length ? "收菜 · 补种" : "收菜 · 补种 · 兑换";
  // 输出面板
  if (!fileView) {
    if (busy && state.current) {
      $("runmeta").innerHTML = `<span class="spin"></span>正在运行 <b style="color:var(--tx)">${esc(state.current.label)}</b><span class="mut">（${esc(state.current.trigger)}）已 ${state.current.elapsed ?? 0} 秒</span>`;
    } else if (!followId) {
      $("runmeta").textContent = "空闲中";
    }
    if (state.current) followId = state.current.id;
    else if (followId === null && state.runs.length) followId = state.runs[0].id;
    else if (followId !== null && !state.runs.some(r => r.id === followId))
      followId = state.runs.length ? state.runs[0].id : null;
    if (followId !== null) {
      try {
        const d = await api("/api/run?id=" + followId);
        renderLog(d.output || "(等待输出…)");
        $("log").scrollTop = 1e9;
        renderRunMeta(d);
      } catch (e) { /* 静默 */ }
    }
  }
  $("clock").textContent = new Date().toLocaleString("zh-CN", { hour12: false });
  if (lastBusy && !busy) loadLogs();
  lastBusy = busy;
}
async function loop() {
  try { await tick(); } catch (e) { /* 静默 */ }
  setTimeout(loop, state && state.busy ? 800 : 1600);
}

/* ---------- 任务触发 ---------- */
async function run(task) {
  if (task === "draw") {
    const all = state && state.settings && state.settings.draw_all;
    const n = state && state.summary && state.summary.www && state.summary.www.draws_remaining;
    if (all) {
      if (!confirm(`抽光模式：将把全部 ${n != null ? n : "?"} 次可抽机会一次抽完，确定？`)) return;
    } else if (!confirm("确定抽奖 1 次？")) return;
  }
  if (task === "farm_steal" && !confirm("农场流程 + 偷菜（消耗体力），确定？")) return;
  if (task === "steal" && !confirm("只偷菜（3 体力/次，自动扫描全服可偷目标），确定？")) return;
  followId = null; fileView = false;
  $("runmeta").innerHTML = '<span class="mut">正在启动…</span>';
  $("log").textContent = "任务已启动，等待第一行输出…";
  try {
    const d = await api("/api/run", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task }) });
    followId = d.id;
  } catch (e) { showToast(e.message, true); $("log").textContent = ""; }
  tick();
}
document.querySelectorAll(".tbtn").forEach(
  b => b.addEventListener("click", () => run(b.dataset.task)));

/* ---------- 登录 ---------- */
async function login() {
  const u = $("l_user").value.trim(), p = $("l_pass").value;
  if (!u || !p) { showToast("请填写登录邮箱和密码", true); return; }
  const bt = $("btnlogin");
  bt.disabled = true; bt.textContent = "登录中…";
  $("log").textContent = "正在登录并验证（约 10~20 秒），输出实时显示…";
  try {
    const d = await api("/api/login", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }) });
    followId = d.id;
  } catch (e) { showToast("登录失败: " + e.message, true); }
  bt.disabled = false; bt.textContent = "登录并保存凭证";
  tick();
}
$("btnlogin").addEventListener("click", login);

/* ---------- 设置模态框 ---------- */
const modal = $("settingsModal");
function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".pane").forEach(p =>
    p.classList.toggle("active", p.dataset.pane === name));
}
function openSettings(tab) {
  modal.classList.add("open");
  switchTab(tab || "account");
  loadSettings();
}
function closeSettings() { modal.classList.remove("open"); }
$("settingsBtn").addEventListener("click", () => openSettings("account"));
$("modalClose").addEventListener("click", closeSettings);
$("modalCancel").addEventListener("click", closeSettings);
modal.addEventListener("click", e => { if (e.target === modal) closeSettings(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && modal.classList.contains("open")) closeSettings(); });
document.querySelectorAll(".tab-btn").forEach(b =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));

async function loadSettings() {
  try { state = await api("/api/state"); } catch (e) { return; }
  const s = state.settings;
  $("l_user").value = s.username || "";
  $("s_user").value = s.username || ""; $("s_pass").value = "";
  $("s_signin").value = s.signin_time;
  $("s_farm").value = s.farm_times.join(",");
  $("s_draw").value = (s.draw_times || []).join(",");
  $("s_redeem").value = (s.redeem_times || []).join(",");
  $("s_steal").value = (s.steal_times || []).join(",");
  $("s_crop").value = s.farm_crop || "";
  $("s_sched").checked = !!s.schedule_enabled;
  $("s_sdraw").checked = !!s.signin_draw;
  $("s_drawall").checked = !!s.draw_all;
  $("s_nmode").value = s.notify_mode || "off";
  $("s_whurl").value = s.webhook_url || "";
  $("s_nwhen").value = s.webhook_when || "fail";
  $("s_base").value = s.base_url || "";
  $("s_farmurl").value = s.farm_url || "";
  $("s_wwwsess").value = s.www_session || "";
  $("s_farmsess").value = s.farm_session || "";
  $("s_host").value = s.host || "";
  $("s_tproxy").checked = !!s.trust_proxy;
  $("whGroup").classList.toggle("hidden", $("s_nmode").value !== "webhook");
}
$("s_nmode").addEventListener("change", () =>
  $("whGroup").classList.toggle("hidden", $("s_nmode").value !== "webhook"));

async function saveSettings() {
  const body = {
    username: $("s_user").value.trim(), farm_crop: $("s_crop").value.trim(),
    signin_time: $("s_signin").value.trim(), farm_times: $("s_farm").value,
    draw_times: $("s_draw").value,
    redeem_times: $("s_redeem").value,
    steal_times: $("s_steal").value,
    schedule_enabled: $("s_sched").checked,
    signin_draw: $("s_sdraw").checked, draw_all: $("s_drawall").checked,
    notify_mode: $("s_nmode").value, webhook_url: $("s_whurl").value.trim(),
    webhook_when: $("s_nwhen").value,
    base_url: $("s_base").value.trim(), farm_url: $("s_farmurl").value.trim(),
    www_session: $("s_wwwsess").value.trim(), farm_session: $("s_farmsess").value.trim(),
    host: $("s_host").value.trim(), trust_proxy: $("s_tproxy").checked
  };
  if ($("s_pass").value) body.password = $("s_pass").value;
  try {
    await api("/api/settings", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("s_pass").value = "";
    showToast("已保存，立即生效（监听地址需重启生效）");
    closeSettings(); tick();
  } catch (e) { showToast("保存失败: " + e.message, true); }
}
$("btnsave").addEventListener("click", saveSettings);

async function testNotify() {
  try {
    const d = await api("/api/notify_test", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    showToast("测试推送已发出：" + d.detail);
  } catch (e) { showToast(e.message, true); }
}

/* ---------- 启动 ---------- */
loop();
loadLogs();
loadUsage();
setInterval(loadUsage, 60000);
