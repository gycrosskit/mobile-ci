(function () {
  "use strict";
  const labels = { configured: "已配置", pending: "待接入", "not-configured": "待接入", missing: "缺失", valid: "有效", expiring: "即将过期", expired: "已过期", "not-yet-valid": "尚未生效", unknown: "需核验", partial: "部分查询未完成", complete: "已查询", invalid: "材料无效", mismatch: "不匹配", conflict: "冲突", "wrong-type": "类型错误", pgyer: "Ad Hoc 测试", testflight: "TestFlight", appstore: "App Store", designated: "指定设备", success: "已完成", "not-run": "未执行" };
  labels.running = "执行中";
  const label = value => labels[value] || "待核验";
  const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const eligible = row => ["ios", "harmony"].includes(row.platform) && row.configurationStatus === "configured" && row.channelEnabled?.[row.platform === "ios" ? "pgyer" : "designated"] === true;
  const covered = (device, row) => (device.sources || []).some(source => source.brand === row.brand && (!source.team || source.team === row.team));
  const eligibleBrands = (rows, platform, device) => rows.filter(row => row.platform === platform && eligible(row) && (!device || !covered(device, row)));
  const formatDate = value => {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date) : "未知";
  };
  const isStale = value => !value || !Number.isFinite(Date.parse(value)) || Date.now() - Date.parse(value) > 24 * 60 * 60 * 1000;
  const safeUrl = (value, base) => {
    try { const url = new URL(value, base); return url.origin === new URL(base).origin && !url.username && !url.password && /^https?:$/.test(url.protocol) ? url.href : null; } catch { return null; }
  };
  const canConfirm = task => task.state === "finished" && task.result === "SUCCESS" && !(task.brands || []).some(row => row.status !== "success");
  const taskSummary = task => task.state === "queued" ? "已排队，等待执行" : task.state === "running" ? "正在执行，请勿重复提交" : task.state === "cancelled" ? "任务已取消" : task.result === "SUCCESS" ? "任务已完成" : task.result === "UNSTABLE" ? "部分操作未完成，请逐品牌核对" : "任务失败，请逐品牌核对";
  const requestId = (cryptoApi = globalThis.crypto) => typeof cryptoApi.randomUUID === "function" ? cryptoApi.randomUUID() : [...cryptoApi.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, "0")).join("");
  const teamLabel = row => [...new Set([row.companyName, row.teamId, row.team].filter(Boolean))].join(" · ") || (row.platform === "android" ? "Android 应用签名" : "待确认");
  const formError = (step, platform, name, udid, brands) => {
    if (!name.trim()) return "请填写设备名称。";
    if (name.trim().length > (platform === "ios" ? 80 : 100)) return "设备名称过长，请缩短后重试。";
    const validUdid = platform === "ios" ? /^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{16})$/ : /^[A-Za-z0-9-]{16,64}$/;
    if (!validUdid.test(udid.trim())) return platform === "ios" ? "请填写有效的 iOS UDID：40 位十六进制，或 8 位-16 位格式。" : "请填写有效的鸿蒙 UDID：16–64 位字母、数字或连字符。";
    return step > 1 && !brands.length ? "请至少选择一个品牌。" : "";
  };
  const signatureSummary = row => {
    if (row.configurationStatus !== "configured") return { text: ["pending", "not-configured"].includes(row.configurationStatus) ? "待接入" : "品牌配置需核验", tone: "warn" };
    if (row.queryStatus !== "complete" || row.errors?.length) return { text: "查询未完成 · 需核验", tone: "warn" };
    if (row.materialStatus === "invalid") return { text: "签名材料无效", tone: "bad" };
    if (row.materialStatus !== "configured") return { text: row.materialStatus === "missing" ? "缺少签名材料" : "签名材料需核验", tone: "warn" };
    if (row.enabled !== true) return { text: "签名未启用", tone: "warn" };
    const channels = Object.entries(row.channelEnabled || {}).filter(([, enabled]) => enabled === true).map(([channel]) => channel);
    const profiles = row.profiles || [], certificates = row.certificates || [];
    if (!certificates.length || (row.platform !== "android" && (!channels.length || channels.some(channel => !profiles.some(item => item.channel === channel))))) return { text: "签名信息不完整", tone: "warn" };
    if (row.platform === "android" && certificates.some(item => item.matchesExpectedSha256 !== true)) return { text: "证书指纹不匹配", tone: "bad" };
    const materials = [...certificates, ...profiles.filter(item => channels.includes(item.channel))];
    const failed = materials.find(item => ["expired", "invalid", "mismatch", "conflict", "wrong-type"].includes(item.status));
    if (failed) return { text: label(failed.status), tone: "bad" };
    if (materials.some(item => !["valid", "expiring"].includes(item.status) || !item.expiresAt || !Number.isFinite(Date.parse(item.expiresAt)))) return { text: "签名状态需核验", tone: "warn" };
    if (materials.some(item => Date.parse(item.expiresAt) <= Date.now())) return { text: "已过期", tone: "bad" };
    if (profiles.filter(item => channels.includes(item.channel)).some(item => item.identityVerified !== true || item.materialReady !== true)) return { text: "签名材料需核验", tone: "warn" };
    if (isStale(row.collectedAt)) return { text: "台账已过期 · 请刷新", tone: "warn" };
    if (materials.some(item => item.status === "expiring")) return { text: "签名即将过期", tone: "warn" };
    return { text: "台账检查通过", tone: "good" };
  };
  const detectDevice = (agent = "", os = "", touchPoints = 0, mobileHint = false) => {
    const ios = /iPhone|iPad|iPod/i.test(agent) || (os === "MacIntel" && touchPoints > 1);
    const harmony = /HarmonyOS|OpenHarmony|HongMeng/i.test(agent);
    const mobile = ios || harmony || mobileHint || /Android|Mobile/i.test(agent);
    return { mobile, platform: ios ? "ios" : harmony ? "harmony" : null,
      safari: ios && /Safari/i.test(agent) && !/CriOS|FxiOS|EdgiOS|OPiOS|MicroMessenger/i.test(agent) };
  };
  const certificateExpiry = row => {
    const dates = (row.certificates || []).map(item => item.expiresAt).filter(value => value && Number.isFinite(Date.parse(value)));
    return dates.length ? formatDate(dates.sort((a, b) => Date.parse(a) - Date.parse(b))[0]).split(" ")[0] : "待核验";
  };
  const helpers = { detectDevice, certificateExpiry, formError, signatureSummary, label, escape, eligible, covered, eligibleBrands, formatDate, isStale, safeUrl, canConfirm, taskSummary, requestId, teamLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const base = new URL("./", location.href);
  const platformName = value => ({android: "Android", ios: "iOS", harmony: "鸿蒙"})[value];
  const deviceContext = detectDevice(navigator.userAgent, navigator.platform, navigator.maxTouchPoints, navigator.userAgentData?.mobile);
  document.body.dataset.device = deviceContext.mobile ? "mobile" : "desktop";
  let acquisition = deviceContext.mobile && deviceContext.platform ? "intro" : "choose", wireless = false;
  let state = null, platform = deviceContext.platform || "ios", view = "devices", addStep = 1, active = false, pending = null, syncDevice = null, uncertain = null, stateTimer = null;
  const badge = status => `<span class="badge ${["valid", "complete", "configured", "success"].includes(status) ? "good" : ["expired", "invalid", "mismatch", "conflict", "wrong-type"].includes(status) ? "bad" : "warn"}">${escape(label(status))}</span>`;
  const rows = () => state?.inventory?.brands || [];
  const devices = () => state?.inventory?.devices?.[platform] || [];
  const brandName = id => rows().find(row => row.brand === id)?.displayName || id;
  const notice = (message, kind = "", login = false) => {
    $("notice").className = `notice ${kind}`;
    $("notice").textContent = message;
    $("notice").hidden = !message;
    if (login) { const link = document.createElement("a"); link.textContent = "重新登录 Jenkins"; link.href = new URL(`../login?from=${encodeURIComponent(base.pathname)}`, base).href; $("notice").append(link); }
  };
  const showError = error => notice(error.message, "error", error.login);
  async function api(path, options = {}) {
    let response;
    try { response = await fetch(new URL(path, base), { credentials: "same-origin", cache: "no-store", ...options }); }
    catch { const error = new Error(path === "capture" ? "设备读取连接中断，请检查调试连接后重试。" : options.method === "POST" ? "连接中断，提交结果尚未确认。请用下方按钮重新查询同一次提交，勿另行提交。" : "无法连接 Jenkins，请检查网络后重试。"); error.uncertain = options.method === "POST" && path !== "capture"; throw error; }
    if (response.status === 401 || response.redirected || !response.headers.get("content-type")?.includes("application/json")) {
      const error = new Error("Jenkins 登录已过期或接口不可用，请重新登录后查看任务状态。"); error.login = true; error.uncertain = options.method === "POST" && (response.ok || response.redirected || response.status >= 500); throw error;
    }
    if (!response.ok) {
      const messages = { 403: "当前登录无操作权限，或登录校验已失效。请重新登录 Jenkins 后重试。", 409: "已有签名任务在执行或排队，请等待结束后再操作。", 400: "输入或品牌选择不符合要求，请检查后重试。", 404: "任务暂时不可查询，请在 Jenkins 中核对。", 503: "签名维护暂时不可用，请稍后重试。" };
      const detail = await response.json().catch(() => ({}));
      const message = path === "capture" && typeof detail.message === "string" ? detail.message : detail.code === "recovery-unavailable" ? "原提交已无法自动查询，请先在 Jenkins 核对已有任务；勿重新执行设备写入。" : messages[response.status];
      const error = new Error(message || "Jenkins 暂时无法完成请求，请在任务记录中核对。"); error.status = response.status; error.login = response.status === 403; error.uncertain = options.method === "POST" && response.status >= 500; throw error;
    }
    try { return await response.json(); } catch { const error = new Error("Jenkins 返回内容不完整，请重新查询任务状态。"); error.uncertain = options.method === "POST" && path !== "capture"; throw error; }
  }
  function updateControls() {
    const locked = active || !!uncertain || !state || state.busy;
    const cannotMaintain = locked || !state?.canMaintain;
    $("refresh").disabled = cannotMaintain;
    ["capture-start", "capture-read", "manual-entry", "capture-again", "switch-platform"].forEach(id => { $(id).disabled = cannotMaintain || addStep > 1; });
    document.querySelectorAll("[data-platform]").forEach(button => { button.disabled = cannotMaintain || addStep > 1; });
    $("capture-target").disabled = cannotMaintain;
    $("preview-add").disabled = cannotMaintain || (addStep > 1 && !eligibleBrands(rows(), platform).length);
    $("add-back").disabled = cannotMaintain;
    $("preview-sync").disabled = cannotMaintain;
    $("sync-all").disabled = cannotMaintain || !devices().length || !eligibleBrands(rows(), platform).length;
    $("apply").disabled = cannotMaintain || !pending;
    $("device-form").querySelectorAll("input").forEach(input => { input.disabled = cannotMaintain; });
    $("sync-form").querySelectorAll("input").forEach(input => { input.disabled = cannotMaintain; });
    document.querySelectorAll('[name="platform"]').forEach(input => { input.disabled = locked || (view === "add" && (!state?.canMaintain || addStep > 1)); });
    document.querySelectorAll("[data-sync]").forEach(button => { button.disabled = cannotMaintain; });
  }
  function renderAddStep() {
    renderAcquisition();
    $("device-step").hidden = addStep !== 1;
    $("brand-step").hidden = addStep !== 2;
    $("review-step").hidden = addStep !== 3;
    $("add-back").hidden = addStep === 1;
    $("step-progress").textContent = `第 ${addStep} 步，共 3 步`;
    $("preview-add").textContent = ["继续", "继续", "检查并确认"][addStep - 1];
    document.querySelectorAll("[data-step]").forEach(item => { if (Number(item.dataset.step) === addStep) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current"); });
    $("add-review").innerHTML = `<p>设备：<strong>${escape($("device-name").value.trim())}</strong></p><p>平台：${escape(platformName(platform))}</p><p>品牌：${selectedBrands("add-brands").map(id => escape(brandName(id))).join("、") || "尚未选择"}</p>`;
    updateControls();
  }
  function brandOptions(container, device, preserve = true) {
    const selected = preserve ? selectedBrands(container) : [];
    const available = eligibleBrands(rows(), platform, device);
    $(container).innerHTML = available.length ? available.map(row => `<label class="brand-option"><input type="checkbox" name="brand" value="${escape(row.brand)}"><span>${escape(row.displayName)}<small>团队：${escape(teamLabel(row))}</small></span></label>`).join("") : '<p class="hint">暂无可选择品牌，请先完成品牌签名接入。</p>';
    $(container).querySelectorAll("input").forEach(input => { input.checked = selected.includes(input.value); });
    const excluded = rows().filter(row => row.platform === platform && !available.includes(row));
    $(container === "add-brands" ? "add-excluded" : "sync-excluded").textContent = excluded.length ? `未列出：${excluded.map(row => `${row.displayName}（${device && covered(device, row) ? "已覆盖" : row.configurationStatus !== "configured" ? "待接入" : "设备测试签名未启用"}）`).join("、")}` : "";
  }
  function renderDevices() {
    const search = $("search").value.trim().toLowerCase();
    const filtered = devices().filter(row => [row.name, row.maskedUdid, ...(row.sources || []).map(source => brandName(source.brand))].some(value => String(value || "").toLowerCase().includes(search)));
    $("devices-count").textContent = `${platformName(platform)} · ${devices().length} 台已批准共享设备`;
    $("device-list").innerHTML = filtered.length ? filtered.map(row => {
      const brands = [...new Set((row.sources || []).map(source => source.brand))];
      return `<article class="device-row"><div><h3>${escape(row.name)}</h3><p class="masked">${escape(row.maskedUdid)}</p><details><summary>查看来源签名</summary><p class="hint">来源记录表示设备曾通过复用核对，最新覆盖情况以刷新台账与包内签名为准。</p><ul class="source-list">${(row.sources || []).map(source => `<li><strong>${escape(brandName(source.brand))}</strong> · ${escape(source.team || "团队待确认")}<br>${escape(source.profile || "签名待确认")}</li>`).join("")}</ul></details></div><div class="device-coverage"><div class="chips">${brands.map(id => `<span class="chip">${escape(brandName(id))}</span>`).join("") || '<span class="hint">来源待确认</span>'}</div><p class="hint">关联 ${brands.length} 个品牌</p></div>${state?.canMaintain && eligibleBrands(rows(), platform, row).length ? `<button type="button" data-sync="${escape(row.id)}">同步到其他品牌</button>` : '<p class="hint">暂无待同步品牌</p>'}</article>`;
    }).join("") : `<div class="empty">${search ? "没有匹配的设备" : "暂无已批准共享设备。可刷新台账，或前往新增设备。"}</div>`;
  }

  function renderSignatures() {
    const search = $("search").value.trim().toLowerCase();
    const filtered = rows().filter(row => row.platform === platform && [row.displayName, row.brand, row.teamId, row.companyName].some(value => String(value || "").toLowerCase().includes(search)));
    $("signature-list").innerHTML = filtered.length ? filtered.map(row => {
      const summary = signatureSummary(row);
      return `<article class="card"><div class="card-heading"><div><h3>${escape(row.displayName)}</h3><p class="detail">${escape(teamLabel(row))}</p></div><span class="badge ${summary.tone}">${escape(summary.text)}</span></div><div class="signature-summary"><p><small>证书有效期</small>${escape(certificateExpiry(row))}</p><p><small>${row.platform === "android" ? "设备范围" : "测试 Profile"}</small>${row.platform === "android" ? "不限制设备" : escape((row.profiles || []).filter(item => row.channelEnabled?.[item.channel] === true).map(item => `${label(item.channel)}${Number.isInteger(item.deviceCount) ? ` · ${item.deviceCount} 台` : ""}`).join("、") || "待核验")}</p></div><details><summary>查看签名详情</summary><p class="detail">签名入口：<strong>${row.enabled === true ? "已启用" : row.enabled === false ? "未启用" : "需核验"}</strong> · 材料：${badge(row.materialStatus)}</p><p class="detail">最近查询：${escape(label(row.queryStatus))} · ${escape(formatDate(row.collectedAt))}</p><p class="detail">应用：${escape(row.applicationId || row.bundleId || row.bundleName || "待确认")}</p><p class="detail">材料来源：${escape(row.materialSource || row.signingSource || "待确认")} · 密钥保管归属：${escape(row.keyOwner || "未登记")}${row.keyAlias ? ` · Key alias：${escape(row.keyAlias)}` : ""}</p><p class="hint">证书主体为证书中登记的身份；密钥保管归属以配置登记为准。</p><p class="hint">品牌配置：${escape(label(row.configurationStatus))}</p><div class="materials"><strong class="detail">签名证书</strong>${materialRows(row.certificates, false)}</div>${row.platform === "android" ? '<p class="hint">Android 不使用 Profile 或设备白名单。</p>' : `<div class="materials"><strong class="detail">品牌签名</strong>${materialRows(row.profiles, true)}</div>`}<div class="materials"><strong class="detail">Jenkins 凭据</strong>${(row.credentials || []).map(item => `<p class="detail">${escape(item.role)} · ${escape(item.id)} · ${escape(label(item.status))}</p>`).join("") || '<p class="hint">尚未登记凭据</p>'}</div>${(row.errors || []).length || (row.configurationNotes || []).length ? `<ul class="error-list">${[...(row.errors || []), ...(row.configurationNotes || [])].map(message => `<li>${escape(message)}</li>`).join("")}</ul>` : ""}</details></article>`;
    }).join("") : '<div class="empty">暂无匹配品牌。请刷新台账或调整搜索。</div>';
  }
  function materialRows(items = [], profile) {
    return items.length ? items.map(item => `<div class="material-row"><div>${profile ? escape(label(item.channel)) : `证书主体：${escape(item.name || item.type || "待确认")}`}<p>${profile ? escape(item.name || "名称未知") + " · " : ""}到期：${escape(formatDate(item.expiresAt))}</p>${profile ? `<p>${escape(item.devicePolicy || "设备范围待核验")}${Number.isInteger(item.deviceCount) ? ` · ${item.deviceCount} 台设备` : ""} · ${item.approvedForReuse === true ? "已核对可复用" : "复用条件待核对"}</p>` : `<p>签发者：${escape(item.issuer || "待确认")}</p><p class="fingerprint">SHA-256：${escape(item.sha256 || "待核验")}</p>`}</div>${badge(item.status)}</div>`).join("") : '<p class="hint">暂无材料信息</p>';
  }
  function render() {
    if (view !== "signatures" && platform === "android") platform = deviceContext.platform || "ios";
    $("android-platform").hidden = view !== "signatures";
    document.body.dataset.view = view;
    $("refresh").hidden = view === "add";
    $("search").placeholder = view === "signatures" ? "搜索品牌或团队" : "搜索设备或品牌";
    $("user").textContent = state?.user || "Jenkins 用户";
    $("access").textContent = state?.canMaintain ? "可维护设备与签名" : "只读权限";
    $("updated").textContent = `台账更新：${formatDate(state?.inventory?.generatedAt)}`;
    $("backup-status").textContent = ({synced: "已同步", unchanged: "已同步", pending: "待重试", running: "进行中"})[state?.backup?.status] || "暂无备份";
    $("backup-time").textContent = state?.backup?.lastSuccess?.at ? `最近同步：${formatDate(state.backup.lastSuccess.at)}` : "";
    const pages = { devices: ["测试设备", "一套设备，为多个品牌做好准备。"], add: ["新增设备", deviceContext.mobile ? (platform === "ios" ? "获取这台 iPhone 的信息，再选择需要的品牌。" : "连接研发电脑后，设备信息会自动填入。") : "选择要添加的设备，其他步骤跟着提示走。"], signatures: ["品牌签名", "查看三端证书、归属和签名材料；Android 仅提供只读查询。"] };
    $("page-title").textContent = pages[view][0];
    $("page-description").textContent = pages[view][1];
    $("add-title").textContent = `新增 ${platformName(platform)} 设备`;
    $("device-name").maxLength = platform === "ios" ? 80 : 100;
    $("device-udid").pattern = platform === "ios" ? "(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{16})" : "[A-Za-z0-9-]{16,64}";
    $("device-udid").title = platform === "ios" ? "40 位十六进制，或 8 位-16 位的新格式 UDID" : "16–64 位字母、数字或连字符";
    $("add-view").hidden = view !== "add";
    $("device-overview").hidden = view !== "devices";
    $("ios-count").textContent = state?.inventory?.devices?.ios?.length ?? "—";
    $("harmony-count").textContent = state?.inventory?.devices?.harmony?.length ?? "—";
    $("go-add").hidden = view !== "devices";
    $("sync-all").hidden = view !== "devices";
    $("sync-all").textContent = `一键同步全部 ${platformName(platform)} 设备`;
    document.querySelector(".toolbar").hidden = view === "add";
    document.querySelectorAll('[name="platform"]').forEach(input => { input.checked = input.value === platform; });
    $("devices-view").hidden = view !== "devices";
    $("signatures-view").hidden = view !== "signatures";
    $("search-label").hidden = view === "add";
    document.querySelectorAll("[data-view]").forEach(button => { if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    (view === "add" ? $("form-platform") : document.querySelector(".toolbar")).prepend($("platform-picker"));
    $("platform-picker").hidden = view === "add" && (deviceContext.mobile || acquisition === "choose" || addStep > 1);
    brandOptions("add-brands");
    if (syncDevice && $("sync-dialog").open) brandOptions("sync-brands", syncDevice);
    renderDevices(); renderSignatures(); renderAddStep();
  }
  function renderAcquisition() {
    const entry = addStep === 1 && !["manual", "captured"].includes(acquisition);
    $("acquire-view").hidden = !entry;
    $("device-form").hidden = entry;
    $("acquire-links").hidden = addStep !== 1 || !entry;
    $("identified").hidden = !deviceContext.mobile || addStep !== 1;
    $("identified-label").textContent = deviceContext.platform === platform ? `已识别 ${platform === "ios" ? "iPhone / iPad" : "鸿蒙手机"}` : acquisition === "choose" ? "请选择设备平台" : `当前选择 ${platformName(platform)}`;
    $("choose-platform").hidden = acquisition !== "choose";
    $("capture-actions").hidden = acquisition !== "intro";
    $("capture-guide").hidden = !["guide", "wireless"].includes(acquisition);
    $("wireless-field").hidden = acquisition !== "wireless";
    $("capture-again").textContent = acquisition === "captured" ? "重新读取设备" : "返回获取设备信息";
    $("device-form-title").textContent = acquisition === "captured" ? "已获取设备信息" : "手动填写设备";
    $("device-udid").readOnly = acquisition === "captured";
    $("device-udid").type = acquisition === "captured" ? "password" : "text";
    if (acquisition === "choose") {
      $("acquire-title").textContent = "要添加哪种设备？";
      $("acquire-description").textContent = "iOS 用手机获取，鸿蒙连接研发电脑读取。";
      return;
    }
    const ios = platform === "ios", guide = ["guide", "wireless"].includes(acquisition);
    $("acquire-title").textContent = guide ? (ios ? "在 Safari 获取设备信息" : wireless ? "填写连接地址" : "用数据线连接") : ios ? (deviceContext.mobile ? "获取这台 iPhone" : "用 iPhone 获取设备信息") : (deviceContext.mobile ? "获取这台鸿蒙手机" : "读取已连接设备");
    $("acquire-description").textContent = ios ? "自动获取需使用 Safari 和 HTTPS 入口。当前未启用，可先手动填写或选择已有设备。" : wireless ? "手机与研发电脑连接同一 Wi-Fi，在开发者选项中打开无线调试。" : "手机通过 USB 连接已接入 Jenkins 的研发电脑，解锁并允许调试。";
    $("capture-start").textContent = ios ? "查看获取方式" : deviceContext.mobile ? "查看连接步骤" : "读取设备";
    const instructions = ios ? [deviceContext.mobile && !deviceContext.safari ? "当前浏览器无法进行系统采集，请改用 Safari 打开本页面。" : "在 iPhone 的 Safari 中打开本页面。", "自动采集需要启用 HTTPS 入口，当前入口未启用。", "当前可手动填写 UDID，或从已有设备库选择。"] : wireless ? ["在手机：设置 → 开发者选项 → 无线调试，查看 IP 地址和端口。", "在已接入 Jenkins 的研发电脑完成 HDC 无线连接，并在手机允许调试。", "填写已连接设备的地址，点击读取。"] : ["设置 → 关于本机，连续点 7 次软件版本，按系统提示开启开发者选项。", "开启 USB 调试，用数据线连接已接入 Jenkins 的研发电脑。", "在手机允许调试后，点击读取。"];
    $("guide-steps").innerHTML = instructions.map(text => `<li>${escape(text)}</li>`).join("");
    $("capture-read").hidden = ios;
    $("capture-read").textContent = wireless ? "读取已连接设备" : "连接后读取";
    $("wireless-toggle").hidden = ios;
    $("wireless-toggle").textContent = wireless ? "改用数据线连接" : "没有数据线？使用无线连接";
  }
  function setPlatform(value) {
    if (active || uncertain || state?.busy || addStep > 1) return;
    if (!["ios", "harmony", ...(view === "signatures" ? ["android"] : [])].includes(value)) return;
    platform = value; pending = null; addStep = 1; wireless = false; acquisition = "intro";
    $("device-udid").value = ""; $("device-name").value = ""; $("capture-target").value = "";
    $("capture-status").hidden = true; $("form-error").hidden = true; $("search").value = "";
    brandOptions("add-brands", null, false); render();
  }
  async function readDevice() {
    if (!state?.canMaintain || active || uncertain || state.busy) return;
    active = true; updateControls(); $("capture-status").hidden = false;
    $("capture-status").className = ""; $("capture-status").textContent = "正在读取，请在手机允许调试…";
    try {
      const crumb = await api("../crumbIssuer/api/json");
      if (typeof crumb.crumbRequestField !== "string" || typeof crumb.crumb !== "string") throw new Error("Jenkins 登录校验不可用，请重新登录。");
      const result = await api("capture", {method: "POST", headers: {"Content-Type": "application/json", [crumb.crumbRequestField]: crumb.crumb}, body: JSON.stringify({platform: "harmony", ...(wireless && $("capture-target").value.trim() ? {target: $("capture-target").value.trim()} : {})})});
      if (result.state !== "ready" || result.platform !== "harmony" || formError(1, "harmony", result.name || "", result.udid || "", [])) throw new Error("未能读取有效设备信息，请检查手机调试授权后重试。");
      $("device-name").value = result.name; $("device-udid").value = result.udid;
      acquisition = "captured"; $("capture-status").hidden = true;
      $("udid-help").textContent = "设备标识已读取并隐藏，仅用于本次操作，不保存在浏览器中。";
    } catch (error) {
      $("capture-status").className = "error";
      $("capture-status").textContent = error.message;
    } finally { active = false; renderAddStep(); }
  }
  $("go-add").addEventListener("click", () => { view = "add"; render(); });
  $("existing-device").addEventListener("click", () => { view = "devices"; render(); });
  $("switch-platform").addEventListener("click", () => $("platform-dialog").showModal());
  document.querySelectorAll("[data-platform]").forEach(button => button.addEventListener("click", () => { setPlatform(button.dataset.platform); $("platform-dialog").close(); }));
  $("manual-entry").addEventListener("click", () => { acquisition = "manual"; $("udid-help").textContent = "仅用于本次操作，不保存在浏览器中。"; render(); $("device-name").focus(); });
  $("capture-again").addEventListener("click", () => { $("device-udid").value = ""; $("capture-target").value = ""; wireless = false; acquisition = "intro"; render(); });
  $("capture-start").addEventListener("click", () => {
    if (platform === "ios" || deviceContext.mobile) { acquisition = "guide"; renderAddStep(); }
    else readDevice();
  });
  $("capture-read").addEventListener("click", readDevice);
  $("wireless-toggle").addEventListener("click", () => { wireless = !wireless; acquisition = wireless ? "wireless" : "guide"; renderAddStep(); });
  async function loadState() {
    const next = await api("state");
    if (!next.inventory || !Array.isArray(next.inventory.brands) || !next.inventory.devices) throw new Error("台账数据不完整，请联系 Jenkins 管理员。");
    state = next; render();
    clearTimeout(stateTimer);
    if (state.busy && !active && !uncertain) stateTimer = setTimeout(() => loadState().catch(showError), 5000);
    const warnings = [];
    if (state.busy && !active) warnings.push("已有签名任务在执行或排队，请等待结束后再操作。");
    if (isStale(state.inventory.generatedAt)) warnings.push(state.inventory.brands.length ? "当前台账超过 24 小时未更新，操作前请刷新并核对。" : "暂无台账，管理员可点击“刷新台账”获取最新状态。");
    if (Number.isInteger(state.skippedSnapshots) && state.skippedSnapshots > 0) warnings.push(`已跳过 ${state.skippedSnapshots} 份无效台账，当前显示最近可用快照，请刷新并核对。`);
    notice(warnings.join(" "), "warning");
  }
  function renderTask(task, title) {
    $("task").hidden = false;
    $("task").className = `task ${task.state === "finished" && task.result !== "SUCCESS" ? "error" : ""}`;
    $("task").innerHTML = `<strong>${escape(title)} · ${escape(taskSummary(task))}</strong>${task.build ? `<p>任务 #${escape(task.build)}</p>` : ""}${task.brands?.length ? `<ul>${task.brands.map(row => `<li>${escape(brandName(row.brand))}：${escape(label(row.status))}${row.errors?.length ? " · " + row.errors.map(escape).join("；") : ""}</li>`).join("")}</ul>` : ""}`;
    const url = task.url && safeUrl(task.url, new URL("../", base));
    if (url) { const link = document.createElement("a"); link.href = url; link.textContent = "查看 Jenkins 任务 ↗"; link.target = "_blank"; link.rel = "noopener"; $("task").append(link); }
  }
  async function poll(id, title, done) {
    try {
      const task = await api(`task?id=${encodeURIComponent(id)}`); renderTask(task, title);
      if (!["queued", "running", "finished", "cancelled"].includes(task.state)) throw new Error("任务状态无法识别，请在 Jenkins 中核对后继续。");
      if (["finished", "cancelled"].includes(task.state)) { active = false; updateControls(); await done(task); return; }
      setTimeout(() => poll(id, title, done), document.hidden ? 10000 : 3000);
    } catch (error) {
      showError(error);
      const retry = document.createElement("button"); retry.type = "button"; retry.textContent = "继续查询任务";
      retry.addEventListener("click", () => { retry.remove(); poll(id, title, done); }); $("task").append(retry);
    }
  }
  async function submit(body, title, done) {
    active = true; updateControls(); notice("");
    renderTask({ state: "queued" }, `${title}，正在提交`);
    try {
      const crumb = await api("../crumbIssuer/api/json");
      if (typeof crumb.crumbRequestField !== "string" || typeof crumb.crumb !== "string") throw new Error("Jenkins 登录校验不可用，请重新登录。");
      const result = await api("operation", { method: "POST", headers: { "Content-Type": "application/json", [crumb.crumbRequestField]: crumb.crumb }, body: JSON.stringify(body) });
      if (!Number.isInteger(result.queueId) || result.queueId < 1) { const error = new Error("提交结果不完整，请重新查询同一次提交。"); error.uncertain = true; throw error; }
      uncertain = null; poll(result.queueId, title, done);
    } catch (error) {
      active = false;
      if (error.uncertain) uncertain = body;
      if (uncertain) {
        const retry = document.createElement("button"); retry.type = "button"; retry.textContent = "查询同一次提交";
        retry.addEventListener("click", () => { retry.remove(); submit({ ...body, recoverOnly: true }, title, done); }); $("task").append(retry);
      }
      updateControls(); showError(error);
      if (error.status === 409) loadState().then(() => showError(error)).catch(showError);
    }
  }
  function operation(body) { return { ...body, requestId: requestId() }; }
  function selectedBrands(container) { return [...$(container).querySelectorAll('input[name="brand"]:checked')].map(input => input.value); }
  function preview(body, summary = "") {
    if (!state?.canMaintain || active || uncertain || state.busy) return;
    if (!body.brands.length) { notice("请至少选择一个覆盖品牌。", "warning"); return; }
    pending = null;
    submit(operation({ ...body, mode: "plan" }), "检查影响范围", async task => {
      if (!canConfirm(task)) { notice("检查未全部通过，暂不能执行。请核对各品牌结果后重新检查。", "warning"); return; }
      await loadState();
      pending = body;
      $("confirm-summary").innerHTML = summary + body.brands.map(id => { const row = rows().find(item => item.brand === id && item.platform === body.platform); return `<div class="confirmation-brand"><strong>${escape(row?.displayName || id)}</strong><small>${escape(platformName(body.platform))} · 团队：${escape(teamLabel(row || {}))}</small></div>`; }).join("");
      updateControls(); $("confirm-dialog").showModal();
    });
  }
  $("device-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!state?.canMaintain || active || uncertain || state.busy) return;
    const name = $("device-name").value.trim(), udid = $("device-udid").value.trim(), brands = selectedBrands("add-brands");
    const error = formError(addStep, platform, name, udid, brands);
    $("form-error").textContent = error; $("form-error").hidden = !error;
    if (error) return;
    if (addStep < 3) { addStep++; renderAddStep(); return; }
    preview({ operation: "devices", platform, source: "manual", brands, name, udid });
  });
  $("add-back").addEventListener("click", () => {
    if (active || uncertain || state?.busy || !state?.canMaintain) return;
    addStep = Math.max(1, addStep - 1); $("form-error").hidden = true; renderAddStep();
  });
  $("sync-form").addEventListener("submit", event => {
    event.preventDefault(); const brands = selectedBrands("sync-brands");
    if (!brands.length) { notice("请至少选择一个覆盖品牌。", "warning"); return; }
    $("sync-dialog").close(); preview({ operation: "devices", platform, source: "shared", brands, deviceIds: [syncDevice.id] });
  });
  $("apply").addEventListener("click", () => {
    if (!pending || active || uncertain || state?.busy || !state?.canMaintain) return;
    const body = pending; pending = null; $("confirm-dialog").close();
    submit(operation({ ...body, mode: "apply" }), "更新设备与签名", async task => {
      $("device-udid").value = ""; $("device-name").value = ""; addStep = 1; acquisition = deviceContext.mobile && deviceContext.platform ? "intro" : "choose"; renderAddStep();
      try { await loadState(); } catch { notice("任务已结束，但台账刷新失败。请按品牌结果核对，稍后重新加载页面。", "warning"); }
      if (task.result !== "SUCCESS" || !canConfirm(task)) notice("部分品牌可能未完成。标记“需核验”的品牌请在 Jenkins 中核对实际结果，避免重复执行。", "warning");
    });
  });
  $("refresh").addEventListener("click", () => {
    if (active || uncertain || state?.busy || !state?.canMaintain) return;
    submit(operation({ operation: "inventory", platform: "all", mode: "plan" }), "刷新台账", async task => {
      try { await loadState(); } catch (error) { showError(error); }
      if (task.result !== "SUCCESS") notice("台账刷新未全部完成，当前显示的是已有快照。请核对 Jenkins 任务。", "warning");
    });
  });
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => { view = button.dataset.view; $("search").value = ""; render(); }));
  document.querySelectorAll('[name="platform"]').forEach(input => input.addEventListener("change", () => {
    if (active || uncertain || state?.busy) return;
    setPlatform(input.value);
  }));
  $("search").addEventListener("input", () => { renderDevices(); renderSignatures(); updateControls(); });
  $("sync-all").addEventListener("click", () => {
    if (!state?.canMaintain || active || uncertain || state.busy) return;
    const pool = devices(), available = eligibleBrands(rows(), platform);
    if (!pool.length || !available.length) return;
    if (pool.length > 100) { notice("设备超过单次 100 台上限，请联系管理员分批同步。", "warning"); return; }
    const excluded = rows().filter(row => row.platform === platform && !eligible(row));
    const summary = `<p>将全部 <strong>${pool.length} 台 ${escape(platformName(platform))} 测试设备</strong>同步到以下 <strong>${available.length} 个品牌</strong>，涉及 ${new Set(available.map(row => row.team)).size} 个团队。</p><p class="hint">不受搜索筛选影响；保留品牌原有设备，已登记设备自动去重。</p>${excluded.length ? `<p class="hint">本次跳过：${excluded.map(row => `${escape(row.displayName)}（${row.configurationStatus !== "configured" ? "待接入" : "设备测试签名未启用"}）`).join("、")}。完成签名接入后可再次同步。</p>` : ""}`;
    preview({ operation: "devices", platform, source: "shared", brands: available.map(row => row.brand), deviceIds: pool.map(device => device.id) }, summary);
  });
  $("device-list").addEventListener("click", event => {
    const button = event.target.closest("[data-sync]");
    if (!button || active || uncertain || state?.busy || !state?.canMaintain) return;
    syncDevice = devices().find(row => row.id === button.dataset.sync); if (!syncDevice) return;
    $("sync-device").textContent = `${syncDevice.name} · ${syncDevice.maskedUdid}`;
    brandOptions("sync-brands", syncDevice, false); $("sync-dialog").showModal();
  });
  document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $(button.dataset.close).close()));
  $("confirm-dialog").addEventListener("close", () => { pending = null; updateControls(); });
  window.addEventListener("focus", () => { if (!active && !uncertain) loadState().catch(showError); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !active && !uncertain) loadState().catch(showError); });
  loadState().catch(error => { showError(error); $("user").textContent = "尚未连接"; });
})();
