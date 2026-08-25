/* 共享工具：导航注入、API 客户端、SSE 实时流、格式化 */

// ---------- 导航 ----------

const NAV_PAGES = [
  { href: "index.html", icon: "◉", label: "总览大屏" },
  { href: "edge-health.html", icon: "▤", label: "边缘节点" },
  { href: "diagnosis-demo.html", icon: "⚡", label: "诊断演示" },
  { href: "topology.html", icon: "⬡", label: "调度拓扑" },
  { href: "arbitration.html", icon: "⚖", label: "一致性与仲裁" },
  { href: "analysis.html", icon: "▣", label: "全局分析" },
];

function injectNav() {
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;
  const here = location.pathname.split("/").pop() || "index.html";
  sidebar.innerHTML =
    '<div class="brand"><div class="title">智能运维协作平台</div>' +
    '<div class="subtitle">Cloud-Edge Collaboration</div></div>' +
    "<nav>" +
    NAV_PAGES.map(
      (p) =>
        '<a href="' + p.href + '" class="' + (p.href === here ? "active" : "") + '">' +
        '<span class="icon">' + p.icon + "</span>" + p.label + "</a>"
    ).join("") +
    "</nav>" +
    '<div class="footer">Edge 8001/8002 · Sch 8003<br>Cloud 8004 · Summary 8006<br>MQTT 1883</div>';
}

// ---------- API 客户端（经网关代理，无跨域问题） ----------

const Api = {
  async request(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch("/api/" + path, opts);
    const text = await resp.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
    if (!resp.ok) {
      const err = new Error((data && (data.message || data.error_code || data.error)) || ("HTTP " + resp.status));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  },
  get(path) { return this.request("GET", path); },
  post(path, body) { return this.request("POST", path, body === undefined ? {} : body); },
};

// ---------- SSE 实时流（网关桥接 MQTT） ----------

/**
 * 订阅实时事件。
 * handlers: { "device-result": fn(data), "suggestion": fn, "input-packet": fn, "mqtt-status": fn }
 * 返回 { close(), connected() }
 */
function connectEvents(handlers) {
  let es = null;
  let alive = false;
  const statusHandler = handlers["mqtt-status"];

  function open() {
    es = new EventSource("/api/events");
    Object.keys(handlers).forEach((name) => {
      if (name === "mqtt-status") return;
      es.addEventListener(name, (ev) => {
        alive = true;
        try {
          handlers[name](JSON.parse(ev.data));
        } catch (e) { console.error("event handler error:", name, e); }
      });
    });
    es.addEventListener("mqtt-status", (ev) => {
      try {
        const d = JSON.parse(ev.data);
        alive = !!d.connected;
        if (statusHandler) statusHandler(d);
      } catch { /* ignore */ }
    });
    es.onerror = () => {
      alive = false;
      if (statusHandler) statusHandler({ connected: false });
      // EventSource 会自动重连
    };
  }
  open();

  return {
    close() { if (es) es.close(); },
    connected() { return alive; },
  };
}

// ---------- 轮询工具 ----------

function startPolling(fn, intervalMs) {
  let stopped = false;
  let timer = null;
  (async function loop() {
    while (!stopped) {
      try { await fn(); } catch (e) { console.warn("poll error:", e.message); }
      await new Promise((r) => { timer = setTimeout(r, intervalMs); });
    }
  })();
  return { stop() { stopped = true; if (timer) clearTimeout(timer); } };
}

// ---------- 格式化 ----------

const fmt = {
  /** 纳秒时间戳 → HH:MM:SS.mmm（本地时区） */
  timeNs(ns) {
    if (ns === null || ns === undefined) return "-";
    return fmt.timeMs(ns / 1e6);
  },
  timeMs(ms) {
    if (ms === null || ms === undefined) return "-";
    const d = new Date(ms);
    const p = (n, w) => String(n).padStart(w || 2, "0");
    return (
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()) + "." + p(d.getMilliseconds(), 3)
    );
  },
  timeS(s) {
    if (s === null || s === undefined) return "-";
    return fmt.timeMs(s * 1000);
  },
  dateMs(ms) {
    if (ms === null || ms === undefined) return "-";
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    return (
      d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds())
    );
  },
  pct(v, digits) {
    if (v === null || v === undefined) return "-";
    return (v * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  },
  num(v, digits) {
    if (v === null || v === undefined) return "-";
    if (typeof v !== "number") return String(v);
    return Number.isInteger(v) ? String(v) : v.toFixed(digits === undefined ? 2 : digits);
  },
  ms(v) {
    if (v === null || v === undefined) return "-";
    return typeof v === "number" ? (v >= 1000 ? (v / 1000).toFixed(2) + " s" : v.toFixed(1) + " ms") : String(v);
  },
  /** 诊断状态 → 徽章 class */
  stateClass(state) {
    switch (String(state || "").toLowerCase()) {
      case "fault": case "abnormal": return "danger";
      case "warning": case "warn": return "warn";
      case "normal": case "ok": return "ok";
      default: return "muted";
    }
  },
  stateText(state) {
    switch (String(state || "").toLowerCase()) {
      case "fault": return "故障";
      case "warning": case "warn": return "预警";
      case "normal": return "正常";
      default: return state === null || state === undefined ? "-" : String(state);
    }
  },
  priorityClass(p) {
    switch (String(p || "").toLowerCase()) {
      case "high": case "urgent": case "p0": return "danger";
      case "medium": case "p1": return "warn";
      default: return "muted";
    }
  },
  escape(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  },
};

// ---------- 小组件 ----------

function badge(text, cls, pulse) {
  return '<span class="badge ' + cls + '"><span class="dot' + (pulse ? " pulse" : "") + '"></span>' + fmt.escape(text) + "</span>";
}

function toast(msg, cls) {
  let wrap = document.querySelector(".toast-wrap");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.className = "toast-wrap";
    document.body.appendChild(wrap);
  }
  const el = document.createElement("div");
  el.className = "toast " + (cls || "");
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function el(id) { return document.getElementById(id); }

function showEmpty(container, msg) {
  container.innerHTML = '<div class="empty">' + fmt.escape(msg || "暂无数据") + "</div>";
}

function jsonDetails(obj, title) {
  return (
    '<details class="json"><summary>' + fmt.escape(title || "查看原始 JSON") + "</summary><pre>" +
    fmt.escape(JSON.stringify(obj, null, 2)) + "</pre></details>"
  );
}

// ---------- 页面初始化 ----------

document.addEventListener("DOMContentLoaded", injectNav);
