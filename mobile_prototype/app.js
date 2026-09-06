/* ============================================================
   app.js - QCS 质量协同系统移动端 (v3.1)
   ------------------------------------------------------------
   数据来源: FastAPI 后端 (api_server.py)
     POST /api/login                 登录
     GET  /api/me                    当前用户
     GET  /api/tasks                 措施列表(按部门过滤 + 逾期统计)
     GET  /api/issues                问题台账(按部门过滤)
     GET  /api/overdue               逾期/即将到期
     POST /api/measure_status        更新措施状态(核心动作)
     POST /api/upload_review_photo   上传完成证据照片(可选)
     GET  /api/measure_history       措施状态变更历史
   ------------------------------------------------------------
   v3.1 QCS: 一线不进系统, 只给管理层使用。
   - 底部导航: 措施 / 问题 / 逾期 / 我的
   - 措施状态: 待执行 → 执行中 → 已完成 → 已关闭 (逾期=实时计算)
   - 数据范围: manager 全部 P/Q/T; engineer 仅本部门
   ============================================================ */

// ============================================================
// 一、配置
// ============================================================

const API_BASE =
  window.location.hostname === "localhost" ||
  window.location.hostname === "127.0.0.1"
    ? "http://127.0.0.1:8000"
    : "http://" + window.location.hostname + ":8000";

// ============================================================
// 二、认证(token 存 localStorage, 所有请求带 Authorization 头)
// ============================================================
let AUTH_TOKEN = localStorage.getItem("lpa_token") || "";
let LOGIN_USER = { username: "", display_name: "", role: "", department: "" };

function saveToken(t) { AUTH_TOKEN = t; localStorage.setItem("lpa_token", t); }
function clearToken() { AUTH_TOKEN = ""; localStorage.removeItem("lpa_token"); }
function authHeaders() { return AUTH_TOKEN ? { "Authorization": "Bearer " + AUTH_TOKEN } : {}; }

/** 统一请求: 自动带 token; 收到 401 时清 token 并回到登录页 */
async function fetchWithAuth(url, options) {
  options = options || {};
  options.headers = Object.assign({}, options.headers || {}, authHeaders());
  const resp = await fetch(url, options);
  if (resp.status === 401) {
    clearToken();
    showLoginView();
  }
  return resp;
}

function showLoginView() {
  const pv = document.getElementById("phoneView");
  const lv = document.getElementById("loginView");
  if (pv) pv.style.display = "none";
  if (lv) lv.style.display = "flex";
}
function showAppView() {
  const pv = document.getElementById("phoneView");
  const lv = document.getElementById("loginView");
  // ★ 必须是 "flex": .phone 三段式布局(顶栏固定/内容滚动/底栏固定)依赖 flex
  if (pv) pv.style.display = "flex";
  if (lv) lv.style.display = "none";
}

// ============================================================
// 三、状态
// ============================================================
const MEASURE_STATUSES = ["待评审", "已采纳", "不采纳", "待执行", "执行中", "已完成", "已关闭"];

// v3.1 QCS: 状态 → 中文标签(列表分组标题)
function statusLabel(s) {
  const map = {
    "待评审": "⏳ 待评审",
    "已采纳": "👍 已采纳",
    "不采纳": "🚫 不采纳",
    "待执行": "⏳ 待执行",
    "执行中": "🔨 执行中",
    "已完成": "✅ 已完成",
    "已关闭": "🔒 已关闭",
  };
  return map[s] || s;
}

const DEPT_LABELS = { P: "P 制造/生产", Q: "Q 质量", T: "T 技术/工艺" };
const ROLE_LABELS = { admin: "系统管理员", manager: "经理", engineer: "工程师/主管" };

let currentTab = "measures";
let apiData = {
  role: "", department: "",
  groups: { 待评审: [], 不采纳: [], 待执行: [], 执行中: [], 已完成: [], 已关闭: [] },
  overdue: [], due_soon: [],
  stats: { total: 0, overdue: 0, due_soon: 0, by_status: {} },
};
let issuesData = [];
let overdueData = { overdue: [], due_soon: [], stats: {} };
let loadError = false;
let issuesLoaded = false;    // 问题台账是否已按需加载(仅经理)
let overdueLoaded = false;   // 逾期数据是否已加载
let personsData = { all: [], departments: [], engineers: {} };  // 选人名单(按部门工程师)

// 状态弹窗上下文
let statusContext = null;      // { measure }
let photoBlob = null;          // 压缩后的照片 Blob
let photoObjectUrl = null;     // 预览图临时 URL

// ============================================================
// 四、DOM 引用
// ============================================================
const avatarEl = document.getElementById("avatar");
const userNameEl = document.getElementById("userName");
const roleChipEl = document.getElementById("roleChip");
const overdueBadgeEl = document.getElementById("overdueBadge");
const operatorSwitchEl = document.getElementById("operatorSwitch");
const contentEl = document.getElementById("content");
const tabBadgeEl = document.getElementById("tabBadge");
const tabMeasuresEl = document.getElementById("tabMeasures");
const tabIssuesEl = document.getElementById("tabIssues");
const tabOverdueEl = document.getElementById("tabOverdue");
const tabMineEl = document.getElementById("tabMine");
const toastEl = document.getElementById("toast");

const statusMaskEl = document.getElementById("statusMask");
const statusTitleEl = document.getElementById("statusTitle");
const statusIssueEl = document.getElementById("statusIssue");
const statusSelectEl = document.getElementById("statusSelect");
const statusCommentEl = document.getElementById("statusComment");
const statusErrorEl = document.getElementById("statusError");
const statusOkBtnEl = document.getElementById("statusOkBtn");
const photoInputEl = document.getElementById("photoInput");
const photoPlaceholderEl = document.getElementById("photoPlaceholder");
const photoHintBoxEl = document.getElementById("photoHintBox");
const photoPreviewEl = document.getElementById("photoPreview");
const photoTextEl = document.getElementById("photoText");

// ============================================================
// 五、数据加载
// ============================================================

async function loadMeasures() {
  try {
    const resp = await fetchWithAuth(API_BASE + "/api/tasks");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    apiData = {
      role: data.role || "",
      department: data.department || "",
      groups: data.groups || { 待执行: [], 执行中: [], 已完成: [], 已关闭: [] },
      overdue: data.overdue || [],
      due_soon: data.due_soon || [],
      stats: data.stats || { total: 0, overdue: 0, due_soon: 0, by_status: {} },
    };
    loadError = false;
  } catch (e) {
    loadError = true;
    console.error("加载措施失败:", e);
  }
}

async function loadIssues() {
  try {
    const resp = await fetchWithAuth(API_BASE + "/api/issues");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    issuesData = (data && data.issues) || [];
  } catch (e) {
    issuesData = [];
    console.error("加载问题失败:", e);
  }
}

async function loadOverdue() {
  try {
    const resp = await fetchWithAuth(API_BASE + "/api/overdue");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    overdueData = {
      overdue: (data && data.overdue) || [],
      due_soon: (data && data.due_soon) || [],
      stats: (data && data.stats) || { overdue: 0, due_soon: 0, by_department: {} },
    };
  } catch (e) {
    overdueData = { overdue: [], due_soon: [], stats: { overdue: 0, due_soon: 0, by_department: {} } };
    console.error("加载逾期失败:", e);
  }
}

/** 用 /api/tasks 的 issue_no/measure_order 补全 /api/overdue 数据(后者缺这两字段) */
function enrichOverdueWithIssueNo() {
  const noMap = {};
  const groups = apiData.groups || {};
  MEASURE_STATUSES.forEach((s) => {
    (groups[s] || []).forEach((m) => {
      if (m.measure_id != null) {
        noMap[m.measure_id] = { issue_no: m.issue_no, measure_order: m.measure_order };
      }
    });
  });
  (overdueData.overdue || []).forEach((m) => {
    const extra = noMap[m.measure_id];
    if (extra) { m.issue_no = extra.issue_no; m.measure_order = extra.measure_order; }
  });
  (overdueData.due_soon || []).forEach((m) => {
    const extra = noMap[m.measure_id];
    if (extra) { m.issue_no = extra.issue_no; m.measure_order = extra.measure_order; }
  });
}

async function loadAll() {
  // 只刷新"已加载过"的模块, 避免每次操作都拉全量(首屏/懒加载友好)
  await loadMeasures();
  if (issuesLoaded) await loadIssues();
  if (overdueLoaded) await loadOverdue();
  enrichOverdueWithIssueNo();
  render();
}

// ============================================================
// 六、渲染
// ============================================================

function renderHeader() {
  const name = LOGIN_USER.display_name || LOGIN_USER.username || "?";
  avatarEl.textContent = (name || "?").charAt(0);
  userNameEl.textContent = name;

  const dept = LOGIN_USER.department;
  const deptText = dept ? (" · " + (DEPT_LABELS[dept] || dept)) : "";
  roleChipEl.textContent = (ROLE_LABELS[LOGIN_USER.role] || LOGIN_USER.role) + deptText;

  const ov = apiData.stats.overdue || 0;
  overdueBadgeEl.textContent = ov;
  tabBadgeEl.textContent = ov;
  tabBadgeEl.className = "tab-badge" + (ov > 0 ? " show" : "");
}

function renderOperatorSwitch() {
  const name = LOGIN_USER.display_name || LOGIN_USER.username || "?";
  operatorSwitchEl.innerHTML = "";
  const chip = document.createElement("div");
  chip.style.cssText = "font-size:12px;color:rgba(255,255,255,0.9);";
  chip.textContent = "🔒 已锁定为登录人";
  operatorSwitchEl.appendChild(chip);
}

function render() {
  renderHeader();
  renderOperatorSwitch();

  // v3.1: 工程师/主管 隐藏"问题"导航(只留 措施/逾期/我的)
  const isEngineer = LOGIN_USER.role === "engineer";
  if (tabIssuesEl) tabIssuesEl.style.display = isEngineer ? "none" : "";
  if (isEngineer && currentTab === "issues") currentTab = "measures";

  tabMeasuresEl.classList.toggle("active", currentTab === "measures");
  tabIssuesEl.classList.toggle("active", currentTab === "issues");
  tabOverdueEl.classList.toggle("active", currentTab === "overdue");
  tabMineEl.classList.toggle("active", currentTab === "mine");

  if (currentTab === "measures") renderMeasures();
  else if (currentTab === "issues") renderIssues();
  else if (currentTab === "overdue") renderOverdue();
  else if (currentTab === "mine") renderMine();
}

function switchTab(tab) {
  currentTab = tab;
  render();
  // 按需懒加载: 首次进入某 tab 才拉对应数据(工程师无"问题"tab, 永不加载 issues)
  if (tab === "issues" && !issuesLoaded) {
    loadIssues().then(() => { issuesLoaded = true; render(); });
  } else if (tab === "overdue" && !overdueLoaded) {
    loadOverdue().then(() => { enrichOverdueWithIssueNo(); overdueLoaded = true; render(); });
  }
}

function emptyTip(text, icon) {
  const tip = document.createElement("div");
  tip.className = "empty-tip";
  tip.innerHTML = '<span class="empty-icon">' + (icon || "📭") + "</span>" + text;
  return tip;
}

/* ---------- 措施列表(首页) ---------- */
function renderMeasures() {
  contentEl.innerHTML = "";
  if (loadError) {
    contentEl.appendChild(emptyTip("后端未连接, 请确认服务已启动", "🔌"));
    return;
  }

  // 顶部告警条
  const ov = apiData.stats.overdue || 0;
  const soon = apiData.stats.due_soon || 0;
  if (ov > 0 || soon > 0) {
    const alert = document.createElement("div");
    alert.className = "alert-bar";
    alert.innerHTML = "⚠️ <b>" + ov + "</b> 条逾期 · <b>" + soon + "</b> 条 3 天内到期";
    alert.onclick = () => switchTab("overdue");
    contentEl.appendChild(alert);
  }

  // 三角色不同结构(两层分配架构)
  const groups = apiData.groups || {};
  const role = LOGIN_USER.role;

  const renderLayer = (title, list) => {
    const cap = document.createElement("div");
    cap.className = "list-caption layer-caption";
    cap.textContent = title + " · " + list.length + " 条";
    contentEl.appendChild(cap);
    if (!list.length) {
      contentEl.appendChild(emptyTip("暂无", "📋"));
    } else {
      groupMeasuresByIssue(list).forEach((g) => {
        contentEl.appendChild(buildIssueHeader(g));
        g.measures.forEach((m) => contentEl.appendChild(buildMeasureCard(m)));
      });
    }
  };

  if (role === "admin") {
    // 管理员: 待分配(待评审+无部门) / 已分配(其余)
    const pending = groups["待评审"] || [];
    const unassigned = pending.filter((m) => !m.department);
    const assigned = [];
    MEASURE_STATUSES.forEach((s) => {
      (groups[s] || []).forEach((m) => {
        if (!(s === "待评审" && !m.department)) assigned.push(m);
      });
    });
    renderLayer("一、待分配措施", unassigned);
    renderLayer("二、已分配措施", assigned);
  } else if (role === "manager") {
    // 经理: 待评审 / 已采纳待分配 / 执行中 / 已完成待验收 / 已关闭
    renderLayer("一、待评审措施", groups["待评审"] || []);
    renderLayer("二、已采纳待分配", groups["已采纳"] || []);
    renderLayer("三、执行中措施", groups["执行中"] || []);
    renderLayer("四、已完成待验收", groups["已完成"] || []);
    renderLayer("五、已关闭措施", groups["已关闭"] || []);
  } else {
    // 工程师: 我的措施(owner=本人, 后端已过滤)
    renderLayer("一、待执行", groups["待执行"] || []);
    renderLayer("二、执行中", groups["执行中"] || []);
    renderLayer("三、已完成", groups["已完成"] || []);
  }

  const total = apiData.stats.total || 0;
  if (!total) {
    contentEl.appendChild(emptyTip("当前没有可见措施", "🎉"));
  }
}

/** 子序号: 问题序号.措施顺序(如 1.2)。fallbackOrder 用于无 measure_order 时兜底 */
function subNoOf(m, fallbackOrder) {
  const no = (m.issue_no != null) ? m.issue_no : (m.issue_id != null ? m.issue_id : "");
  const order = (m.measure_order != null) ? m.measure_order : (fallbackOrder != null ? fallbackOrder : 1);
  return no + "." + order;
}

/** 措施状态 → 颜色(逾期红 / 待评审橙黄 / 不采纳红 / 已完成绿 / 已关闭灰 / 其余蓝) */
function statusColorOf(m) {
  if (m.is_overdue) return "#e03131";
  const map = {
    "待评审": "#e8590c",
    "已采纳": "#7048e8",
    "不采纳": "#c92a2a",
    "待执行": "#3d5a80",
    "执行中": "#1971c2",
    "已完成": "#2f9e44",
    "已关闭": "#868e96",
  };
  return map[m.status] || "#3d5a80";
}

/** 乐观更新: 立即把某措施从旧状态分组移到新状态分组(带字段补丁), 并重渲染。
 *  用于"点击后立即改状态, 不等待接口返回"的体验。接口失败时再 loadAll 回滚。 */
function applyOptimistic(measureId, newStatus, patch) {
  const groups = apiData.groups || {};
  let target = null;
  MEASURE_STATUSES.forEach((s) => {
    const list = groups[s] || [];
    for (let i = 0; i < list.length; i++) {
      if (list[i].measure_id === measureId) {
        target = list.splice(i, 1)[0];
        break;
      }
    }
  });
  if (target) {
    if (patch) Object.assign(target, patch);
    if (newStatus) target.status = newStatus;
    (groups[target.status] = groups[target.status] || []).push(target);
  }
  render();
}

/** 把某状态下的一组措施按【问题】分组, 并给每条措施赋子序号(问题序号.措施顺序) */
function groupMeasuresByIssue(list) {
  const map = new Map();
  list.forEach((m) => {
    const no = (m.issue_no != null) ? m.issue_no : (m.issue_id != null ? m.issue_id : 0);
    if (!map.has(no)) {
      map.set(no, {
        issueNo: no,
        issueId: m.issue_id,
        problemDesc: m.problem_description || "",
        measures: []
      });
    }
    map.get(no).measures.push(m);
  });
  const arr = Array.from(map.values());
  arr.sort((a, b) => (Number(a.issueNo) || 0) - (Number(b.issueNo) || 0));
  arr.forEach((g) => {
    g.measures.forEach((m, i) => {
      m._subNo = subNoOf(m, i + 1);
    });
  });
  return arr;
}

/** 问题分组标题: 连续序号 + 完整问题描述(可点击进详情) */
function buildIssueHeader(g) {
  const header = document.createElement("div");
  header.className = "issue-group";
  header.innerHTML =
    '<div class="issue-group-title">问题 ' + escapeHtml(g.issueNo) + "</div>" +
    '<div class="issue-group-desc">' + escapeHtml(g.problemDesc || "(无描述)") + "</div>";
  header.onclick = () => openIssueDetail(g.issueId, g.issueNo);
  return header;
}

function buildMeasureCard(m) {
  const card = document.createElement("div");
  card.className = "issue-card";
  card.style.cursor = "pointer";

  // 逾期红色高亮
  if (m.is_overdue) card.style.borderLeft = "4px solid #e03131";

  // 第一行: 措施编号 + 状态标签
  const head = document.createElement("div");
  head.className = "issue-head";
  const statusColor = statusColorOf(m);
  const noLabel = (m._subNo != null) ? m._subNo : ("#" + m.measure_id);
  head.innerHTML =
    '<span class="issue-id">' + noLabel + "</span>" +
    '<span style="font-size:12px;font-weight:bold;color:' + statusColor + ';">' +
    (m.status || "") + (m.is_overdue ? " ⚠️" : "") + "</span>";
  card.appendChild(head);

  // 措施内容
  const content = document.createElement("div");
  content.className = "issue-desc";
  content.textContent = "🔧 " + (m.measure_content || "(无措施)");
  card.appendChild(content);

  // 责任人 + 部门 + 预计完成
  const meta = document.createElement("div");
  meta.className = "issue-station";
  meta.textContent = "👤 " + (m.owner || "未指定") +
    (m.department ? " · " + (DEPT_LABELS[m.department] || m.department) : "") +
    (m.due_date ? " · 📅 " + m.due_date : "");
  card.appendChild(meta);

  // 逾期天数提示
  if (m.is_overdue) {
    const ov = document.createElement("div");
    ov.style.cssText = "color:#e03131;font-size:12px;font-weight:bold;margin-top:2px;";
    ov.textContent = "⚠️ 已逾期 " + m.overdue_days + " 天";
    card.appendChild(ov);
  } else if (m.is_due_soon) {
    const soon = document.createElement("div");
    soon.style.cssText = "color:#e8590c;font-size:12px;margin-top:2px;";
    soon.textContent = "⏰ 即将到期";
    card.appendChild(soon);
  }

  // 操作按钮(按 角色 + 状态 + 是否已分配部门 动态生成)
  const acts = getActions(m);
  if (acts.length > 0) {
    const row = document.createElement("div");
    row.className = "btn-row";
    row.style.marginTop = "8px";
    acts.forEach((a) => {
      const b = document.createElement("button");
      b.className = "btn " + a.cls;
      b.textContent = a.label;
      b.onclick = (e) => { e.stopPropagation(); handleAction(m, a); };
      row.appendChild(b);
    });
    card.appendChild(row);
  }

  card.onclick = () => openHistory(m);
  return card;
}

/** 按 角色 + 措施状态 + 是否已分配部门, 生成可用操作按钮列表 */
function getActions(m) {
  const role = LOGIN_USER.role;
  const s = m.status;
  const hasDept = !!(m.department && String(m.department).trim() !== "");
  const acts = [];

  if (role === "admin") {
    // 管理员: 第一次分配(待评审 + 未分配部门)
    if (s === "待评审" && !hasDept) {
      acts.push({ label: "🏷️ 分配部门", cls: "btn-primary", act: "assign_dept" });
    }
  } else if (role === "manager") {
    if (s === "待评审") {
      acts.push({ label: "✅ 采纳", cls: "btn-pass", act: "adopt" });
      acts.push({ label: "❌ 不采纳", cls: "btn-reject", act: "reject" });
    } else if (s === "不采纳") {
      acts.push({ label: "🔁 重新采纳", cls: "btn-primary", act: "adopt" });
    } else if (s === "已采纳") {
      acts.push({ label: "👤 分配工程师", cls: "btn-primary", act: "assign_person" });
    } else if (s === "已完成") {
      acts.push({ label: "🔒 验收关闭", cls: "btn-pass", act: "status", to: "已关闭" });
      acts.push({ label: "↩️ 退回返工", cls: "btn-reject", act: "status", to: "执行中" });
    } else if (s === "已关闭") {
      acts.push({ label: "↩️ 重开", cls: "btn-reject", act: "status", to: "执行中" });
    }
  } else {
    // 工程师: 我的措施(owner=本人, 后端已过滤), 不能验收关闭
    if (s === "待执行") {
      acts.push({ label: "▶️ 开始执行", cls: "btn-primary", act: "status", to: "执行中" });
    } else if (s === "执行中") {
      acts.push({ label: "✅ 标记完成", cls: "btn-pass", act: "status", to: "已完成" });
    }
  }
  return acts;
}

/** 分发操作: 采纳/不采纳/分配部门/更新状态 */
function handleAction(m, a) {
  if (a.act === "adopt") {
    // 采纳二次确认
    if (!window.confirm("确认采纳措施 " + subNoOf(m) + "？")) return;
    confirmAdopt(m, true);
  } else if (a.act === "reject") {
    confirmAdopt(m, false);   // confirmAdopt 内部已有"不采纳"二次确认
  } else if (a.act === "assign_dept") {
    openAssignDeptModal(m);      // 管理员第一次分配: 选部门
  } else if (a.act === "assign_person") {
    openAssignPersonModal(m);    // 经理第二次分配: 选工程师
  } else if (a.act === "status") {
    // 验收关闭是关键动作, 二次确认
    if (a.to === "已关闭" && !window.confirm("确认验收关闭措施 " + subNoOf(m) + "？")) return;
    openStatusModal(m, a.to);
  }
}

/** 采纳 / 不采纳(经理) */
async function confirmAdopt(m, adopt) {
  if (!adopt && !window.confirm("确认不采纳措施 " + subNoOf(m) + "？(可重新采纳)")) return;
  const operator = LOGIN_USER.display_name || LOGIN_USER.username;
  const newStatus = adopt ? "已采纳" : "不采纳";
  // 乐观更新: 点击立即移动措施, 不等接口返回
  applyOptimistic(m.measure_id, newStatus, null);
  try {
    const resp = await fetchWithAuth(API_BASE + "/api/measure_adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        measure_id: m.measure_id,
        decision: adopt ? "adopt" : "reject",
        reviewer: operator,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.success) {
      await loadAll();   // 失败回滚到真实数据
      showToast("⚠️ " + (data.message || "操作失败"));
      return;
    }
    showToast("✅ " + data.message);
    await loadAll();
    // 若当前在问题详情弹窗内操作, 原地刷新, 让措施在两层间可见地移动
    if (typeof window.__qcsOnActionDone === "function") window.__qcsOnActionDone();
  } catch (e) {
    await loadAll();   // 回滚
    showToast("⚠️ 操作失败：后端未连接");
  }
}

/** 加载选人名单(按部门工程师, 供经理二次分配选人) */
async function loadPersons() {
  try {
    const resp = await fetchWithAuth(API_BASE + "/api/persons");
    if (!resp.ok) return;
    const data = await resp.json();
    if (data) {
      personsData = {
        all: data.all || [],
        departments: data.departments || [],
        engineers: data.engineers || {},
        managers: data.managers || {},
      };
    }
  } catch (e) { /* 忽略, 二次分配弹窗用输入框兜底 */ }
}

/** 第一次分配弹窗(管理员): 选【部门经理】—— 不能越级分给工程师 */
function openAssignDeptModal(m) {
  const mask = document.createElement("div");
  mask.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.45);display:flex;align-items:center;justify-content:center;z-index:400;padding:20px;";
  const box = document.createElement("div");
  box.style.cssText = "width:340px;max-width:100%;background:#fff;border-radius:16px;padding:20px 18px 16px;box-shadow:0 10px 40px rgba(0,0,0,0.25);";
  mask.appendChild(box);
  document.body.appendChild(mask);

  // 候选人: 只显示 role=manager 的账号(按部门分组), 不含工程师
  const managers = personsData.managers || {};
  const mgrList = [];
  Object.keys(managers).forEach((d) => {
    (managers[d] || []).forEach((n) => mgrList.push({ dept: d, name: n }));
  });

  const field = mgrList.length
    ? '<label style="font-size:12px;color:#495057;">部门经理</label>' +
      '<select id="assignMgr" class="modal-input" style="margin-bottom:10px;">' +
      mgrList.map((x, i) =>
        '<option value="' + i + '">' + escapeHtml(DEPT_LABELS[x.dept] || x.dept) +
        ' ｜ ' + escapeHtml(x.name) + "</option>").join("") +
      '</select>'
    : '<div style="color:#c92a2a;font-size:12px;margin-bottom:10px;line-height:1.6;">' +
      '暂无可分配的部门经理。请先在电脑端「账号管理」创建<b>角色=经理</b>、' +
      '部门为 P/Q/T 的账号。</div>';

  box.innerHTML =
    '<div style="font-size:17px;font-weight:bold;margin-bottom:6px;">🏷️ 分配给部门经理（第一次）</div>' +
    '<div class="modal-issue">措施 ' + subNoOf(m) + ' · ' +
    escapeHtml(m.measure_content || "") + '</div>' +
    field +
    '<div id="assignErr" class="modal-error"></div>' +
    '<div class="modal-btns">' +
    '<button class="btn-modal-cancel" id="assignCancel">取消</button>' +
    '<button class="btn-modal-ok" id="assignOk"' +
    (mgrList.length ? "" : " disabled style='opacity:.5;'") + '>确认分配</button>' +
    '</div>';

  document.getElementById("assignCancel").onclick = () => mask.remove();
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });
  document.getElementById("assignOk").onclick = async () => {
    if (!mgrList.length) return;
    const idx = parseInt(document.getElementById("assignMgr").value, 10);
    const mgr = mgrList[idx];
    const operator = LOGIN_USER.display_name || LOGIN_USER.username;
    try {
      const resp = await fetchWithAuth(API_BASE + "/api/measure_assign_dept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          measure_id: m.measure_id, department: mgr.dept,
          manager_name: mgr.name, operator: operator,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        const errEl = document.getElementById("assignErr");
        errEl.textContent = data.message || "分配失败";
        errEl.classList.add("show");
        return;
      }
      mask.remove();
      showToast("✅ " + data.message);
      await loadAll();
      if (typeof window.__qcsOnActionDone === "function") window.__qcsOnActionDone();
    } catch (e) {
      const errEl = document.getElementById("assignErr");
      errEl.textContent = "分配失败：后端未连接";
      errEl.classList.add("show");
    }
  };
}

/** 第二次分配弹窗(经理): 选本部门工程师 + 预计完成时间 */
function openAssignPersonModal(m) {
  const mask = document.createElement("div");
  mask.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.45);display:flex;align-items:center;justify-content:center;z-index:400;padding:20px;";
  const box = document.createElement("div");
  box.style.cssText = "width:340px;max-width:100%;background:#fff;border-radius:16px;padding:20px 18px 16px;box-shadow:0 10px 40px rgba(0,0,0,0.25);";
  mask.appendChild(box);
  document.body.appendChild(mask);

  // 本部门工程师名单(自动完成 + 实时校验)
  const dept = m.department || LOGIN_USER.department || "";
  const engList = (personsData.engineers || {})[dept] || [];

  box.innerHTML =
    '<div style="font-size:17px;font-weight:bold;margin-bottom:6px;">👤 分配工程师（第二次）</div>' +
    '<div class="modal-issue">措施 ' + subNoOf(m) + ' · ' +
    escapeHtml(m.measure_content || "") + '</div>' +
    '<label style="font-size:12px;color:#495057;">工程师姓名（本部门，可搜索/输入）</label>' +
    '<div id="assignOwnerWrap" style="position:relative;margin-bottom:4px;"></div>' +
    '<div id="assignHint" style="font-size:11px;color:#868e96;margin-bottom:8px;min-height:14px;"></div>' +
    '<label style="font-size:12px;color:#495057;">预计完成</label>' +
    '<input id="assignDue" class="modal-input" type="date" style="margin-bottom:10px;" />' +
    '<div id="assignErr" class="modal-error"></div>' +
    '<div class="modal-btns">' +
    '<button class="btn-modal-cancel" id="assignCancel">取消</button>' +
    '<button class="btn-modal-ok" id="assignOk">确认分配</button>' +
    '</div>';

  document.getElementById("assignCancel").onclick = () => mask.remove();
  mask.addEventListener("click", (e) => { if (e.target === mask) mask.remove(); });

  const hintEl = document.getElementById("assignHint");

  // ---- 自定义可搜索下拉(替代 datalist, iOS/Android 体验一致) ----
  const wrap = document.getElementById("assignOwnerWrap");
  const ownerInput = document.createElement("input");
  ownerInput.className = "modal-input";
  ownerInput.type = "text";
  ownerInput.placeholder = "搜索或输入工程师姓名";
  ownerInput.autocomplete = "off";
  ownerInput.style.marginBottom = "0";
  wrap.appendChild(ownerInput);

  const listEl = document.createElement("div");
  listEl.style.cssText = "display:none;position:absolute;top:100%;left:0;right:0;" +
    "background:#fff;border:1px solid #dee2e6;border-radius:8px;max-height:180px;" +
    "overflow-y:auto;z-index:20;box-shadow:0 4px 12px rgba(0,0,0,0.12);";
  wrap.appendChild(listEl);

  const validateOwner = () => {
    const v = ownerInput.value.trim();
    if (!v) { hintEl.textContent = ""; hintEl.style.color = "#868e96"; return; }
    if (engList.indexOf(v) >= 0) {
      hintEl.textContent = "✓ " + v + "（" + (DEPT_LABELS[dept] || dept) + " 工程师）";
      hintEl.style.color = "#2f9e44";
    } else {
      hintEl.textContent = "✗ 未找到本部门工程师「" + v + "」，无法分配";
      hintEl.style.color = "#c92a2a";
    }
  };

  const renderList = (kw) => {
    const keyword = (kw || "").trim();
    const matched = engList.filter((n) => n.indexOf(keyword) >= 0);
    listEl.innerHTML = "";
    if (!matched.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "padding:8px 12px;color:#868e96;font-size:13px;";
      empty.textContent = "无匹配工程师";
      listEl.appendChild(empty);
    } else {
      matched.forEach((n) => {
        const opt = document.createElement("div");
        opt.style.cssText = "padding:9px 12px;font-size:14px;cursor:pointer;border-bottom:1px solid #f1f3f5;";
        opt.textContent = n;
        opt.addEventListener("mousedown", (e) => e.preventDefault());  // 防止点击时失焦
        opt.addEventListener("click", () => {
          ownerInput.value = n;
          listEl.style.display = "none";
          validateOwner();
        });
        opt.addEventListener("mouseenter", () => { opt.style.background = "#f1f3f5"; });
        opt.addEventListener("mouseleave", () => { opt.style.background = ""; });
        listEl.appendChild(opt);
      });
    }
    listEl.style.display = "block";
  };

  ownerInput.addEventListener("focus", () => renderList(ownerInput.value));
  ownerInput.addEventListener("input", () => { renderList(ownerInput.value); validateOwner(); });
  // 点击弹窗其他区域时收起下拉
  document.addEventListener("click", function closeList(e) {
    if (!wrap.contains(e.target)) listEl.style.display = "none";
  });

  document.getElementById("assignOk").onclick = async () => {
    const owner = ownerInput.value.trim();
    const due = document.getElementById("assignDue").value;
    const errEl = document.getElementById("assignErr");
    if (!owner) { errEl.textContent = "请填写工程师姓名"; errEl.classList.add("show"); return; }
    if (!due) { errEl.textContent = "请选择预计完成时间"; errEl.classList.add("show"); return; }
    // 前端先校验(后端会二次校验, 双重保险)
    if (engList.indexOf(owner) < 0) {
      errEl.textContent = "「" + owner + "」不是本部门工程师，无法分配";
      errEl.classList.add("show");
      return;
    }
    const operator = LOGIN_USER.display_name || LOGIN_USER.username;
    try {
      const resp = await fetchWithAuth(API_BASE + "/api/measure_assign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ measure_id: m.measure_id, owner: owner, due_date: due, operator: operator }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        errEl.textContent = data.message || "分配失败";
        errEl.classList.add("show");
        return;
      }
      mask.remove();
      showToast("✅ " + data.message);
      await loadAll();
      if (typeof window.__qcsOnActionDone === "function") window.__qcsOnActionDone();
    } catch (e) {
      errEl.textContent = "分配失败：后端未连接";
      errEl.classList.add("show");
    }
  };
}

/* ---------- 问题台账 ---------- */
function renderIssues() {
  contentEl.innerHTML = "";
  if (!issuesData.length) {
    // 经理: 只有管理员把措施分配给自己后, 对应问题才会出现
    contentEl.appendChild(emptyTip("暂无分配给你的问题（管理员分配措施后才会出现）", "📋"));
    return;
  }
  const cap = document.createElement("div");
  cap.className = "list-caption";
  cap.textContent = "问题台账 · 共 " + issuesData.length + " 个";
  contentEl.appendChild(cap);
  issuesData.forEach((iss) => contentEl.appendChild(buildIssueCard(iss)));
}

function buildIssueCard(iss) {
  const card = document.createElement("div");
  card.className = "issue-card";

  const head = document.createElement("div");
  head.className = "issue-head";
  const stColor = iss.status === "已关闭" ? "#2f9e44" : (iss.status === "未开始" ? "#868e96" : "#3d5a80");
  head.innerHTML =
    '<span class="issue-id">问题 #' + iss.issue_no + "</span>" +
    '<span style="font-size:12px;font-weight:bold;color:' + stColor + ';">' + iss.status + "</span>";
  card.appendChild(head);

  const desc = document.createElement("div");
  desc.className = "issue-desc";
  desc.textContent = (iss.problem_description || "(无描述)");
  card.appendChild(desc);

  const meta = document.createElement("div");
  meta.className = "issue-station";
  meta.textContent = (iss.department ? (DEPT_LABELS[iss.department] || iss.department) + " · " : "") +
    (iss.work_station || "未填工位") +
    (iss.issue_date ? " · " + iss.issue_date : "");
  card.appendChild(meta);

  // 措施进度
  const progress = document.createElement("div");
  progress.style.cssText = "font-size:12px;color:#6c757d;margin-top:4px;";
  progress.textContent = "措施进度: " + (iss.measure_closed || 0) + "/" + (iss.measure_total || 0) + " 已关闭";
  card.appendChild(progress);

  // 点击问题 → 打开问题详情(措施按 待评审/已评审 两层展示, 可直接操作)
  card.style.cursor = "pointer";
  card.onclick = () => openIssueDetail(iss.issue_id, iss.issue_no);

  return card;
}

/* ---------- 逾期 ---------- */
function renderOverdue() {
  contentEl.innerHTML = "";

  // 已逾期
  const cap1 = document.createElement("div");
  cap1.className = "list-caption";
  cap1.textContent = "🔴 已逾期 · " + (overdueData.overdue || []).length + " 条";
  contentEl.appendChild(cap1);
  if (!overdueData.overdue || !overdueData.overdue.length) {
    contentEl.appendChild(emptyTip("暂无逾期措施", "🎉"));
  } else {
    overdueData.overdue.forEach((m) => {
      const card = document.createElement("div");
      card.className = "issue-card";
      card.style.borderLeft = "4px solid #e03131";
      card.innerHTML =
        '<div class="issue-head"><span class="issue-id">' + subNoOf(m) + "</span>" +
        '<span style="font-size:12px;font-weight:bold;color:#e03131;">逾期 ' + m.overdue_days + " 天</span></div>" +
        '<div class="issue-desc">🔧 ' + escapeHtml(m.measure_content || "") + "</div>" +
        '<div class="issue-station">👤 ' + escapeHtml(m.owner || "未指定") +
        " · " + (DEPT_LABELS[m.department] || m.department || "") +
        " · 📅 " + (m.due_date || "未填") + "</div>" +
        '<div class="issue-station">📋 ' + escapeHtml(m.problem_description || "") + "</div>";
      card.style.cursor = "pointer";
      card.onclick = () => openStatusModalFromId(m);
      contentEl.appendChild(card);
    });
  }

  // 即将到期
  const cap2 = document.createElement("div");
  cap2.className = "list-caption";
  cap2.style.marginTop = "16px";
  cap2.textContent = "🟠 即将到期(3天内) · " + (overdueData.due_soon || []).length + " 条";
  contentEl.appendChild(cap2);
  if (!overdueData.due_soon || !overdueData.due_soon.length) {
    contentEl.appendChild(emptyTip("暂无即将到期措施", "✅"));
  } else {
    overdueData.due_soon.forEach((m) => {
      const card = document.createElement("div");
      card.className = "issue-card";
      card.style.borderLeft = "4px solid #e8590c";
      card.innerHTML =
        '<div class="issue-head"><span class="issue-id">' + subNoOf(m) + "</span>" +
        '<span style="font-size:12px;font-weight:bold;color:#e8590c;">' +
        (m.days_left === 0 ? "今日到期" : ("剩 " + m.days_left + " 天")) + "</span></div>" +
        '<div class="issue-desc">🔧 ' + escapeHtml(m.measure_content || "") + "</div>" +
        '<div class="issue-station">👤 ' + escapeHtml(m.owner || "未指定") +
        " · " + (DEPT_LABELS[m.department] || m.department || "") +
        " · 📅 " + (m.due_date || "未填") + "</div>";
      card.style.cursor = "pointer";
      card.onclick = () => openStatusModalFromId(m);
      contentEl.appendChild(card);
    });
  }
}

/* ---------- 我的 ---------- */
function renderMine() {
  contentEl.innerHTML = "";

  const info = document.createElement("div");
  info.className = "issue-card";
  info.innerHTML =
    '<div style="font-size:16px;font-weight:bold;margin-bottom:8px;">' +
    escapeHtml(LOGIN_USER.display_name || LOGIN_USER.username || "?") + "</div>" +
    '<div class="issue-station">账号: ' + escapeHtml(LOGIN_USER.username || "") + "</div>" +
    '<div class="issue-station">角色: ' + (ROLE_LABELS[LOGIN_USER.role] || LOGIN_USER.role) + "</div>" +
    '<div class="issue-station">部门: ' +
    (LOGIN_USER.department ? (DEPT_LABELS[LOGIN_USER.department] || LOGIN_USER.department) : "全部") + "</div>";
  contentEl.appendChild(info);

  // 我的措施统计(6 状态)
  const st = apiData.stats || {};
  const by = st.by_status || {};
  const stat = document.createElement("div");
  stat.className = "issue-card";
  stat.innerHTML =
    '<div style="font-weight:bold;margin-bottom:8px;">📊 我的措施统计</div>' +
    '<div class="issue-station">总数: ' + (st.total || 0) + " 条</div>" +
    MEASURE_STATUSES.map(function (s) {
      return '<div class="issue-station">' + statusLabel(s) + ": " + (by[s] || 0) + "</div>";
    }).join("") +
    '<div class="issue-station" style="color:#e03131;font-weight:bold;">⚠️ 逾期: ' +
    (st.overdue || 0) + " 条 · 即将到期: " + (st.due_soon || 0) + " 条</div>";
  contentEl.appendChild(stat);

  // 经理额外显示部门维度逾期对比(简化看板, 逾期标红)
  if (LOGIN_USER.role === "manager") {
    contentEl.appendChild(buildDashboardCard());
  }

  // 退出按钮
  const logoutBtn = document.createElement("button");
  logoutBtn.className = "btn btn-reject";
  logoutBtn.style.marginTop = "12px";
  logoutBtn.textContent = "🚪 退出登录";
  logoutBtn.onclick = doLogout;
  contentEl.appendChild(logoutBtn);
}

/** 经理简化看板: 部门维度逾期对比(CSS 条形图, 逾期标红) */
function buildDashboardCard() {
  const card = document.createElement("div");
  card.className = "issue-card";
  card.innerHTML = '<div style="font-weight:bold;margin-bottom:10px;">📈 部门逾期对比</div>';

  const od = overdueData.stats || {};
  const byDept = od.by_department || {};
  let maxVal = 1;
  ["P", "Q", "T"].forEach(function (d) { if ((byDept[d] || 0) > maxVal) maxVal = byDept[d]; });

  ["P", "Q", "T"].forEach(function (d) {
    const v = byDept[d] || 0;
    const w = Math.max(v > 0 ? Math.round(v / maxVal * 100) : 0, v > 0 ? 4 : 0);
    const row = document.createElement("div");
    row.style.cssText = "margin-bottom:8px;";
    row.innerHTML =
      '<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px;">' +
      '<span>' + (DEPT_LABELS[d] || d) + "</span>" +
      '<span style="color:#e03131;font-weight:bold;">' + v + " 条逾期</span></div>" +
      '<div style="height:8px;background:#f1f3f5;border-radius:4px;overflow:hidden;">' +
      '<div style="height:8px;width:' + w + '%;background:#e03131;border-radius:4px;"></div></div>';
    card.appendChild(row);
  });

  const summary = document.createElement("div");
  summary.style.cssText = "font-size:12px;color:#6c757d;margin-top:8px;";
  summary.textContent = "总计逾期 " + (od.overdue || 0) + " 条 · 即将到期 " + (od.due_soon || 0) + " 条";
  card.appendChild(summary);
  return card;
}

// ============================================================
// 七、状态更新弹窗
// ============================================================

/** 打开状态更新弹窗(m=措施对象, preset=预选目标状态) */
function openStatusModal(m, preset) {
  statusContext = { measure: m };

  statusTitleEl.textContent = "更新措施状态";
  statusIssueEl.textContent =
    subNoOf(m) + " · " + (m.work_station || "未填工位") +
    " · " + (m.measure_content || "");

  // 目标状态: 由调用方 preset 指定(单个)。采纳/分配走独立弹窗。
  const nexts = preset ? [preset] : [];
  statusSelectEl.innerHTML = "";
  nexts.forEach((ns) => {
    const opt = document.createElement("option");
    opt.value = ns;
    opt.textContent = "目标状态: " + ns;
    statusSelectEl.appendChild(opt);
  });
  statusSelectEl.disabled = true;   // 单一目标状态, 不允许改选
  if (preset) statusSelectEl.value = preset;

  statusCommentEl.value = "";
  statusCommentEl.placeholder =
    (preset === "已完成" || preset === "已关闭")
      ? "备注(完成说明 / 验证结论)"
      : "备注(退回/重开原因, 必填)";

  _resetPhoto();
  // 照片: 仅在推进到终态(已完成/已关闭)时可选项; 退回/重开不需要
  const target = statusSelectEl.value;
  const showPhoto = (target === "已完成" || target === "已关闭");
  photoPlaceholderEl.className = "photo-placeholder" + (showPhoto ? " show" : "");
  if (showPhoto) {
    photoHintBoxEl.style.display = "flex";
    photoPreviewEl.style.display = "none";
    photoTextEl.textContent = "📷 添加完成证据照片(可选)";
  }

  statusSelectEl.onchange = () => {
    const t = statusSelectEl.value;
    photoPlaceholderEl.className = "photo-placeholder" + ((t === "已完成" || t === "已关闭") ? " show" : "");
    if (t === "已完成" || t === "已关闭") {
      photoHintBoxEl.style.display = "flex";
      photoPreviewEl.style.display = "none";
    }
  };

  statusErrorEl.classList.remove("show");
  statusErrorEl.textContent = "";
  statusMaskEl.classList.add("show");
}

function openStatusModalFromId(m) {
  // 逾期列表里的措施对象字段名与 tasks 一致, 直接复用
  openStatusModal(m, null);
}

function closeStatusModal() {
  statusMaskEl.classList.remove("show");
  statusContext = null;
  _resetPhoto();
}

statusMaskEl.addEventListener("click", (e) => {
  if (e.target === statusMaskEl) closeStatusModal();
});

function _resetPhoto() {
  photoBlob = null;
  if (photoInputEl) photoInputEl.value = "";
  if (photoObjectUrl) {
    URL.revokeObjectURL(photoObjectUrl);
    photoObjectUrl = null;
  }
  if (photoPreviewEl) photoPreviewEl.style.display = "none";
  if (photoHintBoxEl) photoHintBoxEl.style.display = "flex";
}

function triggerPhotoPick() {
  if (!photoInputEl) return;
  photoInputEl.click();
}

photoInputEl.addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  try {
    photoBlob = await compressPhotoFile(file, 1600, 0.8);
  } catch (err) {
    console.error("照片压缩失败:", err);
    statusErrorEl.textContent = "⚠️ 照片处理失败(请换一张图片重试)";
    statusErrorEl.classList.add("show");
    return;
  }
  if (!photoBlob) {
    statusErrorEl.textContent = "⚠️ 无法读取该图片(请换一张)";
    statusErrorEl.classList.add("show");
    return;
  }
  if (photoObjectUrl) URL.revokeObjectURL(photoObjectUrl);
  photoObjectUrl = URL.createObjectURL(photoBlob);
  photoPreviewEl.src = photoObjectUrl;
  photoPreviewEl.style.display = "block";
  photoHintBoxEl.style.display = "none";
});

/** 前端压缩照片: Canvas 按最长边缩放, 转 JPEG(质量0.8), 返回 Blob */
function compressPhotoFile(file, maxSide, quality) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("图片解码失败"));
      img.onload = () => {
        let { width, height } = img;
        if (Math.max(width, height) > maxSide) {
          const scale = maxSide / Math.max(width, height);
          width = Math.round(width * scale);
          height = Math.round(height * scale);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, width, height);
        canvas.toBlob(
          (blob) => {
            if (!blob) reject(new Error("压缩失败"));
            else resolve(blob);
          },
          "image/jpeg",
          quality
        );
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

/** 弹窗确认: 提交状态更新 */
async function confirmStatusUpdate() {
  if (!statusContext) return;
  const m = statusContext.measure;
  const newStatus = statusSelectEl.value;
  const comment = (statusCommentEl.value || "").trim();

  // 校验: 标记完成/验证关闭/退回重开 都必须填备注
  const isReopen = (m.status === "已完成" || m.status === "已关闭") && newStatus === "执行中";
  const needComment = newStatus === "已完成" || newStatus === "已关闭" || isReopen;
  if (needComment && !comment) {
    let hint = "完成说明";
    if (newStatus === "已关闭") hint = "验证结论";
    else if (isReopen) hint = "退回/重开原因";
    statusErrorEl.textContent = "⚠️ 请填写" + hint;
    statusErrorEl.classList.add("show");
    return;
  }

  statusOkBtnEl.disabled = true;
  statusOkBtnEl.textContent = "提交中...";

  // 可选照片: 仅在推进到终态时上传(证据)
  let photoPath = "";
  if (photoBlob && (newStatus === "已完成" || newStatus === "已关闭")) {
    try {
      const fd = new FormData();
      fd.append("file", photoBlob, "evidence.jpg");
      const upResp = await fetchWithAuth(API_BASE + "/api/upload_review_photo", {
        method: "POST",
        body: fd,
      });
      const upData = await upResp.json().catch(() => ({}));
      if (!upResp.ok || !upData.success) {
        statusErrorEl.textContent = "⚠️ 照片上传失败：" + (upData.message || "HTTP " + upResp.status);
        statusErrorEl.classList.add("show");
        statusOkBtnEl.disabled = false;
        statusOkBtnEl.textContent = "确认";
        return;
      }
      photoPath = upData.photo_path || "";
    } catch (e) {
      statusErrorEl.textContent = "⚠️ 照片上传失败：后端服务未启动或接口不可用";
      statusErrorEl.classList.add("show");
      statusOkBtnEl.disabled = false;
      statusOkBtnEl.textContent = "确认";
      return;
    }
  }

  // 乐观更新: 立即关闭弹窗 + 更新状态, 不等状态接口返回
  closeStatusModal();
  applyOptimistic(m.measure_id, newStatus, null);
  showToast("✅ 已提交「" + newStatus + "」");

  try {
    const resp = await fetchWithAuth(API_BASE + "/api/measure_status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        measure_id: m.measure_id,
        new_status: newStatus,
        operator: LOGIN_USER.display_name || LOGIN_USER.username,
        comment: comment,
        photo_path: photoPath,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.success) {
      await loadAll();   // 失败回滚到真实数据
      showToast("⚠️ " + (data.message || "更新失败"));
      return;
    }
    showToast("✅ " + (data.message || (subNoOf(m) + " 已更新为「" + newStatus + "」")));
    await loadAll();
    // 若当前在问题详情弹窗内操作, 原地刷新
    if (typeof window.__qcsOnActionDone === "function") window.__qcsOnActionDone();
  } catch (e) {
    await loadAll();   // 回滚
    showToast("⚠️ 更新失败：后端服务未启动或接口不可用");
  } finally {
    statusOkBtnEl.disabled = false;
    statusOkBtnEl.textContent = "确认";
  }
}

// ============================================================
// 七、问题详情(点击问题标题进入, 只显示本部门措施)
// ============================================================

function openIssueDetail(issueId, issueNo) {
  const mask = document.createElement("div");
  mask.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.45);display:flex;align-items:center;justify-content:center;z-index:400;padding:20px;";
  const box = document.createElement("div");
  box.style.cssText = "width:350px;max-width:100%;background:#fff;border-radius:16px;padding:20px 18px 16px;box-shadow:0 10px 40px rgba(0,0,0,0.25);max-height:80vh;overflow-y:auto;";
  mask.appendChild(box);
  document.body.appendChild(mask);

  // 子弹窗(分配部门/更新状态/采纳)操作成功后, 原地刷新本弹窗,
  // 让措施在"待评审 → 已评审"两层之间可见地移动, 而不是消失
  window.__qcsOnActionDone = function () {
    renderIssueDetailBody(box, issueId, issueNo);
  };
  mask.addEventListener("click", (e) => {
    if (e.target === mask) { window.__qcsOnActionDone = null; mask.remove(); }
  });

  renderIssueDetailBody(box, issueId, issueNo);
}

/** 渲染问题详情弹窗主体(可重复调用以原地刷新) */
function renderIssueDetailBody(box, issueId, issueNo) {
  // 汇总该问题下【本部门可见】的措施(apiData 已按部门过滤)
  const measures = [];
  const groups = apiData.groups || {};
  MEASURE_STATUSES.forEach((s) => {
    (groups[s] || []).forEach((m) => {
      if (String(m.issue_id) === String(issueId)) measures.push(m);
    });
  });

  // 从问题台账里取问题信息
  let issue = null;
  (issuesData || []).forEach((i) => { if (String(i.issue_id) === String(issueId)) issue = i; });

  box.innerHTML = "";

  const head = document.createElement("div");
  head.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;";
  const title = document.createElement("div");
  title.style.cssText = "font-size:17px;font-weight:bold;";
  title.textContent = "问题详情" + (issueNo ? " #" + issueNo : "");
  const closeBtn = document.createElement("button");
  closeBtn.textContent = "✕";
  closeBtn.style.cssText = "border:none;background:none;font-size:20px;cursor:pointer;color:#868e96;";
  closeBtn.onclick = () => {
    window.__qcsOnActionDone = null;
    if (box.parentNode) box.parentNode.remove();
  };
  head.appendChild(title);
  head.appendChild(closeBtn);
  box.appendChild(head);

  // 问题完整描述(不截断, 换行显示)
  const desc = document.createElement("div");
  desc.style.cssText = "color:#212529;font-size:14px;line-height:1.6;margin-bottom:10px;white-space:normal;word-break:break-word;";
  desc.textContent = (issue && issue.problem_description) ||
    (measures[0] && measures[0].problem_description) || "(无描述)";
  box.appendChild(desc);

  if (issue) {
    // 问题基本信息: 部门 / 录入人 / 问题来源 / 创建日期 / 工位(缺值统一显示"未填")
    const meta = document.createElement("div");
    meta.style.cssText = "color:#6c757d;font-size:12px;margin-bottom:12px;line-height:1.8;";
    const parts = [
      "部门: " + (issue.department ? (DEPT_LABELS[issue.department] || issue.department) : "未分配"),
      "来源: " + (issue.issue_source || "未填"),
      "创建日期: " + (issue.issue_date || "未填"),
      "工位: " + (issue.work_station || "未填"),
    ];
    meta.textContent = parts.join(" ｜ ");
    box.appendChild(meta);
  }

  if (!measures.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "color:#868e96;text-align:center;padding:12px 0;";
    empty.textContent = "该问题下暂无本部门措施";
    box.appendChild(empty);
    return;
  }

  // ---- 第一层: 待评审措施 ----
  const pending = measures.filter((m) => m.status === "待评审");
  box.appendChild(buildLayerTitle("一、待评审措施", pending.length));
  if (!pending.length) {
    box.appendChild(buildLayerEmpty("暂无待评审措施"));
  } else {
    pending.forEach((m) => box.appendChild(buildMeasureRow(m)));
  }

  // ---- 第二层: 已评审措施(采纳/不采纳后落到这里, 不再消失) ----
  const reviewed = measures.filter((m) => m.status !== "待评审");
  box.appendChild(buildLayerTitle("二、已评审措施", reviewed.length));
  if (!reviewed.length) {
    box.appendChild(buildLayerEmpty("暂无已评审措施"));
  } else {
    reviewed.forEach((m) => box.appendChild(buildMeasureRow(m)));
  }
}

function buildLayerTitle(text, count) {
  const el = document.createElement("div");
  el.className = "list-caption layer-caption";
  el.style.marginTop = "10px";
  el.textContent = text + " · " + count + " 条";
  return el;
}

function buildLayerEmpty(text) {
  const el = document.createElement("div");
  el.style.cssText = "color:#868e96;font-size:12px;padding:6px 0 10px;";
  el.textContent = text;
  return el;
}

/** 弹窗内的措施行: 内容 + 状态标签 + 操作按钮(与列表卡片保持一致的按钮逻辑) */
function buildMeasureRow(m) {
  const row = document.createElement("div");
  row.className = "issue-card";
  row.style.marginBottom = "8px";
  if (m.is_overdue) row.style.borderLeft = "4px solid #e03131";

  const stColor = statusColorOf(m);
  const head = document.createElement("div");
  head.className = "issue-head";
  head.innerHTML =
    '<span class="issue-id">' + subNoOf(m) + "</span>" +
    '<span style="font-size:12px;font-weight:bold;color:' + stColor + ';">' +
    (m.status || "") + (m.is_overdue ? " ⚠️" : "") + "</span>";
  row.appendChild(head);

  const content = document.createElement("div");
  content.className = "issue-desc";
  content.textContent = "🔧 " + (m.measure_content || "(无措施)");
  row.appendChild(content);

  // 二次分配情况 + 预计完成(工程师执行进度看状态标签)
  const meta = document.createElement("div");
  meta.className = "issue-station";
  meta.textContent = "二次分配给: " + (m.owner || "未分配") +
    (m.department ? " · " + (DEPT_LABELS[m.department] || m.department) : "") +
    (m.due_date ? " · 📅 " + m.due_date : "");
  row.appendChild(meta);

  if (m.is_overdue) {
    const ov = document.createElement("div");
    ov.style.cssText = "color:#e03131;font-size:12px;font-weight:bold;margin-top:2px;";
    ov.textContent = "⚠️ 已逾期 " + m.overdue_days + " 天";
    row.appendChild(ov);
  }

  const acts = getActions(m);
  if (acts.length > 0) {
    const brow = document.createElement("div");
    brow.className = "btn-row";
    brow.style.marginTop = "8px";
    acts.forEach((a) => {
      const b = document.createElement("button");
      b.className = "btn " + a.cls;
      b.textContent = a.label;
      b.onclick = (e) => { e.stopPropagation(); handleAction(m, a); };
      brow.appendChild(b);
    });
    row.appendChild(brow);
  }
  return row;
}

// ============================================================
// 八、措施状态变更历史(点击卡片查看)
// ============================================================

async function openHistory(m) {
  // 遮罩: 措施详情(完整问题描述 + 措施信息 + 操作按钮 + 状态历史)
  const mask = document.createElement("div");
  mask.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,0.45);display:flex;align-items:center;" +
    "justify-content:center;z-index:400;padding:20px;";
  const box = document.createElement("div");
  box.style.cssText =
    "width:350px;max-width:100%;background:#fff;border-radius:16px;padding:20px 18px 16px;" +
    "box-shadow:0 10px 40px rgba(0,0,0,0.25);max-height:80vh;overflow-y:auto;";
  mask.appendChild(box);
  document.body.appendChild(mask);

  // 在本弹窗内操作(采纳/分配部门/更新状态)成功后, 重开弹窗显示最新状态
  window.__qcsOnActionDone = function () {
    window.__qcsOnActionDone = null;
    mask.remove();
    let latest = m;
    const groups = apiData.groups || {};
    MEASURE_STATUSES.forEach((s) => {
      (groups[s] || []).forEach((x) => {
        if (x.measure_id === m.measure_id) latest = x;
      });
    });
    openHistory(latest);
  };
  mask.addEventListener("click", (e) => {
    if (e.target === mask) { window.__qcsOnActionDone = null; mask.remove(); }
  });

  const head = document.createElement("div");
  head.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;";
  const title = document.createElement("div");
  title.style.cssText = "font-size:17px;font-weight:bold;";
  title.textContent = subNoOf(m) + " · 措施详情";
  const closeBtn = document.createElement("button");
  closeBtn.textContent = "✕";
  closeBtn.style.cssText = "border:none;background:none;font-size:20px;cursor:pointer;color:#868e96;";
  closeBtn.onclick = () => { window.__qcsOnActionDone = null; mask.remove(); };
  head.appendChild(title);
  head.appendChild(closeBtn);
  box.appendChild(head);

  // 1. 所属问题(完整描述, 不截断) —— 工程师没有"问题"tab, 这里是主要查看入口
  const issueBox = document.createElement("div");
  issueBox.style.cssText =
    "background:#f8f9fa;border-left:4px solid #3d5a80;border-radius:8px;" +
    "padding:10px 12px;margin-bottom:12px;";
  const ilabel = document.createElement("div");
  ilabel.style.cssText = "font-size:12px;color:#6c757d;margin-bottom:4px;";
  ilabel.textContent = "📋 所属问题";
  const idesc = document.createElement("div");
  idesc.style.cssText =
    "font-size:13px;color:#212529;line-height:1.6;white-space:normal;word-break:break-word;";
  idesc.textContent = m.problem_description || "(无描述)";
  issueBox.appendChild(ilabel);
  issueBox.appendChild(idesc);
  box.appendChild(issueBox);

  // 2. 措施信息
  const sub = document.createElement("div");
  sub.style.cssText = "color:#495057;font-size:12px;margin-bottom:12px;line-height:1.8;";
  sub.innerHTML =
    "<b>措施内容:</b> " + escapeHtml(m.measure_content || "(无措施)") + "<br>" +
    "<b>状态:</b> " + escapeHtml(m.status || "") +
    (m.is_overdue ? ' <span style="color:#e03131;font-weight:bold;">⚠️ 逾期 ' +
      m.overdue_days + " 天</span>" : "") + "<br>" +
    "<b>部门:</b> " + escapeHtml(m.department ?
      (DEPT_LABELS[m.department] || m.department) : "未分配") + "<br>" +
    "<b>责任人:</b> " + escapeHtml(m.owner || "未指定") +
    " · <b>预计完成:</b> " + escapeHtml(m.due_date || "未填") +
    (m.work_station ? "<br><b>工位:</b> " + escapeHtml(m.work_station) : "");
  box.appendChild(sub);

  // 3. 操作按钮(按角色 + 状态动态生成, 与列表卡片一致)
  const acts = getActions(m);
  if (acts.length > 0) {
    const brow = document.createElement("div");
    brow.className = "btn-row";
    brow.style.marginBottom = "12px";
    acts.forEach((a) => {
      const b = document.createElement("button");
      b.className = "btn " + a.cls;
      b.textContent = a.label;
      b.onclick = () => handleAction(m, a);
      brow.appendChild(b);
    });
    box.appendChild(brow);
  }

  // 4. 分隔 + 历史标题
  const hlabel = document.createElement("div");
  hlabel.style.cssText =
    "font-size:13px;font-weight:bold;margin:12px 0 6px;padding-top:10px;" +
    "border-top:1px solid #e9ecef;";
  hlabel.textContent = "状态变更历史";
  box.appendChild(hlabel);

  const loading = document.createElement("div");
  loading.style.cssText = "color:#868e96;text-align:center;padding:16px 0;";
  loading.textContent = "⏳ 正在加载历史...";
  box.appendChild(loading);

  try {
    const resp = await fetchWithAuth(API_BASE + "/api/measure_history?measure_id=" + m.measure_id);
    const data = await resp.json().catch(() => null);
    loading.remove();

    if (!resp.ok || !data || !data.success) {
      const err = document.createElement("div");
      err.style.cssText = "color:#c92a2a;text-align:center;padding:12px 0;";
      err.textContent = "⚠️ " + ((data && data.message) || "历史加载失败");
      box.appendChild(err);
      return;
    }

    if (!data.history || data.history.length === 0) {
      const empty = document.createElement("div");
      empty.style.cssText = "color:#868e96;text-align:center;padding:12px 0;";
      empty.textContent = "该措施暂无状态变更记录";
      box.appendChild(empty);
      return;
    }

    // 状态变更日志(倒序)
    data.history.forEach((r) => {
      const row = document.createElement("div");
      row.style.cssText =
        "border:1px solid #e9ecef;border-radius:10px;padding:10px;margin-bottom:8px;line-height:1.6;";
      const statusColor = { "待执行": "#868e96", "执行中": "#3d5a80", "已完成": "#e8590c", "已关闭": "#2f9e44" };
      const color = statusColor[r.review_result] || "#3d5a80";
      let rowHtml =
        '<div style="display:flex;justify-content:space-between;align-items:center;">' +
        "<b>" + escapeHtml(r.reviewer || "?") + "</b>" +
        '<b style="color:' + color + ';">' + escapeHtml(r.review_result || "?") + "</b></div>" +
        '<div style="color:#495057;margin-top:4px;">' + escapeHtml(r.review_comment || "(无备注)") + "</div>" +
        '<div style="color:#adb5bd;font-size:11px;margin-top:4px;">' + escapeHtml(r.reviewed_at || "") + "</div>";

      if (r.photo_path) {
        const fullUrl = API_BASE + r.photo_path;
        rowHtml +=
          '<a href="' + fullUrl + '" target="_blank" rel="noopener" ' +
          'style="display:inline-block;margin-top:8px;">' +
          '<img src="' + fullUrl + '" alt="证据照片" ' +
          'style="width:96px;height:96px;object-fit:cover;border-radius:8px;' +
          'border:1px solid #e9ecef;display:block;" /></a>';
      }
      row.innerHTML = rowHtml;
      box.appendChild(row);
    });
  } catch (e) {
    loading.remove();
    const err = document.createElement("div");
    err.style.cssText = "color:#c92a2a;text-align:center;padding:12px 0;";
    err.textContent = "⚠️ 历史加载失败：后端服务未启动或接口不可用";
    box.appendChild(err);
  }
}

// ============================================================
// 九、工具函数
// ============================================================

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

let toastTimer = null;
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2000);
}

// ============================================================
// 十、登录 / 登出 / 启动
// ============================================================

async function doLogin() {
  const username = (document.getElementById("loginUsername").value || "").trim();
  const password = document.getElementById("loginPassword").value || "";
  const errEl = document.getElementById("loginError");
  if (!username || !password) {
    errEl.textContent = "请输入账号和密码";
    return;
  }
  try {
    const resp = await fetch(API_BASE + "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username, password: password }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.success) {
      errEl.textContent = data.message || ("登录失败 HTTP " + resp.status);
      return;
    }
    saveToken(data.token);
    LOGIN_USER = {
      username: data.username,
      display_name: data.display_name,
      role: data.role,
      department: data.department || "",
    };
    errEl.textContent = "";
    enterApp();
  } catch (e) {
    errEl.textContent = "登录失败：后端服务未启动";
  }
}

async function doLogout() {
  try {
    await fetchWithAuth(API_BASE + "/api/logout", { method: "POST" });
  } catch (_) {}
  clearToken();
  showLoginView();
}

/** 登录成功 / token 有效后进入业务界面 */
async function enterApp() {
  showAppView();
  render();
  // 首屏只加载"措施"(默认tab), 不拉全量
  await loadMeasures();
  render();
  // 逾期角标数据异步补一次(不阻塞首屏); 工程师也不加载 issues
  loadOverdue().then(() => { enrichOverdueWithIssueNo(); overdueLoaded = true; render(); });
  // 人员名单(供经理二次分配选工程师)异步加载
  loadPersons();
}

// 绑定登录/退出按钮
document.getElementById("loginBtn").onclick = doLogin;
document.getElementById("logoutBtn").onclick = doLogout;
document.getElementById("loginPassword").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});

(async function init() {
  if (!AUTH_TOKEN) {
    showLoginView();
    return;
  }
  try {
    const resp = await fetchWithAuth(API_BASE + "/api/me");
    if (resp.status === 401) return;  // fetchWithAuth 已清 token 并切回登录页
    const data = await resp.json().catch(() => null);
    if (resp.ok && data && data.success) {
      LOGIN_USER = {
        username: data.username,
        display_name: data.display_name,
        role: data.role,
        department: data.department || "",
      };
      enterApp();
      return;
    }
  } catch (e) {
    console.error("校验登录失败:", e);
  }
  clearToken();
  showLoginView();
})();
