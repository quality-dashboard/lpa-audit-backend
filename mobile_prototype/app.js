/* ============================================================
   app.js - 分层审核移动端原型逻辑 (v2: 接入真实后端数据)
   ------------------------------------------------------------
   数据来源: FastAPI后端 (api_server.py) 的 GET /api/tasks 接口
     请求: http://<电脑IP或127.0.0.1>:8000/api/tasks?person_name=姓名
     (地址按访问设备自动切换, 见下方 API_BASE)
     返回: { as_level1:[...], as_level2:[...], as_level3:[...] }
   使用前提: 必须先启动后端(见下方说明), 再双击index.html。
   ------------------------------------------------------------
   ⚠️ 操作人姓名必须与数据库里填的责任人姓名完全一致
   (包含"王强/李敏/张涛"是占位演示名, 换成你库里真实的人名即可)
   ------------------------------------------------------------
   后端启动命令(在项目目录执行):
     uvicorn api_server:app --reload --host 0.0.0.0 --port 8000
   ============================================================ */

// ============================================================
// 一、配置
// ============================================================

// 后端接口地址 —— 根据页面是被谁打开的, 自动切换(不写死任何固定IP):
//   ① 电脑本机打开(localhost/127.0.0.1): 后端就在本机 → 用 http://127.0.0.1:8000
//   ② 手机通过局域网IP访问(如 http://192.168.x.x:5500): 后端在电脑上
//      → 取页面地址栏里的主机名(即电脑IP), 拼成 http://电脑IP:8000
//      (127.0.0.1在手机上指向手机自己, 所以绝不能写死)
// 用法: fetch(`${API_BASE}/api/tasks?person_name=...`)
const API_BASE =
  window.location.hostname === "localhost" ||
  window.location.hostname === "127.0.0.1"
    ? "http://127.0.0.1:8000"
    : "http://" + window.location.hostname + ":8000";

// 三位操作人(对应三层责任人) —— name必须与数据库责任人字段存的值完全一致
// (当前库里只存了姓氏, 后期换真实数据时把name/surname改成真实姓名)
const OPERATORS = [
  { name: "王", surname: "王", role: "第一责任人", roleTag: "操作工", level: 1 },
  { name: "李", surname: "李", role: "第二责任人", roleTag: "工程师", level: 2 },
  { name: "张", surname: "张", role: "第三责任人", roleTag: "经理",   level: 3 },
];

// ============================================================
// 二、状态
// ============================================================
let currentOpIndex = 0;          // 当前操作人下标
let currentTab = "pending";      // 当前tab: pending / done
let modalContext = null;         // 当前弹窗上下文 { issue, action }
let photoMockPicked = false;     // 照片占位框是否点击过(模拟已选择)

// 后端返回的数据: { as_level1:[], as_level2:[], as_level3:[], to_review:[], closed:[] }
// to_review = 待我评审的问题(仅第三责任人视角有数据)
// closed    = 与我相关的已关闭问题(持久化数据, "已处理"页签展示)
let apiData = { as_level1: [], as_level2: [], as_level3: [], to_review: [], closed: [] };
let loadError = false;           // 后端不可用标志(顶部显示红条提示)

// ============================================================
// 三、DOM 引用
// ============================================================
const avatarEl = document.getElementById("avatar");
const userNameEl = document.getElementById("userName");
const roleChipEl = document.getElementById("roleChip");
const pendingCountEl = document.getElementById("pendingCount");
const operatorSwitchEl = document.getElementById("operatorSwitch");
const contentEl = document.getElementById("content");
const tabBadgeEl = document.getElementById("tabBadge");
const tabPendingEl = document.getElementById("tabPending");
const tabDoneEl = document.getElementById("tabDone");
const toastEl = document.getElementById("toast");
const modalMaskEl = document.getElementById("modalMask");
const modalTitleEl = document.getElementById("modalTitle");
const modalIssueEl = document.getElementById("modalIssue");
const photoPlaceholderEl = document.getElementById("photoPlaceholder");
const photoTextEl = document.getElementById("photoText");
const modalInputEl = document.getElementById("modalInput");
const modalErrorEl = document.getElementById("modalError");
const modalOkBtnEl = document.getElementById("modalOkBtn");

// ============================================================
// 四、弹窗配置(三种操作, 与正式系统交互规则一致)
// ============================================================
const ACTION_CONFIG = {
  pass: {
    title: "✅ 通过确认",
    placeholder: "请填写整改完成情况说明...",
    btnText: "确认通过",
    danger: false,
    showPhoto: true,     // 通过时显示照片占位框(暂不强制上传, 后续版本接入)
    result: "通过",      // 提交给后端的review_result值
  },
  reject: {
    title: "❌ 退回确认",
    placeholder: "请填写验收不合格的原因...",
    btnText: "确认退回",
    danger: true,
    showPhoto: false,
    result: "退回",
  },
  track: {
    title: "🔺 跟踪中确认",
    placeholder: "请填写当前整改进展...",
    btnText: "确认记录",
    danger: false,
    showPhoto: false,
    result: "跟踪中",
  },
};

// ============================================================
// 五、数据加载(对接后端)
// ============================================================

/**
 * 从后端拉取指定责任人的关联问题, 成功后重新渲染列表。
 * 【后端接入点】fetch失败(后端没启动/网络断)时置loadError,
 * 页面顶部显示中文错误提示。
 */
async function loadTasks(personName) {
  try {
    const resp = await fetch(
      API_BASE + "/api/tasks?person_name=" + encodeURIComponent(personName)
    );
    if (!resp.ok) {
      // 后端返回了错误状态码(如数据库未配置) → 读出中文detail展示
      let detail = "HTTP " + resp.status;
      try {
        const err = await resp.json();
        if (err && err.detail) detail = err.detail;
      } catch (_) { /* 保持默认提示 */ }
      throw new Error(detail);
    }
    const data = await resp.json();
    apiData = {
      as_level1: data.as_level1 || [],
      as_level2: data.as_level2 || [],
      as_level3: data.as_level3 || [],
      to_review: data.to_review || [],  // 待我评审(仅第三责任人有值)
      closed: data.closed || [],        // 已处理(与该人相关的已关闭问题)
    };
    loadError = false;
  } catch (e) {
    // 网络层失败(后端没启动)或上述抛出的业务错误 → 统一进入错误态
    loadError = true;
    console.error("加载任务失败:", e);
  }
  render();  // 无论成败都渲染: 成功画列表, 失败画错误提示
}

/** 当前操作人视角对应的待处理数组(level1视角→as_level1组, 依此类推) */
function getMyPending() {
  const op = OPERATORS[currentOpIndex];
  const key = "as_level" + op.level;
  return apiData[key] || [];
}

// ============================================================
// 六、渲染函数
// ============================================================

/** 顶部错误提示条(后端不可用时显示在内容区最上方)
 *  注: style.css按需求不能改, 故用行内样式实现 */
function buildErrorBar() {
  const bar = document.createElement("div");
  bar.style.cssText =
    "background:#fde8e8;border:1px solid #e03131;color:#c92a2a;" +
    "border-radius:10px;padding:12px 14px;font-size:13px;" +
    "margin-bottom:12px;line-height:1.6;";
  bar.innerHTML =
    "⚠️ <b>后端服务未启动或接口不可用</b><br>" +
    '<span style="font-size:11px;color:#868e96">启动命令: uvicorn api_server:app --reload --host 0.0.0.0 --port 8000</span>';
  return bar;
}

/** 加载中提示(等待后端响应时) */
function buildLoadingTip() {
  const tip = document.createElement("div");
  tip.className = "empty-tip";
  tip.innerHTML = '<span class="empty-icon">⏳</span>正在加载任务数据...';
  return tip;
}

/** 渲染顶部操作人切换按钮排 */
function renderOperatorSwitch() {
  operatorSwitchEl.innerHTML = "";
  OPERATORS.forEach((op, idx) => {
    const btn = document.createElement("button");
    btn.className = "op-btn" + (idx === currentOpIndex ? " active" : "");
    btn.textContent = op.name;
    // 点击切换视角 → 拉取该责任人的真实数据
    btn.onclick = () => switchOperator(idx);
    operatorSwitchEl.appendChild(btn);
  });
}

/** 渲染主内容区 */
function render() {
  const op = OPERATORS[currentOpIndex];

  // --- 顶栏信息 ---
  avatarEl.textContent = op.surname;
  userNameEl.textContent = op.name;
  roleChipEl.textContent = op.role + " · " + op.roleTag;

  const myPending = getMyPending();
  // 第三责任人的待处理数 = 待评审数 + 待最终确认数(两组都是他的待办)
  const pendingTotal = op.level === 3
    ? myPending.length + apiData.to_review.length
    : myPending.length;
  pendingCountEl.textContent = pendingTotal;

  // --- tab栏角标/选中态 ---
  tabBadgeEl.textContent = pendingTotal;
  tabBadgeEl.className = "tab-badge" + (pendingTotal > 0 ? " show" : "");
  tabPendingEl.classList.toggle("active", currentTab === "pending");
  tabDoneEl.classList.toggle("active", currentTab === "done");

  // --- 内容区 ---
  contentEl.innerHTML = "";

  // 后端不可用: 顶部错误条 + 空列表占位(已处理tab仍可用, 是本地数据)
  if (loadError && currentTab === "pending") {
    contentEl.appendChild(buildErrorBar());
  }

  if (currentTab === "pending") {
    renderPendingList(op, myPending);
  } else {
    renderDoneList(op);
  }
}

/** 渲染"待处理"列表(真实数据: 后端返回的该责任人关联问题)
 *  第三责任人视角分两个区域: ①待我评审(to_review, 未评审的前置环节)
 *                            ②待我最终确认(as_level3, 分层审核第3层) */
function renderPendingList(op, myPending) {
  // 第三责任人: 先渲染"待我评审"区, 再渲染"待最终确认"区
  const totalForLevel3 = op.level === 3
    ? myPending.length + apiData.to_review.length : myPending.length;

  if (totalForLevel3 === 0) {
    // 有错误时上面已渲染错误条, 这里补一句引导; 正常空态显示庆祝文案
    const tip = document.createElement("div");
    tip.className = "empty-tip";
    tip.innerHTML = loadError
      ? '<span class="empty-icon">🔌</span>暂无数据(后端未连接)'
      : '<span class="empty-icon">🎉</span>暂无需要处理的任务<br>' +
        '<span style="font-size:12px">「' + op.name + '」名下当前没有关联问题</span>';
    contentEl.appendChild(tip);
    return;
  }

  // --- 区域①: 待我评审(仅第三责任人, 后端to_review组) ---
  if (op.level === 3 && apiData.to_review.length > 0) {
    const cap1 = document.createElement("div");
    cap1.className = "list-caption";
    cap1.textContent = "📋 待我评审 · 共 " + apiData.to_review.length +
      " 条(新录入问题, 需决策采纳与否)";
    contentEl.appendChild(cap1);
    apiData.to_review.forEach((issue) => {
      contentEl.appendChild(buildReviewCard(issue, op));  // 专用卡片: 采纳/不采纳按钮
    });
  }

  // --- 区域②: 分层审核待办(各视角的as_level组) ---
  if (myPending.length > 0) {
    const caption = document.createElement("div");
    caption.className = "list-caption";
    caption.textContent =
      "🛠️ " + (op.level === 3 ? "待我最终确认" : op.role + "视角") +
      " · 共 " + myPending.length + " 条(来自数据库)";
    contentEl.appendChild(caption);
    myPending.forEach((issue) => {
      contentEl.appendChild(buildIssueCard(issue, op));
    });
  }
}

/** 构造单条问题卡片
 *  issue来自后端: {id, problem_description, work_station, status, issue_date}
 *  op为当前操作人(按钮配置按他的角色层级决定, 数据本身不含层级) */
function buildIssueCard(issue, op) {
  const card = document.createElement("div");
  card.className = "issue-card";
  card.style.cursor = "pointer";
  // 点击卡片本体 → 查看完整流转历史(审核/采纳/退回全记录)
  card.onclick = () => openHistory(issue);

  // --- 第一行: 问题编号 + 状态标签 ---
  const head = document.createElement("div");
  head.className = "issue-head";
  head.innerHTML =
    '<span class="issue-id">问题 #' + issue.id + "</span>" +
    '<span class="status-chip status-' + statusClass(issue.status) + '">' +
    (issue.status || "未评审") + "</span>";
  card.appendChild(head);

  // --- 问题描述 ---
  const desc = document.createElement("div");
  desc.className = "issue-desc";
  desc.textContent = issue.problem_description || "(无描述)";
  card.appendChild(desc);

  // --- 工位 ---
  const station = document.createElement("div");
  station.className = "issue-station";
  station.textContent = "📍 " + (issue.work_station || "未填工位") +
    (issue.issue_date ? " · " + issue.issue_date : "");
  card.appendChild(station);

  // --- 提示行: 按当前视角说明该人角色 ---
  const meta = document.createElement("div");
  meta.className = "issue-meta";
  const roleTexts = { 1: "我是第一责任人(整改方)", 2: "我是第二责任人(验收方)", 3: "我是第三责任人(确认方)" };
  meta.textContent = roleTexts[op.level] || "";
  card.appendChild(meta);

  // --- 三个操作按钮(真实写入后端, 详见confirmModal的POST /api/review) ---
  const row = document.createElement("div");
  row.className = "btn-row";

  const btnPass = document.createElement("button");
  btnPass.className = "btn btn-pass";
  btnPass.textContent = "✅ 通过";
  // stopPropagation: 按钮点击不触发卡片的"查看历史"(卡片onclick)
  btnPass.onclick = (e) => { e.stopPropagation(); openModal(issue, "pass", op); };
  row.appendChild(btnPass);

  // 第一责任人(第1层)没有退回按钮 → 置灰(与正式系统规则一致)
  const btnReject = document.createElement("button");
  btnReject.className = "btn btn-reject" + (op.level === 1 ? " btn-disabled" : "");
  btnReject.textContent = "❌ 退回";
  if (op.level === 1) {
    btnReject.disabled = true;
    btnReject.title = "整改方没有退回按钮";
  } else {
    btnReject.onclick = (e) => { e.stopPropagation(); openModal(issue, "reject", op); };
  }
  row.appendChild(btnReject);

  const btnTrack = document.createElement("button");
  btnTrack.className = "btn btn-track";
  btnTrack.textContent = "🔺 跟踪中";
  btnTrack.onclick = (e) => { e.stopPropagation(); openModal(issue, "track", op); };
  row.appendChild(btnTrack);

  card.appendChild(row);
  return card;
}

/** 构造"待我评审"专用卡片(第三责任人视角)
 *  与审核卡片的区别: 按钮是"采纳/不采纳"而非"通过/退回/跟踪中",
 *  对应后端 POST /api/review_decision; 点卡片本体仍可看流转历史 */
function buildReviewCard(issue, op) {
  const card = document.createElement("div");
  card.className = "issue-card";
  card.style.cursor = "pointer";
  card.onclick = () => openHistory(issue);

  // --- 第一行: 问题编号 + 状态标签 ---
  const head = document.createElement("div");
  head.className = "issue-head";
  head.innerHTML =
    '<span class="issue-id">问题 #' + issue.id + "</span>" +
    '<span class="status-chip status-wait">' + (issue.status || "未评审") + "</span>";
  card.appendChild(head);

  // --- 问题描述 ---
  const desc = document.createElement("div");
  desc.className = "issue-desc";
  desc.textContent = issue.problem_description || "(无描述)";
  card.appendChild(desc);

  // --- 工位 + 日期 ---
  const station = document.createElement("div");
  station.className = "issue-station";
  station.textContent = "📍 " + (issue.work_station || "未填工位") +
    (issue.issue_date ? " · " + issue.issue_date : "");
  card.appendChild(station);

  // --- 角色提示行 ---
  const meta = document.createElement("div");
  meta.className = "issue-meta";
  meta.textContent = "我是第三责任人(评审决策方)";
  card.appendChild(meta);

  // --- 两个评审按钮(不需要填备注, 评审备注由后端按规则固定写入) ---
  const row = document.createElement("div");
  row.className = "btn-row";

  const btnAdopt = document.createElement("button");
  btnAdopt.className = "btn btn-pass";
  btnAdopt.textContent = "✅ 采纳";
  btnAdopt.onclick = (e) => { e.stopPropagation(); confirmDecision(issue, op, "adopt"); };
  row.appendChild(btnAdopt);

  const btnReject = document.createElement("button");
  btnReject.className = "btn btn-reject";
  btnReject.textContent = "❌ 不采纳";
  btnReject.onclick = (e) => { e.stopPropagation(); confirmDecision(issue, op, "reject"); };
  row.appendChild(btnReject);

  card.appendChild(row);
  return card;
}

// ============================================================
// 评审决策: 确认弹窗 → POST /api/review_decision 真实写入
// ============================================================

let decisionMaskEl = null;  // 评审确认弹窗遮罩(同时只存在一个)

/** 关闭评审确认弹窗(直接移除DOM) */
function closeDecision() {
  if (decisionMaskEl) {
    decisionMaskEl.remove();
    decisionMaskEl = null;
  }
}

/** 打开评审确认弹窗: issue=问题, op=当前操作人, decision='adopt'/'reject'
 *  评审不要求填备注 → 纯确认弹窗(标题+问题摘要+确认/取消按钮) */
function confirmDecision(issue, op, decision) {
  closeDecision();  // 防重复叠加
  const isAdopt = decision === "adopt";

  // --- 遮罩 + 弹窗容器(行内样式, 与历史弹窗同款风格) ---
  decisionMaskEl = document.createElement("div");
  decisionMaskEl.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1001;" +
    "display:flex;align-items:center;justify-content:center;padding:16px;";
  const box = document.createElement("div");
  box.style.cssText =
    "background:#fff;border-radius:14px;max-width:340px;width:100%;padding:18px;font-size:13px;";
  decisionMaskEl.appendChild(box);
  document.body.appendChild(decisionMaskEl);

  // 点遮罩空白处关闭
  decisionMaskEl.addEventListener("click", (e) => {
    if (e.target === decisionMaskEl) closeDecision();
  });

  // --- 标题 ---
  const title = document.createElement("b");
  title.style.cssText = "font-size:14px;display:block;margin-bottom:8px;color:" +
    (isAdopt ? "#2f9e44" : "#c92a2a") + ";";
  title.textContent = isAdopt ? "✅ 确认采纳该问题？" : "❌ 确认不采纳该问题？";
  box.appendChild(title);

  // --- 问题摘要 ---
  const summary = document.createElement("div");
  summary.style.cssText = "color:#495057;line-height:1.6;margin-bottom:10px;";
  summary.textContent =
    "问题 #" + issue.id + " · " + (issue.work_station || "未填工位") + "\n" +
    (issue.problem_description || "").slice(0, 40);
  box.appendChild(summary);

  // --- 后果说明(帮评审人理解决策影响) ---
  const hint = document.createElement("div");
  hint.style.cssText = "color:#868e96;font-size:12px;margin-bottom:14px;line-height:1.5;";
  hint.textContent = isAdopt
    ? "采纳后问题进入分层整改流程（第一责任人待办）"
    : "不采纳后问题将搁置，不进入分层审核（后续可在电脑端重新采纳）";
  box.appendChild(hint);

  // --- 按钮行: 取消 + 确认 ---
  const btnRow = document.createElement("div");
  btnRow.style.cssText = "display:flex;gap:10px;";
  const btnCancel = document.createElement("button");
  btnCancel.textContent = "取消";
  btnCancel.style.cssText =
    "flex:1;border:1px solid #dee2e6;background:#fff;border-radius:10px;" +
    "padding:10px 0;cursor:pointer;font-size:13px;color:#495057;";
  btnCancel.onclick = closeDecision;
  btnRow.appendChild(btnCancel);

  const btnOk = document.createElement("button");
  btnOk.textContent = isAdopt ? "确认采纳" : "确认不采纳";
  btnOk.style.cssText =
    "flex:1;border:none;border-radius:10px;padding:10px 0;cursor:pointer;" +
    "font-size:13px;color:#fff;background:" + (isAdopt ? "#2f9e44" : "#c92a2a") + ";";
  btnOk.onclick = async () => {
    // 防重复提交: 禁用两个按钮
    btnOk.disabled = true;
    btnCancel.disabled = true;
    btnOk.textContent = "提交中...";
    try {
      // 真实写入: POST评审决策到后端
      const resp = await fetch(API_BASE + "/api/review_decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          issue_id: issue.id,
          reviewer: op.name,
          decision: decision,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.success) {
        // 业务校验失败: 恢复按钮, 弹窗内红字提示(不关闭, 可取消)
        btnOk.disabled = false;
        btnCancel.disabled = false;
        btnOk.textContent = isAdopt ? "确认采纳" : "确认不采纳";
        hint.style.color = "#c92a2a";
        hint.textContent = "⚠️ " + (data.message || "提交失败(HTTP " + resp.status + ")");
        return;
      }
      closeDecision();
      showToast("✅ 问题 #" + issue.id + " " + data.message);
      // 刷新任务列表: 采纳→问题流转到第一责任人; 不采纳→从待评审消失
      await loadTasks(op.name);
    } catch (e) {
      btnOk.disabled = false;
      btnCancel.disabled = false;
      btnOk.textContent = isAdopt ? "确认采纳" : "确认不采纳";
      hint.style.color = "#c92a2a";
      hint.textContent = "⚠️ 提交失败：后端服务未启动或接口不可用";
    }
  };
  btnRow.appendChild(btnOk);
  box.appendChild(btnRow);
}

/** 渲染"已处理"列表 —— 数据库中与当前责任人相关的已关闭问题(持久化)
 *  v3: 数据来自后端 /api/tasks 的 closed 组, 刷新/重开页面仍然可见,
 *      不再依赖前端会话临时状态; 点击卡片可查看完整流转历史 */
function renderDoneList(op) {
  const closed = apiData.closed || [];

  if (closed.length === 0) {
    const tip = document.createElement("div");
    tip.className = "empty-tip";
    tip.innerHTML = loadError
      ? '<span class="empty-icon">🔌</span>暂无数据(后端未连接)'
      : '<span class="empty-icon">📭</span>暂无已关闭问题<br>' +
        '<span style="font-size:12px">与「' + op.name + '」相关的问题闭环后将显示在这里</span>';
    contentEl.appendChild(tip);
    return;
  }

  const caption = document.createElement("div");
  caption.className = "list-caption";
  caption.textContent = "✅ 已关闭 · 共 " + closed.length + " 条(来自数据库)";
  contentEl.appendChild(caption);

  closed.forEach((issue) => {
    // 已关闭问题不再带操作按钮, 只读卡片; 点卡片看流转历史
    const card = document.createElement("div");
    card.className = "issue-card processed-card";
    card.style.cursor = "pointer";
    card.onclick = () => openHistory(issue);
    card.innerHTML =
      '<div class="issue-head"><span class="issue-id">问题 #' + issue.id + "</span>" +
      '<span class="status-chip status-closed">' + (issue.status || "已关闭") + "</span></div>" +
      '<div class="issue-desc">' + escapeHtml(issue.problem_description || "(无描述)") + "</div>" +
      '<div class="issue-station">📍 ' + escapeHtml(issue.work_station || "未填工位") +
      (issue.issue_date ? " · " + issue.issue_date : "") + "</div>" +
      '<div class="issue-meta">👆 点击查看完整流转历史</div>';
    contentEl.appendChild(card);
  });
}

/** 状态文字 → 样式类名 */
function statusClass(status) {
  const map = { 已评审: "reviewed", 已退回: "back", 未评审: "wait", 已关闭: "closed" };
  return map[status] || "wait";
}

/** HTML转义(防备注里的特殊字符打乱页面) */
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ============================================================
// 七、交互逻辑
// ============================================================

/** 切换操作人: 拉取该责任人的真实数据(异步, 加载期间显示等待提示) */
function switchOperator(idx) {
  currentOpIndex = idx;
  currentTab = "pending";
  // 先渲染"加载中", 再异步拉数据(数据回来后render会重画列表)
  render();
  contentEl.insertBefore(buildLoadingTip(), contentEl.firstChild);
  loadTasks(OPERATORS[idx].name);
}

/** 切换底部tab */
function switchTab(tab) {
  currentTab = tab;
  render();
  // 切到"已处理"时重新拉取: 已关闭列表(closed)来自数据库, 每次打开拿最新
  if (tab === "done") {
    loadTasks(OPERATORS[currentOpIndex].name);
  }
}

/** 打开操作确认弹窗(issue=问题, action=pass/reject/track, op=当前操作人) */
function openModal(issue, action, op) {
  const cfg = ACTION_CONFIG[action];
  modalContext = { issue, action, op };
  photoMockPicked = false;

  modalTitleEl.textContent = cfg.title;
  modalIssueEl.textContent =
    "问题 #" + issue.id + " · " + (issue.work_station || "未填工位") +
    " · " + (issue.problem_description || "").slice(0, 18) + "…";
  modalInputEl.value = "";
  modalInputEl.placeholder = cfg.placeholder;
  modalOkBtnEl.textContent = cfg.btnText;
  modalOkBtnEl.className = "btn-modal-ok" + (cfg.danger ? " danger" : "");

  // 照片占位框: 仅"通过"时显示(与正式系统防造假门槛一致)
  photoPlaceholderEl.className = "photo-placeholder" + (cfg.showPhoto ? " show" : "");
  photoTextEl.textContent = "请上传整改后现场照片";

  modalErrorEl.classList.remove("show");
  modalErrorEl.textContent = "";

  modalMaskEl.classList.add("show");
  modalInputEl.focus();
}

/** 照片占位框点击: 模拟已选择(不真上传) */
function togglePhotoMock() {
  photoMockPicked = !photoMockPicked;
  photoPlaceholderEl.classList.toggle("picked", photoMockPicked);
  photoTextEl.textContent = photoMockPicked ? "✓ 已选择照片（模拟）" : "请上传整改后现场照片";
}

/** 关闭弹窗 */
function closeModal() {
  modalMaskEl.classList.remove("show");
  modalContext = null;
}

/** 点遮罩空白处关闭弹窗(手机习惯操作) */
modalMaskEl.addEventListener("click", (e) => {
  if (e.target === modalMaskEl) closeModal();
});

/** 弹窗点确认: 备注必填校验 → POST /api/review 真实写入数据库 */
async function confirmModal() {
  if (!modalContext) return;
  const { issue, action, op } = modalContext;
  const remark = modalInputEl.value.trim();

  // 校验: 备注必填(通过=整改说明 / 退回=原因 / 跟踪中=进展)
  if (!remark) {
    modalErrorEl.textContent = "⚠️ 备注必填，请填写后再提交";
    modalErrorEl.classList.add("show");
    return;
  }

  // 防重复提交: 提交期间禁用按钮并给视觉反馈
  modalOkBtnEl.disabled = true;
  modalOkBtnEl.textContent = "提交中...";

  try {
    // 真实写入: POST到后端 /api/review, 按当前操作人的层级提交
    const resp = await fetch(API_BASE + "/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        issue_id: issue.id,
        review_level: op.level,          // 1/2/3 由当前操作人角色决定
        reviewer: op.name,               // 审核人姓名
        review_result: ACTION_CONFIG[action].result,  // 通过/退回/跟踪中
        review_comment: remark,
      }),
    });

    // 解析后端返回(成功{"success":true} / 校验失败400带中文message)
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.success) {
      // 业务校验失败: 错误显示在弹窗内, 弹窗不关闭, 方便改备注重试
      modalErrorEl.textContent = "⚠️ " + (data.message || "提交失败(HTTP " + resp.status + ")");
      modalErrorEl.classList.add("show");
      return;
    }

    closeModal();
    showToast("✅ 问题 #" + issue.id + " " + data.message);

    // 重新拉取当前人的任务列表: 该问题会按新层级流转到对应责任人的待办;
    // 若本次操作导致问题闭环, 它会出现在"已处理"页签(closed来自数据库)
    await loadTasks(op.name);
  } catch (e) {
    // 网络层失败(后端没启动等): 错误显示在弹窗内
    modalErrorEl.textContent = "⚠️ 提交失败：后端服务未启动或接口不可用";
    modalErrorEl.classList.add("show");
  } finally {
    // 无论成败都恢复按钮状态(失败时可重试)
    modalOkBtnEl.disabled = false;
    modalOkBtnEl.textContent = modalContext ? ACTION_CONFIG[modalContext.action].btnText : "确认";
  }
}

/** Toast轻提示: 2秒后自动消失 */
let toastTimer = null;
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2000);
}

// ============================================================
// 八、流转历史弹窗: 点击任意问题卡片查看完整审核轨迹
//    数据来自 GET /api/history?issue_id=X, 最新记录在最上;
//    弹窗用JS动态创建+行内样式, 不改index.html/style.css。
// ============================================================

// 审核层级 → 显示名(与正式系统三级责任人命名一致)
const LEVEL_NAMES = {
  1: "第一责任人(操作工)",
  2: "第二责任人(工程师/主管)",
  3: "第三责任人(经理)",
};

let historyMaskEl = null;  // 历史弹窗遮罩元素(同时只存在一个)

/** 关闭历史弹窗(直接移除DOM, 不影响页面列表) */
function closeHistory() {
  if (historyMaskEl) {
    historyMaskEl.remove();
    historyMaskEl = null;
  }
}

/** 打开流转历史弹窗: issue为问题对象(用id查历史, 用描述做副标题) */
async function openHistory(issue) {
  closeHistory();  // 防止重复叠加

  // --- 创建遮罩 + 弹窗容器(行内样式, 白底圆角与现有风格一致) ---
  historyMaskEl = document.createElement("div");
  historyMaskEl.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;" +
    "display:flex;align-items:center;justify-content:center;padding:16px;";
  const box = document.createElement("div");
  box.style.cssText =
    "background:#fff;border-radius:14px;max-width:400px;width:100%;" +
    "max-height:75vh;overflow-y:auto;padding:16px;font-size:13px;";
  historyMaskEl.appendChild(box);
  document.body.appendChild(historyMaskEl);

  // 点遮罩空白处关闭(与审核弹窗操作习惯一致)
  historyMaskEl.addEventListener("click", (e) => {
    if (e.target === historyMaskEl) closeHistory();
  });

  // --- 标题行: 问题编号 + 关闭按钮 ---
  const head = document.createElement("div");
  head.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;";
  const title = document.createElement("b");
  title.style.cssText = "font-size:14px;";
  title.textContent = "📜 流转历史 · 问题 #" + issue.id;
  const closeBtn = document.createElement("button");
  closeBtn.textContent = "✕";
  closeBtn.style.cssText =
    "border:none;background:#f1f3f5;border-radius:8px;width:26px;height:26px;" +
    "cursor:pointer;font-size:13px;color:#495057;";
  closeBtn.onclick = closeHistory;
  head.appendChild(title);
  head.appendChild(closeBtn);
  box.appendChild(head);

  // --- 副标题: 问题描述 + 工位 ---
  const subTitle = document.createElement("div");
  subTitle.style.cssText = "color:#868e96;font-size:12px;margin-bottom:12px;line-height:1.5;";
  subTitle.textContent = (issue.problem_description || "(无描述)") +
    (issue.work_station ? " · " + issue.work_station : "");
  box.appendChild(subTitle);

  // --- 先显示loading, 异步拉取历史记录 ---
  const loading = document.createElement("div");
  loading.style.cssText = "color:#868e96;text-align:center;padding:16px 0;";
  loading.textContent = "⏳ 正在加载流转历史...";
  box.appendChild(loading);

  try {
    const resp = await fetch(API_BASE + "/api/history?issue_id=" + issue.id);
    const data = await resp.json().catch(() => null);
    loading.remove();

    // 后端返回错误对象(如数据库异常)或HTTP失败 → 弹窗内提示, 页面不崩
    if (!resp.ok || !Array.isArray(data)) {
      const err = document.createElement("div");
      err.style.cssText = "color:#c92a2a;text-align:center;padding:12px 0;";
      err.textContent = "⚠️ " + ((data && data.message) || "历史加载失败(HTTP " + resp.status + ")");
      box.appendChild(err);
      return;
    }

    // 无任何记录(刚登记还未评审)
    if (data.length === 0) {
      const empty = document.createElement("div");
      empty.style.cssText = "color:#868e96;text-align:center;padding:12px 0;";
      empty.textContent = "该问题暂无审核记录(刚登记, 还未评审)";
      box.appendChild(empty);
      return;
    }

    // 逐条渲染历史(后端已按id倒序, 最新在最上):
    // 每条含 层级/审核人/结果/备注/时间, 评审采纳与退回原因都在备注里
    data.forEach((r) => {
      const row = document.createElement("div");
      row.style.cssText =
        "border:1px solid #e9ecef;border-radius:10px;padding:10px;margin-bottom:8px;line-height:1.6;";
      // 结果配色: 通过=绿 / 退回=红 / 跟踪中=橙(与卡片状态色一致)
      const resultColor = r.review_result === "通过" ? "#2f9e44"
        : r.review_result === "退回" ? "#c92a2a" : "#e8590c";
      row.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;">' +
        "<b>" + (LEVEL_NAMES[r.review_level] || ("第" + r.review_level + "层")) +
        " · " + escapeHtml(r.reviewer || "?") + "</b>" +
        '<b style="color:' + resultColor + ';">' + escapeHtml(r.review_result || "?") + "</b></div>" +
        '<div style="color:#495057;margin-top:4px;">' + escapeHtml(r.review_comment || "(无备注)") + "</div>" +
        '<div style="color:#adb5bd;font-size:11px;margin-top:4px;">' + escapeHtml(r.reviewed_at || "") + "</div>";
      box.appendChild(row);
    });
  } catch (e) {
    // 网络层失败(后端没启动等)
    loading.remove();
    const err = document.createElement("div");
    err.style.cssText = "color:#c92a2a;text-align:center;padding:12px 0;";
    err.textContent = "⚠️ 历史加载失败：后端服务未启动或接口不可用";
    box.appendChild(err);
  }
}

// ============================================================
// 九、启动: 首次进入即拉取第一位操作人的真实数据
// ============================================================
renderOperatorSwitch();
render();
loadTasks(OPERATORS[0].name);
