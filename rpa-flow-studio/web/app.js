"use strict";

const state = {
  modules: [],
  moduleMap: new Map(),
  customModules: [],
  configs: [],
  configId: "default",
  config: null,
  selectedStepId: null,
  dragStepId: null,
  run: null,
  hiddenLogs: false,
  logCollapsed: false,
  customDraft: null,
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `请求失败：${response.status}`);
  }
  return data;
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function newId() {
  return `step-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 2800);
}

function moduleInitialParams(module) {
  const params = {};
  for (const field of module.fields) params[field.name] = deepClone(field.default);
  return params;
}

function moduleIcon(category) {
  return ({ HIS: "H", "窗口": "窗", "截图": "图", OCR: "识", "通用": "通" })[category] || "·";
}

function renderPalette() {
  const palette = $("#module-palette");
  palette.innerHTML = "";
  const groups = new Map();
  for (const module of state.modules) {
    if (!groups.has(module.category)) groups.set(module.category, []);
    groups.get(module.category).push(module);
  }
  for (const [category, modules] of groups) {
    const title = document.createElement("div");
    title.className = "category-title";
    title.textContent = category;
    palette.appendChild(title);
    for (const module of modules) {
      const button = document.createElement("button");
      button.className = "module-card";
      button.title = module.description;
      button.innerHTML = `<span class="module-icon">${moduleIcon(category)}</span><span><strong>${escapeHtml(module.name)}</strong><small>${escapeHtml(module.description)}</small></span>`;
      button.addEventListener("click", () => addStep(module));
      palette.appendChild(button);
    }
  }
  if (state.customModules.length) {
    const title = document.createElement("div");
    title.className = "category-title";
    title.textContent = "自定义";
    palette.prepend(title);
    for (const custom of [...state.customModules].reverse()) {
      const base = state.moduleMap.get(custom.baseType);
      if (!base) continue;
      const button = document.createElement("button");
      button.className = "module-card";
      button.title = custom.description || base.description;
      button.innerHTML = `<span class="module-icon">自</span><span><strong>${escapeHtml(custom.name)}</strong><small>${escapeHtml(custom.description || base.name)}</small></span>`;
      button.addEventListener("click", () => addCustomStep(custom));
      palette.insertBefore(button, title.nextSibling);
    }
  }
  $("#module-count").textContent = state.modules.length + state.customModules.length;
}

function addStep(module) {
  const step = {
    id: newId(), type: module.type, name: module.name, enabled: true,
    params: moduleInitialParams(module),
  };
  state.config.steps.push(step);
  state.selectedStepId = step.id;
  renderFlow();
  renderInspector();
  showToast(`已添加：${module.name}`);
}

function addCustomStep(custom) {
  const base = state.moduleMap.get(custom.baseType);
  if (!base) return showToast("这个自定义模块依赖的基础动作不存在", true);
  const step = {
    id: newId(),
    type: custom.baseType,
    name: custom.name,
    enabled: true,
    params: { ...moduleInitialParams(base), ...deepClone(custom.params) },
    templateId: custom.id,
  };
  state.config.steps.push(step);
  state.selectedStepId = step.id;
  switchPage("flow");
  renderFlow();
  renderInspector();
  showToast(`已添加自定义模块：${custom.name}`);
}

function stepIndex(stepId) {
  return state.config.steps.findIndex((step) => step.id === stepId);
}

function selectStep(stepId) {
  state.selectedStepId = stepId;
  renderFlow();
  renderInspector();
}

function moveStep(stepId, delta) {
  const index = stepIndex(stepId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= state.config.steps.length) return;
  const [step] = state.config.steps.splice(index, 1);
  state.config.steps.splice(target, 0, step);
  renderFlow();
}

function removeStep(stepId) {
  const index = stepIndex(stepId);
  if (index < 0) return;
  state.config.steps.splice(index, 1);
  if (state.selectedStepId === stepId) state.selectedStepId = null;
  renderFlow();
  renderInspector();
}

function renderFlow() {
  const list = $("#flow-list");
  list.innerHTML = "";
  $("#add-hint").hidden = state.config.steps.length !== 0;
  state.config.steps.forEach((step, index) => {
    const module = state.moduleMap.get(step.type);
    const item = document.createElement("div");
    item.className = `flow-step${step.id === state.selectedStepId ? " selected" : ""}${step.enabled ? "" : " disabled"}`;
    item.draggable = true;
    item.dataset.id = step.id;
    item.innerHTML = `
      <div class="drag-handle" title="拖动排序">⋮⋮</div>
      <div class="step-number">${String(index + 1).padStart(2, "0")}</div>
      <div class="step-copy"><strong>${escapeHtml(step.name)}</strong><small>${escapeHtml(module?.description || step.type)}</small></div>
      <div class="step-actions">
        <button class="icon-button up" title="上移">↑</button>
        <button class="icon-button down" title="下移">↓</button>
        <button class="icon-button delete" title="删除">×</button>
      </div>`;
    item.addEventListener("click", () => selectStep(step.id));
    item.querySelector(".up").addEventListener("click", (event) => { event.stopPropagation(); moveStep(step.id, -1); });
    item.querySelector(".down").addEventListener("click", (event) => { event.stopPropagation(); moveStep(step.id, 1); });
    item.querySelector(".delete").addEventListener("click", (event) => { event.stopPropagation(); removeStep(step.id); });
    item.addEventListener("dragstart", () => { state.dragStepId = step.id; item.classList.add("dragging"); });
    item.addEventListener("dragend", () => { state.dragStepId = null; item.classList.remove("dragging"); });
    item.addEventListener("dragover", (event) => event.preventDefault());
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      if (!state.dragStepId || state.dragStepId === step.id) return;
      const from = stepIndex(state.dragStepId);
      const to = stepIndex(step.id);
      const [moved] = state.config.steps.splice(from, 1);
      state.config.steps.splice(to, 0, moved);
      renderFlow();
    });
    list.appendChild(item);
  });
  $("#run-to-button").disabled = !state.selectedStepId || state.run?.status === "running";
}

function createField(field, value, onChange) {
  const label = document.createElement("label");
  label.className = field.type === "boolean" ? "checkbox-field" : "form-field";
  if (field.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    input.addEventListener("change", () => onChange(input.checked));
    label.append(input, document.createTextNode(field.label));
    return label;
  }
  const caption = document.createElement("span");
  caption.textContent = field.label;
  let input;
  if (field.type === "stringList") {
    input = document.createElement("textarea");
    input.value = Array.isArray(value) ? value.join("\n") : String(value || "");
    input.addEventListener("input", () => onChange(input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)));
  } else if (field.type === "select") {
    input = document.createElement("select");
    for (const optionValue of field.options || []) {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = optionValue;
      option.selected = optionValue === value;
      input.appendChild(option);
    }
    input.addEventListener("change", () => onChange(input.value));
  } else {
    input = document.createElement("input");
    input.type = field.type === "number" ? "number" : "text";
    input.value = value ?? "";
    for (const key of ["min", "max", "step"]) if (field[key] !== undefined) input[key] = field[key];
    input.addEventListener("input", () => onChange(field.type === "number" ? Number(input.value) : input.value));
  }
  label.append(caption, input);
  return label;
}

function renderInspector() {
  const inspector = $("#inspector");
  inspector.innerHTML = "";
  const step = state.config.steps.find((item) => item.id === state.selectedStepId);
  if (!step) {
    inspector.innerHTML = `<div class="inspector-empty">请选择一个流程步骤。<br>这里可以修改操作对象、输入参数和执行开关。</div>`;
    $("#run-to-button").disabled = true;
    return;
  }
  const module = state.moduleMap.get(step.type);
  const summary = document.createElement("div");
  summary.className = "module-summary";
  summary.innerHTML = `<strong>${escapeHtml(module.name)}</strong><p>${escapeHtml(module.description)}</p>`;
  inspector.appendChild(summary);

  inspector.appendChild(createField({ label: "步骤名称", type: "text" }, step.name, (value) => { step.name = value; renderFlow(); }));
  inspector.appendChild(createField({ label: "启用这个步骤", type: "boolean" }, step.enabled, (value) => { step.enabled = value; renderFlow(); }));
  const divider = document.createElement("div"); divider.className = "inspector-divider"; inspector.appendChild(divider);
  for (const field of module.fields) {
    if (!(field.name in step.params)) step.params[field.name] = deepClone(field.default);
    inspector.appendChild(createField(field, step.params[field.name], (value) => { step.params[field.name] = value; }));
  }
  const divider2 = document.createElement("div"); divider2.className = "inspector-divider"; inspector.appendChild(divider2);
  const remove = document.createElement("button");
  remove.className = "inspector-danger";
  remove.textContent = "删除此步骤";
  remove.addEventListener("click", () => removeStep(step.id));
  inspector.appendChild(remove);
  $("#run-to-button").disabled = state.run?.status === "running";
}

function syncHeaderInputs() {
  $("#workflow-name").value = state.config.name || "";
  $("#hospitalization-number").value = state.config.variables?.hospitalization_number || "";
}

function readHeaderInputs() {
  state.config.name = $("#workflow-name").value.trim();
  state.config.variables ||= {};
  state.config.variables.hospitalization_number = $("#hospitalization-number").value.trim();
}

async function saveConfig() {
  readHeaderInputs();
  const data = await api("/api/config", { method: "POST", body: JSON.stringify({ id: state.configId, config: state.config }) });
  state.configId = data.id;
  state.config = data.config;
  state.configs = data.configs;
  syncHeaderInputs();
  renderFlow();
  renderConfigPage();
  showToast("配置已保存");
}

async function reloadConfig() {
  const data = await api(`/api/config?id=${encodeURIComponent(state.configId)}`);
  state.configId = data.id;
  state.config = data.config;
  state.selectedStepId = null;
  syncHeaderInputs();
  renderFlow();
  renderInspector();
  showToast("已重新加载已保存配置");
}

async function loadConfig(configId) {
  const data = await api(`/api/config?id=${encodeURIComponent(configId)}`);
  state.configId = data.id;
  state.config = data.config;
  state.selectedStepId = null;
  syncHeaderInputs();
  renderFlow();
  renderInspector();
  renderConfigPage();
  switchPage("flow");
  showToast(`已载入：${state.config.name}`);
}

async function deleteConfig(configId) {
  const target = state.configs.find((item) => item.id === configId);
  if (!window.confirm(`确定删除配置“${target?.name || configId}”吗？`)) return;
  const data = await api(`/api/config?id=${encodeURIComponent(configId)}`, { method: "DELETE" });
  state.configs = data.configs;
  if (state.configId === configId) {
    await loadConfig(state.configs[0].id);
    switchPage("configs");
  } else renderConfigPage();
  showToast("配置已删除");
}

function renderConfigPage() {
  const list = $("#config-list");
  list.innerHTML = state.configs.map((config) => {
    const date = new Date(config.updatedAt * 1000).toLocaleString("zh-CN", { hour12: false });
    return `<article class="config-card${config.id === state.configId ? " active-config" : ""}">
      <h3>${escapeHtml(config.name)}</h3>
      <div class="config-meta">${config.stepCount} 个步骤 · ${escapeHtml(date)}</div>
      <div class="config-id">${escapeHtml(config.id)}</div>
      <div class="card-actions">
        <button class="card-button edit-config" data-id="${escapeHtml(config.id)}">编辑</button>
        <button class="card-button danger delete-config" data-id="${escapeHtml(config.id)}">删除</button>
      </div>
    </article>`;
  }).join("");
  list.querySelectorAll(".edit-config").forEach((button) => button.addEventListener("click", () => loadConfig(button.dataset.id).catch((error) => showToast(error.message, true))));
  list.querySelectorAll(".delete-config").forEach((button) => button.addEventListener("click", () => deleteConfig(button.dataset.id).catch((error) => showToast(error.message, true))));
  renderCustomModuleList();
}

function switchPage(page) {
  document.querySelectorAll(".page-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.page === page));
  $("#page-flow").classList.toggle("active", page === "flow");
  $("#page-configs").classList.toggle("active", page === "configs");
  if (page === "configs") renderConfigPage();
}

function openModuleDialog(existing = null) {
  const dialog = $("#module-dialog");
  const select = $("#custom-module-base");
  select.innerHTML = state.modules.map((module) => `<option value="${escapeHtml(module.type)}">${escapeHtml(module.category)} · ${escapeHtml(module.name)}</option>`).join("");
  state.customDraft = existing ? deepClone(existing) : {
    id: null,
    name: "",
    description: "",
    baseType: state.modules[0]?.type || "",
    params: {},
  };
  $("#custom-module-name").value = state.customDraft.name;
  $("#custom-module-description").value = state.customDraft.description;
  select.value = state.customDraft.baseType;
  renderCustomModuleFields();
  dialog.showModal();
}

function renderCustomModuleFields() {
  const container = $("#custom-module-fields");
  container.innerHTML = "";
  const base = state.moduleMap.get($("#custom-module-base").value);
  if (!base) return;
  state.customDraft.baseType = base.type;
  const defaults = moduleInitialParams(base);
  state.customDraft.params = { ...defaults, ...(state.customDraft.params || {}) };
  for (const field of base.fields) {
    container.appendChild(createField(field, state.customDraft.params[field.name], (value) => { state.customDraft.params[field.name] = value; }));
  }
}

async function saveCustomModule() {
  state.customDraft.name = $("#custom-module-name").value.trim();
  state.customDraft.description = $("#custom-module-description").value.trim();
  state.customDraft.baseType = $("#custom-module-base").value;
  if (!state.customDraft.name) throw new Error("模块名称不能为空");
  const data = await api("/api/custom-modules", {
    method: "POST",
    body: JSON.stringify({ module: state.customDraft }),
  });
  state.customModules = data.modules;
  $("#module-dialog").close();
  renderPalette();
  renderConfigPage();
  showToast("自定义模块已保存");
}

async function deleteCustomModule(moduleId) {
  const target = state.customModules.find((item) => item.id === moduleId);
  if (!window.confirm(`确定删除自定义模块“${target?.name || moduleId}”吗？`)) return;
  const data = await api(`/api/custom-modules?id=${encodeURIComponent(moduleId)}`, { method: "DELETE" });
  state.customModules = data.modules;
  renderPalette();
  renderConfigPage();
  showToast("自定义模块已删除");
}

function renderCustomModuleList() {
  const list = $("#custom-module-list");
  if (!state.customModules.length) {
    list.innerHTML = `<div class="empty-library">还没有自定义模块。点击“新增模块”，从已有安全动作创建自己的模块模板。</div>`;
    return;
  }
  list.innerHTML = state.customModules.map((module) => {
    const base = state.moduleMap.get(module.baseType);
    return `<article class="custom-module-item">
      <h3>${escapeHtml(module.name)}</h3>
      <div class="config-meta">基础动作：${escapeHtml(base?.name || module.baseType)}</div>
      <div class="config-id">${escapeHtml(module.description || "未填写说明")}</div>
      <div class="card-actions">
        <button class="card-button edit-module" data-id="${escapeHtml(module.id)}">修改</button>
        <button class="card-button danger delete-module" data-id="${escapeHtml(module.id)}">删除</button>
      </div>
    </article>`;
  }).join("");
  list.querySelectorAll(".edit-module").forEach((button) => button.addEventListener("click", () => openModuleDialog(state.customModules.find((item) => item.id === button.dataset.id))));
  list.querySelectorAll(".delete-module").forEach((button) => button.addEventListener("click", () => deleteCustomModule(button.dataset.id).catch((error) => showToast(error.message, true))));
}

function openConfigDialog() {
  $("#new-config-name").value = `${state.config.name} - 副本`;
  $("#clone-current-config").checked = true;
  $("#config-dialog").showModal();
}

async function createConfig() {
  const name = $("#new-config-name").value.trim();
  if (!name) throw new Error("配置名称不能为空");
  const clone = $("#clone-current-config").checked;
  const config = clone ? deepClone(state.config) : {
    schemaVersion: 1,
    name,
    variables: { hospitalization_number: "ZY26031245" },
    settings: { outputDir: "../output" },
    steps: [{
      id: newId(),
      type: "window.activate",
      name: "连接 HIS 窗口",
      enabled: true,
      params: { windowTitle: "医院信息系统（HIS）" },
    }],
  };
  config.name = name;
  const data = await api("/api/config", { method: "POST", body: JSON.stringify({ config }) });
  state.configId = data.id;
  state.config = data.config;
  state.configs = data.configs;
  state.selectedStepId = null;
  $("#config-dialog").close();
  syncHeaderInputs();
  renderFlow();
  renderInspector();
  renderConfigPage();
  switchPage("flow");
  showToast("新配置已创建");
}

function toggleLogPanel() {
  state.logCollapsed = !state.logCollapsed;
  $(".console-panel").classList.toggle("collapsed", state.logCollapsed);
  $("#page-flow").classList.toggle("log-collapsed", state.logCollapsed);
  $("#toggle-log-button").textContent = state.logCollapsed ? "展开日志" : "收起日志";
}

async function startRun(untilStepId = null) {
  if (state.run?.status === "running") return;
  readHeaderInputs();
  state.hiddenLogs = false;
  const variables = { hospitalization_number: state.config.variables.hospitalization_number };
  const data = await api("/api/run", {
    method: "POST",
    body: JSON.stringify({ config: state.config, variables, untilStepId }),
  });
  state.run = { status: "running", runId: data.runId, logs: [] };
  renderRun();
  showToast(untilStepId ? "正在运行到选中步骤" : "任务已开始");
}

async function cancelRun() {
  if (!["running", "cancelling"].includes(state.run?.status)) return;
  const data = await api("/api/run/cancel", { method: "POST", body: "{}" });
  state.run = data.run;
  renderRun();
}

function renderRun() {
  const run = state.run || { status: "idle", logs: [] };
  const strip = $("#status-strip");
  strip.className = `status-strip ${run.status}`;
  const labels = {
    idle: "Agent 已连接，当前空闲",
    running: "RPA 正在操作本机桌面，请勿移动鼠标或锁屏",
    succeeded: "任务执行成功",
    cancelling: "正在安全取消任务，请稍候",
    cancelled: "任务已取消",
    failed: "任务执行失败，详细原因请查看下方运行日志",
  };
  $("#status-text").textContent = labels[run.status] || run.status;
  const active = ["running", "cancelling"].includes(run.status);
  $("#run-button").hidden = active;
  $("#cancel-run-button").hidden = !active;
  $("#cancel-run-button").disabled = run.status === "cancelling";
  $("#save-button").disabled = active;
  $("#run-to-button").disabled = active || !state.selectedStepId;

  if (["failed", "cancelled"].includes(run.status) && state.logCollapsed) {
    state.logCollapsed = false;
    $(".console-panel").classList.remove("collapsed");
    $("#page-flow").classList.remove("log-collapsed");
    $("#toggle-log-button").textContent = "收起日志";
  }

  const log = $("#run-log");
  if (state.hiddenLogs || !run.logs?.length) {
    log.innerHTML = `<div class="log-placeholder">${state.hiddenLogs ? "日志显示已清空；新日志到达后会重新显示" : "尚未运行任务"}</div>`;
  } else {
    log.innerHTML = run.logs.map((item) => `<div class="log-row ${escapeHtml(item.level)}"><span class="log-time">${escapeHtml(item.time)}</span><span class="log-level">${escapeHtml(item.level)}</span><span>${escapeHtml(item.message)}</span></div>`).join("");
    log.scrollTop = log.scrollHeight;
  }
  const result = $("#run-result");
  if (run.result) {
    result.hidden = false;
    const paths = run.result.markedScreenshots?.length ? run.result.markedScreenshots : run.result.screenshots;
    result.textContent = paths?.length ? `输出：${paths.join("  |  ")}` : `输出目录：${run.result.outputDir}`;
  } else result.hidden = true;
}

async function pollRun() {
  try {
    const data = await api("/api/run");
    const previousCount = state.run?.logs?.length || 0;
    state.run = data.run;
    if ((state.run.logs?.length || 0) > previousCount) state.hiddenLogs = false;
    renderRun();
  } catch (error) {
    $("#status-text").textContent = `Agent 连接失败：${error.message}`;
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

async function init() {
  try {
    const data = await api("/api/bootstrap");
    state.modules = data.modules;
    state.moduleMap = new Map(state.modules.map((module) => [module.type, module]));
    state.customModules = data.customModules || [];
    state.configs = data.configs || [];
    state.configId = data.configId || "default";
    state.config = data.config;
    state.run = data.run;
    renderPalette();
    syncHeaderInputs();
    renderFlow();
    renderInspector();
    renderRun();
    renderConfigPage();
  } catch (error) {
    showToast(error.message, true);
  }

  $("#workflow-name").addEventListener("input", () => { state.config.name = $("#workflow-name").value; });
  $("#hospitalization-number").addEventListener("input", () => { state.config.variables.hospitalization_number = $("#hospitalization-number").value; });
  $("#save-button").addEventListener("click", () => saveConfig().catch((error) => showToast(error.message, true)));
  $("#reload-button").addEventListener("click", () => reloadConfig().catch((error) => showToast(error.message, true)));
  $("#run-button").addEventListener("click", () => startRun().catch((error) => showToast(error.message, true)));
  $("#cancel-run-button").addEventListener("click", () => cancelRun().catch((error) => showToast(error.message, true)));
  $("#run-to-button").addEventListener("click", () => startRun(state.selectedStepId).catch((error) => showToast(error.message, true)));
  $("#clear-log-button").addEventListener("click", () => { state.hiddenLogs = true; renderRun(); });
  $("#toggle-log-button").addEventListener("click", toggleLogPanel);
  document.querySelectorAll(".page-tab").forEach((tab) => tab.addEventListener("click", () => switchPage(tab.dataset.page)));
  $("#new-module-button").addEventListener("click", () => openModuleDialog());
  $("#new-module-config-button").addEventListener("click", () => openModuleDialog());
  $("#custom-module-base").addEventListener("change", () => {
    state.customDraft.params = {};
    renderCustomModuleFields();
  });
  $("#module-form").addEventListener("submit", (event) => {
    event.preventDefault();
    saveCustomModule().catch((error) => showToast(error.message, true));
  });
  $("#new-config-button").addEventListener("click", openConfigDialog);
  $("#config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    createConfig().catch((error) => showToast(error.message, true));
  });
  document.querySelectorAll(".cancel-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  setInterval(pollRun, 900);
}

document.addEventListener("DOMContentLoaded", init);
