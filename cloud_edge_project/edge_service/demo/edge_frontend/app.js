"use strict";

const state = {
  playing: false,
  busy: false,
  complete: false,
  packetCount: 80,
  processed: 0,
  selected: null,
  history: new Map(),
};

const $ = (id) => document.getElementById(id);
const stageCards = [...document.querySelectorAll(".stage-card")];
const busNodes = [...document.querySelectorAll("[data-bus]")];

function number(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function json(value) {
  return JSON.stringify(value, null, 2);
}

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
  return payload;
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 4200);
}

function setConnected(connected, label) {
  $("connectionDot").className = `state-dot ${connected ? "online" : "error"}`;
  $("connectionLabel").textContent = label;
}

function setButtons() {
  $("nextButton").disabled = state.busy || state.complete;
  $("resetButton").disabled = state.busy;
  $("speedSelect").disabled = state.busy && !state.playing;
  const play = $("playButton");
  play.disabled = state.complete || (state.busy && !state.playing);
  play.classList.toggle("playing", state.playing);
  play.lastChild.textContent = state.playing ? " 暂停演示" : " 连续演示";
}

function updateProgress() {
  $("processedCount").textContent = state.processed;
  $("railCount").textContent = state.processed;
  $("progressBar").style.width = `${Math.min(100, state.processed / state.packetCount * 100)}%`;
}

function stageState(name, status, active = false) {
  const card = document.querySelector(`[data-stage="${name}"]`);
  card.classList.toggle("active", active);
  card.querySelector(".stage-state").textContent = status;
  const bus = document.querySelector(`[data-bus="${name}"]`);
  if (bus) bus.classList.toggle("active", active);
}

function resetStageStates() {
  stageCards.forEach((card) => {
    card.classList.remove("active");
    card.querySelector(".stage-state").textContent = "等待";
  });
  busNodes.forEach((node) => node.classList.remove("active"));
}

function facts(element, pairs) {
  element.replaceChildren(...pairs.map(([term, description]) => {
    const wrapper = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = term;
    dd.textContent = description ?? "—";
    dd.title = description ?? "—";
    wrapper.append(dt, dd);
    return wrapper;
  }));
}

function drawWaveform(values) {
  if (!Array.isArray(values) || values.length < 2) {
    $("waveformPath").setAttribute("d", "M0 60H500");
    return;
  }
  const peak = Math.max(...values.map((value) => Math.abs(Number(value))), 1e-9);
  const path = values.map((value, index) => {
    const x = index / (values.length - 1) * 500;
    const y = 60 - Number(value) / peak * 47;
    return `${index ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
  $("waveformPath").setAttribute("d", path);
}

function renderInput(input) {
  const vibration = input.data.vibration;
  drawWaveform(vibration.values_preview);
  facts($("inputFacts"), [
    ["packet_id", input.packet_id],
    ["sequence", String(input.sequence_number)],
    ["sender_id", input.sender_id],
    ["bearing_id", input.bearing_id],
    ["振动采样", `${vibration.sample_count} 点 @ ${vibration.sample_rate_hz} Hz`],
    ["模块温度", `${number(input.data.bearing_module_temperature_c, 1)} °C`],
  ]);
  $("inputJson").textContent = json(input);
}

function renderPerception(perception) {
  const features = perception.features;
  const vibration = features.vibration;
  const metrics = [
    [vibration.rms, 3],
    [vibration.kurtosis, 3],
    [vibration.dominant_frequency_hz, 1],
    [features.current_relationship.current_imbalance_ratio, 4],
  ];
  [...$("featureMetrics").children].forEach((item, index) => {
    item.querySelector("strong").textContent = number(metrics[index][0], metrics[index][1]);
  });
  const quality = perception.perception_quality;
  $("qualityStatus").textContent = quality.status.toUpperCase();
  $("qualityFlags").textContent = quality.flags.length ? quality.flags.join(" / ") : "没有感知质量告警";
  $("perceptionJson").textContent = json(perception);
}

function renderModel(model) {
  const fallback = model.execution_mode === "CODE_FALLBACK";
  const badge = $("routeBadge");
  badge.className = `route-badge ${fallback ? "fallback" : "local"}`;
  badge.textContent = model.execution_mode;
  $("routeExplanation").textContent = fallback
    ? `模型路线未交付结果，本包使用开发测试规则。原因：${model.fallback_reason || "未记录"}`
    : "本包由本地模型独立分析，输出已通过结构校验。";
  facts($("modelFacts"), [
    ["queue_wait_ms", `${number(model.queue_wait_ms)} ms`],
    ["inference_latency_ms", `${number(model.inference_latency_ms)} ms`],
    ["total_latency_ms", `${number(model.total_latency_ms)} ms`],
    ["breaker_state", model.breaker_state],
    ["output_valid", String(model.output_valid)],
    ["fallback_reason", model.fallback_reason || "—"],
  ]);
  $("modelJson").textContent = json(model);
}

function renderEdge(edge) {
  const result = edge.edge_result;
  const dial = $("resultDial");
  dial.className = `result-dial ${result}`;
  $("resultIcon").textContent = result === "normal" ? "✓" : result === "warning" ? "!" : "×";
  $("edgeResultValue").textContent = result;
  $("confidenceValue").textContent = `${number(edge.confidence * 100, 1)}%`;
  $("riskValue").textContent = edge.edge_risk_level.toUpperCase();
  $("modelVersion").textContent = edge.model_version;
  $("edgeJson").textContent = json(edge);
  $("latestResult").textContent = `${result.toUpperCase()} / ${edge.edge_risk_level.toUpperCase()}`;
}

async function renderPacket(packet, animate = true) {
  state.selected = packet.sequence_number;
  $("selectedPacketTitle").textContent = `第 ${packet.sequence_number} 包 · ${packet.input.packet_id}`;
  $("requestId").textContent = `request_id  ${packet.request_id}`;
  resetStageStates();
  renderInput(packet.input);
  stageState("input", "已接收", true);
  if (animate) await delay(90);
  renderPerception(packet.perception);
  stageState("perception", "已生成", true);
  if (animate) await delay(90);
  renderModel(packet.model);
  stageState("model", packet.model.execution_mode === "LOCAL_MODEL" ? "模型完成" : "已降级", true);
  if (animate) await delay(90);
  renderEdge(packet.edge_result);
  stageState("edge", "已输出", true);
  $("latestLatency").textContent = packet.model.total_latency_ms === null
    ? "—" : `${number(packet.model.total_latency_ms)} ms`;
  renderPacketList();
}

function packetSummary(packet) {
  const perception = packet.perception.features.vibration;
  return {
    sequence_number: packet.sequence_number,
    packet_id: packet.input.packet_id,
    edge_result: packet.edge_result.edge_result,
    edge_risk_level: packet.edge_result.edge_risk_level,
    confidence: packet.edge_result.confidence,
    execution_mode: packet.model.execution_mode,
    total_latency_ms: packet.model.total_latency_ms,
    vibration_rms: perception.rms,
    kurtosis: perception.kurtosis,
  };
}

function renderPacketList() {
  const container = $("packetList");
  const items = [...state.history.values()].sort((a, b) => b.sequence_number - a.sequence_number);
  if (!items.length) {
    container.innerHTML = '<div class="empty-rail"><span class="empty-pulse"></span><p>尚未处理数据包</p><small>点击“处理下一包”开始</small></div>';
    return;
  }
  container.replaceChildren(...items.map((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `packet-item${item.sequence_number === state.selected ? " selected" : ""}`;
    button.innerHTML = `
      <span class="packet-sequence">${String(item.sequence_number).padStart(2, "0")}</span>
      <span class="packet-copy"><strong>${item.packet_id}</strong><small>RMS ${number(item.vibration_rms, 3)} · ${number(item.total_latency_ms)} ms</small></span>
      <span class="packet-result-dot ${item.edge_result}" aria-label="${item.edge_result}"></span>`;
    button.addEventListener("click", () => selectPacket(item.sequence_number));
    return button;
  }));
}

async function selectPacket(sequence) {
  if (state.busy || sequence === state.selected) return;
  try {
    const payload = await api(`/api/packets/${sequence}`);
    await renderPacket(payload.packet, false);
  } catch (error) {
    showToast(`读取第 ${sequence} 包失败：${error.message}`);
  }
}

async function processNext() {
  if (state.busy || state.complete) return false;
  state.busy = true;
  setButtons();
  stageState("input", "接收中", true);
  try {
    const payload = await api("/api/next", { method: "POST", body: "{}" });
    if (!payload.packet) {
      state.complete = true;
      return false;
    }
    const packet = payload.packet;
    state.processed = packet.sequence_number;
    state.complete = Boolean(payload.complete);
    state.history.set(packet.sequence_number, packetSummary(packet));
    updateProgress();
    await renderPacket(packet, true);
    return true;
  } catch (error) {
    state.playing = false;
    setConnected(false, "处理异常");
    showToast(`数据包处理失败：${error.message}`);
    return false;
  } finally {
    state.busy = false;
    setButtons();
  }
}

async function playLoop() {
  state.playing = !state.playing;
  setButtons();
  if (!state.playing) return;
  while (state.playing && !state.complete) {
    const processed = await processNext();
    if (!processed) break;
    await delay(Number($("speedSelect").value));
  }
  state.playing = false;
  setButtons();
}

async function resetDemo() {
  if (state.busy) return;
  state.playing = false;
  state.busy = true;
  setButtons();
  try {
    const status = await api("/api/reset", { method: "POST", body: "{}" });
    state.complete = false;
    state.processed = 0;
    state.selected = null;
    state.history.clear();
    applyStatus(status);
    resetStageStates();
    $("selectedPacketTitle").textContent = "等待第 1 包";
    $("requestId").textContent = "request_id —";
    $("latestLatency").textContent = "—";
    $("latestResult").textContent = "等待数据";
    $("edgeResultValue").textContent = "等待结果";
    $("resultDial").className = "result-dial waiting";
    drawWaveform([]);
  } catch (error) {
    showToast(`重置失败：${error.message}`);
  } finally {
    state.busy = false;
    setButtons();
  }
}

function applyStatus(status) {
  state.packetCount = status.packet_count;
  state.processed = status.processed_packets;
  state.complete = status.complete;
  state.history = new Map(status.history.map((item) => [item.sequence_number, item]));
  $("modelMode").textContent = status.model_mode === "real" ? "LOCAL_MODEL" : "CODE_FALLBACK";
  $("modeNotice").textContent = status.model_mode === "real"
    ? "真实模型演示：每包通过本地模型服务独立分析。"
    : "开发测试：感知模块为真实实现；模型不可用时使用 edge_rule_test_v1，不代表真实诊断结论。";
  updateProgress();
  renderPacketList();
}

async function initialize() {
  setButtons();
  try {
    const status = await api("/api/status");
    applyStatus(status);
    setConnected(true, "服务在线");
    if (status.history.length) await selectPacket(status.history.at(-1).sequence_number);
  } catch (error) {
    setConnected(false, "无法连接");
    showToast(`无法连接演示服务：${error.message}`);
  }
}

$("nextButton").addEventListener("click", processNext);
$("playButton").addEventListener("click", playLoop);
$("resetButton").addEventListener("click", resetDemo);
initialize();
