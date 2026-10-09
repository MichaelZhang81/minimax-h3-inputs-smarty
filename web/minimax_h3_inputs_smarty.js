// MiniMaxH3 素材槽位控件（minimax-h3-inputs-smarty 前端）。
// 面板布局（自上而下）：提示词（自绘 textarea，吸收窗口多余高度）、目标时长、
// 15 个素材槽位紧凑面板（行间无空隙），每行：
//   [标签][下拉列表(input目录)][自由输入(文件名/URL)][上传][刷新][预览]
// 预览：右侧磁吸悬浮窗（可拖动、可关闭），图片/视频/音频按槽位类型渲染。
// 真实 STRING 控件隐藏但继续持有/序列化槽位值（H3MediaBoard 同款模式）。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_ID = "MiniMaxH3_InputBundle";
const PANEL_TYPE = "MMXH3_SLOT_PANEL";
const PROMPT_TYPE = "MMXH3_PROMPT";
const FILES_ROUTE = "/minimax-h3-inputs-smarty/files";
const VIEW_ROUTE = "/minimax-h3-inputs-smarty/view";
const ACCEPT_OF = {
  image: "image/*",
  video: "video/*",
  audio: "audio/*",
};
const ROW_HEIGHT = 24;
const PROMPT_MIN_HEIGHT = 140; // ≈ 8 行

function collectSlots(nodeData) {
  const out = [];
  for (const section of ["required", "optional"]) {
    const entries = nodeData?.input?.[section] ?? {};
    for (const [name, spec] of Object.entries(entries)) {
      if (Array.isArray(spec) && spec[1] && spec[1].mmxh3_kind) {
        out.push({
          name,
          kind: spec[1].mmxh3_kind,
          label: spec[1].mmxh3_label || name,
          files: Array.isArray(spec[1].mmxh3_files) ? spec[1].mmxh3_files.slice() : [],
        });
      }
    }
  }
  return out;
}

function hideWidget(node, name) {
  const w = node.widgets?.find((x) => x.name === name);
  if (!w) return;
  w.hidden = true;
  if (!w.options) w.options = {};
  w.options.hidden = true;
}

function slotWidget(node, name) {
  return node.widgets?.find((x) => x.name === name);
}

function setSlotValue(node, name, value) {
  const w = slotWidget(node, name);
  if (w) {
    w.value = value;
    w.callback?.(value);
  }
  node.graph?.setDirtyCanvas?.(true, true);
}

// ---------- 右侧磁吸预览窗（每节点一个，可拖动/关闭，内容随槽位切换） ----------

function viewSrc(value) {
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return value; // URL 直接预览（远程资源）
  return VIEW_ROUTE + "?value=" + encodeURIComponent(value);
}

function ensurePreviewWindow(node) {
  if (node.__mmxh3Preview) return node.__mmxh3Preview;
  const win = document.createElement("div");
  win.style.cssText =
    "position:fixed;top:90px;right:14px;width:340px;z-index:99999;background:#1e1e1e;" +
    "border:1px solid #555;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.55);" +
    "overflow:hidden;display:none;";

  const head = document.createElement("div");
  head.style.cssText =
    "display:flex;align-items:center;gap:6px;background:#2a2a2a;padding:4px 8px;" +
    "cursor:move;user-select:none;border-bottom:1px solid #444;";
  const title = document.createElement("span");
  title.style.cssText =
    "flex:1;color:var(--fg-color,#ddd);font-size:12px;white-space:nowrap;overflow:hidden;" +
    "text-overflow:ellipsis;";
  const close = document.createElement("button");
  close.textContent = "✕";
  close.title = "关闭预览";
  close.type = "button";
  close.style.cssText =
    "flex:0 0 auto;background:none;border:none;color:#ccc;cursor:pointer;font-size:13px;" +
    "padding:0 2px;";
  close.addEventListener("click", () => {
    win.style.display = "none";
  });
  head.appendChild(title);
  head.appendChild(close);
  win.appendChild(head);

  const body = document.createElement("div");
  body.style.cssText =
    "height:270px;display:flex;align-items:center;justify-content:center;padding:8px;" +
    "box-sizing:border-box;";
  win.appendChild(body);

  // 拖动（磁吸窗在右缘，拖动后转为自由定位）
  head.addEventListener("pointerdown", (ev) => {
    if (ev.target !== head && ev.target !== title) return;
    const rect = win.getBoundingClientRect();
    const offX = ev.clientX - rect.left;
    const offY = ev.clientY - rect.top;
    win.style.right = "auto";
    win.style.left = rect.left + "px";
    win.style.top = rect.top + "px";
    const move = (e) => {
      win.style.left = Math.max(0, e.clientX - offX) + "px";
      win.style.top = Math.max(0, e.clientY - offY) + "px";
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    ev.preventDefault();
  });

  document.body.appendChild(win);
  node.__mmxh3Preview = { win, title, body };
  return node.__mmxh3Preview;
}

function openPreview(node, slot) {
  const pv = ensurePreviewWindow(node);
  pv.title.textContent = slot.label + " 预览";
  pv.body.innerHTML = "";
  const value = (slotWidget(node, slot.name)?.value ?? "").trim();
  if (!value) {
    const hint = document.createElement("div");
    hint.textContent = "槽位为空，先选择或输入素材";
    hint.style.cssText = "color:#888;font-size:12px;";
    pv.body.appendChild(hint);
    pv.win.style.display = "block";
    return;
  }
  const src = viewSrc(value);
  if (!src) {
    const hint = document.createElement("div");
    hint.textContent = "无法预览：" + value;
    hint.style.cssText = "color:#f66;font-size:12px;";
    pv.body.appendChild(hint);
    pv.win.style.display = "block";
    return;
  }
  const media =
    slot.kind === "image"
      ? document.createElement("img")
      : slot.kind === "video"
        ? document.createElement("video")
        : document.createElement("audio");
  media.src = src;
  media.controls = slot.kind !== "image";
  media.autoplay = slot.kind !== "image";
  media.loop = slot.kind !== "image";
  media.style.cssText =
    "max-width:100%;max-height:100%;border-radius:4px;object-fit:contain;";
  if (slot.kind === "audio") media.style.width = "100%";
  media.onerror = () => {
    media.remove();
    const hint = document.createElement("div");
    hint.textContent = "预览加载失败：" + value;
    hint.style.cssText = "color:#f66;font-size:12px;";
    pv.body.appendChild(hint);
  };
  pv.body.appendChild(media);
  pv.win.style.display = "block";
}

// ---------- 槽位行 ----------

function buildRow(node, slot) {
  const row = document.createElement("div");
  row.style.cssText =
    "display:flex;gap:6px;align-items:center;height:" + ROW_HEIGHT + "px;width:100%;";

  const label = document.createElement("span");
  label.textContent = slot.label;
  label.title = slot.name;
  label.style.cssText =
    "flex:0 0 40px;color:var(--fg-color,#ccc);text-align:right;white-space:nowrap;" +
    "font-size:12px;line-height:" + ROW_HEIGHT + "px;";
  row.appendChild(label);

  const select = document.createElement("select");
  select.title = "从用户上传目录选择";
  select.style.cssText =
    "flex:1 1 36%;background:var(--comfy-input-bg,#1a1a1a);color:var(--fg-color,#ddd);" +
    "border:1px solid var(--border-color,#444);border-radius:4px;padding:0 4px;" +
    "height:20px;min-width:0;font-size:12px;";
  const fillSelect = (keep) => {
    select.innerHTML = "";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "（空）";
    select.appendChild(empty);
    for (const f of slot.files) {
      const o = document.createElement("option");
      o.value = f;
      o.textContent = f;
      select.appendChild(o);
    }
    select.value = slot.files.includes(keep) ? keep : "";
  };
  fillSelect();
  row.appendChild(select);

  const text = document.createElement("input");
  text.type = "text";
  text.placeholder = "文件名 / 网址URL";
  text.title = "可手输 input 目录文件名、相对路径或 http(s) 网址（自动下载）";
  text.style.cssText =
    "flex:1 1 36%;background:var(--comfy-input-bg,#1a1a1a);color:var(--fg-color,#ddd);" +
    "border:1px solid var(--border-color,#444);border-radius:4px;padding:0 4px;" +
    "height:20px;min-width:0;font-size:12px;";
  row.appendChild(text);

  const sync = (val) => {
    const v = val ?? "";
    setSlotValue(node, slot.name, v);
    text.value = v;
    select.value = slot.files.includes(v) ? v : "";
  };

  select.addEventListener("change", () => sync(select.value));
  text.addEventListener("change", () => sync(text.value.trim()));

  const mkBtn = (txt, title, onClick) => {
    const b = document.createElement("button");
    b.textContent = txt;
    b.title = title;
    b.type = "button";
    b.style.cssText =
      "flex:0 0 auto;background:var(--comfy-menu-bg,#2a2a2a);color:var(--fg-color,#ddd);" +
      "border:1px solid var(--border-color,#444);border-radius:4px;padding:0 6px;" +
      "height:20px;cursor:pointer;font-size:12px;line-height:18px;";
    b.addEventListener("click", onClick);
    return b;
  };

  const upload = mkBtn("上传", "选择本地文件上传到用户上传目录", () => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ACCEPT_OF[slot.kind] || "";
    picker.onchange = async () => {
      const file = picker.files?.[0];
      if (!file) return;
      try {
        const body = new FormData();
        body.append("image", file, file.name);
        body.append("type", "input");
        const resp = await api.fetchApi("/upload/image", { method: "POST", body });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const r = await resp.json();
        const path = [r.subfolder, r.name].filter(Boolean).join("/");
        if (!slot.files.includes(path)) slot.files.push(path);
        fillSelect(path);
        sync(path);
      } catch (e) {
        console.warn("[MiniMaxH3] 上传失败", e);
        alert("上传失败: " + (e?.message || e));
      }
    };
    picker.click();
  });
  row.appendChild(upload);

  const refresh = mkBtn("⟳", "刷新用户上传目录列表", async () => {
    try {
      const resp = await api.fetchApi(
        FILES_ROUTE + "?kind=" + encodeURIComponent(slot.kind)
      );
      if (resp.ok) {
        const names = await resp.json();
        if (Array.isArray(names)) {
          slot.files = names.slice();
          fillSelect(text.value);
        }
      }
    } catch (e) {
      console.warn("[MiniMaxH3] 刷新文件列表失败", e);
    }
  });
  row.appendChild(refresh);

  const preview = mkBtn("预览", "打开右侧磁吸预览窗", () => {
    openPreview(node, slot);
  });
  row.appendChild(preview);

  // 工作流载入后把已保存的槽位值显示出来
  setTimeout(() => {
    const cur = slotWidget(node, slot.name)?.value;
    if (cur) {
      text.value = cur;
      select.value = slot.files.includes(cur) ? cur : "";
    }
  }, 100);

  return { row };
}

// ---------- 提示词 DOM 控件（吸收节点窗口多余高度） ----------

function buildPrompt(node) {
  const host = document.createElement("div");
  host.style.cssText = "display:flex;flex-direction:column;width:100%;height:100%;";

  const ta = document.createElement("textarea");
  ta.placeholder = "提示词（可含 <Picture n> / <Video k> / <Audio j> 标签）";
  ta.style.cssText =
    "flex:1;width:100%;min-height:0;resize:none;box-sizing:border-box;" +
    "background:var(--comfy-input-bg,#1a1a1a);color:var(--fg-color,#ddd);" +
    "border:1px solid var(--border-color,#444);border-radius:4px;padding:4px 6px;" +
    "font-size:12px;line-height:1.5;font-family:inherit;overflow:auto;";
  ta.addEventListener("input", () => {
    setSlotValue(node, "prompt", ta.value);
  });
  host.appendChild(ta);

  const w = node.addDOMWidget("mmxh3_prompt", PROMPT_TYPE, host, {
    serialize: false,
    getMinHeight: () => PROMPT_MIN_HEIGHT,
    getValue: () => slotWidget(node, "prompt")?.value ?? "",
    setValue: (v) => {
      ta.value = v ?? "";
    },
  });

  // 面板高度联动：多余高度只给提示词输入框
  const bindStretch = () => {
    const wrapper = host.parentElement;
    const root = host.closest(".lg-node") || host.closest("[data-node-id]");
    if (!wrapper || !root) return;
    const layout = () => {
      const rootH = root.clientHeight;
      if (!rootH) return;
      const used = root.scrollHeight - wrapper.offsetHeight;
      const h = Math.max(PROMPT_MIN_HEIGHT, rootH - used);
      if (Math.abs(h - wrapper.offsetHeight) > 2) {
        wrapper.style.height = h + "px";
        wrapper.style.flexShrink = "0";
      }
    };
    layout();
    if (typeof ResizeObserver !== "undefined") {
      try {
        new ResizeObserver(layout).observe(root);
      } catch (e) { /* 忽略 */ }
    }
  };
  setTimeout(bindStretch, 100);
  setTimeout(bindStretch, 500);

  // 工作流载入后回填已保存的提示词
  setTimeout(() => {
    const cur = slotWidget(node, "prompt")?.value;
    if (cur) ta.value = cur;
  }, 100);

  return w;
}

app.registerExtension({
  name: "MiniMaxH3.SmartSlots",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== NODE_ID) return;
    const slots = collectSlots(nodeData);
    // 控件型输入（自定义 widget 类型）——只生成控件，不生成连接点
    (nodeData.input.optional ??= {})["mmxh3_prompt"] = [PROMPT_TYPE, {}];
    (nodeData.input.optional ??= {})["mmxh3_slot_panel"] = [
      PANEL_TYPE,
      { slots },
    ];

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated?.apply(this, arguments);
      // 隐藏原始 STRING 控件（值仍在，仅显示交给自绘控件）
      hideWidget(this, "prompt");
      for (const s of slots) hideWidget(this, s.name);
      // 面板顺序：提示词控件紧跟 prompt 之后、槽位面板在 duration 之后
      const moveAfter = (name, after) => {
        const widgets = this.widgets ?? [];
        const i = widgets.findIndex((w) => w.name === name);
        const a = widgets.findIndex((w) => w.name === after);
        if (i >= 0 && a >= 0 && i !== a + 1) {
          const [w] = widgets.splice(i, 1);
          const pos = widgets.findIndex((x) => x.name === after);
          widgets.splice(pos + 1, 0, w);
        }
      };
      moveAfter("mmxh3_prompt", "prompt");
      moveAfter("mmxh3_slot_panel", "duration");
      return r;
    };
  },
  getCustomWidgets() {
    return {
      [PROMPT_TYPE](node, inputName, inputData) {
        return buildPrompt(node);
      },
      [PANEL_TYPE](node, inputName, inputData) {
        const cfg = (Array.isArray(inputData) ? inputData[1] : inputData) ?? {};
        const slots = Array.isArray(cfg.slots) ? cfg.slots : [];

        const host = document.createElement("div");
        host.style.cssText = "display:flex;flex-direction:column;width:100%;";

        for (const slot of slots) {
          const { row } = buildRow(node, slot);
          host.appendChild(row);
        }

        const w = node.addDOMWidget(inputName, PANEL_TYPE, host, {
          serialize: false,
          getMinHeight: () => slots.length * ROW_HEIGHT,
          getValue: () => slotWidget(node, slots[0]?.name)?.value ?? "",
          setValue: () => { /* 值由各行同步真实 STRING 控件 */ },
        });
        return w;
      },
    };
  },
});
