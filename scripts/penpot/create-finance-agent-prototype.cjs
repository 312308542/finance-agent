#!/usr/bin/env node

/**
 * 通过 Penpot MCP 生成 Hermes Finance Agent 的第一版可编辑原型。
 *
 * 这个脚本故意把生成过程拆成多次 MCP 调用：
 * - Penpot MCP 单次 execute_code 有 30 秒任务限制；
 * - 分页面生成更稳定，也便于后续继续迭代某个页面。
 *
 * 使用前提：
 * 1. Penpot Docker 已启动：http://localhost:9001
 * 2. Penpot MCP 已启动：http://localhost:4401/mcp
 * 3. Penpot 设计文件中已运行 MCP 插件并连接成功
 *
 * 运行：
 *   node D:\Code\aiAgents\finance-agent\scripts\penpot\create-finance-agent-prototype.cjs
 */

const targetUrl = process.env.PENPOT_MCP_URL || "http://localhost:4401/mcp";
const exportPath =
  process.env.PENPOT_EXPORT_PATH ||
  "D:\\Code\\aiAgents\\finance-agent\\prototypes\\penpot\\dashboard-overview.png";

let sessionId = null;
let nextId = 1;

async function postRpc(message) {
  const headers = {
    Accept: "application/json, text/event-stream",
    "Content-Type": "application/json",
  };

  if (sessionId) {
    headers["mcp-session-id"] = sessionId;
  }

  const response = await fetch(targetUrl, {
    method: "POST",
    headers,
    body: JSON.stringify(message),
  });

  const nextSessionId = response.headers.get("mcp-session-id");
  if (nextSessionId) {
    sessionId = nextSessionId;
  }

  const body = await response.text();
  if (response.status === 202 || body.trim() === "") {
    return null;
  }

  if (!response.ok) {
    throw new Error(`Penpot MCP HTTP ${response.status}: ${body}`);
  }

  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return JSON.parse(body);
  }

  const events = [];
  let current = [];
  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (line === "") {
      if (current.length > 0) {
        events.push(current.join("\n"));
        current = [];
      }
      continue;
    }

    if (line.startsWith("data:")) {
      current.push(line.slice(5).trimStart());
    }
  }

  if (current.length > 0) {
    events.push(current.join("\n"));
  }

  for (const eventText of events) {
    const trimmed = eventText.trim();
    if (trimmed && trimmed !== "[DONE]") {
      return JSON.parse(trimmed);
    }
  }

  return null;
}

async function callTool(name, args = {}) {
  return postRpc({
    jsonrpc: "2.0",
    id: nextId++,
    method: "tools/call",
    params: {
      name,
      arguments: args,
    },
  });
}

function readToolJson(response) {
  const text = response?.result?.content?.[0]?.text;
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`Penpot 工具返回了非 JSON 内容：\n${text}`);
  }
}

async function execute(code) {
  const response = await callTool("execute_code", { code });
  const parsed = readToolJson(response);
  if (!parsed || parsed.error) {
    throw new Error(`Penpot execute_code 失败：${JSON.stringify(parsed || response, null, 2)}`);
  }
  return parsed.result;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function switchToPage(name) {
  await execute(`
const page = penpotUtils.getPageByName(${JSON.stringify(name)});
if (!page) {
  throw new Error("找不到页面：" + ${JSON.stringify(name)});
}
penpot.openPage(page);
return { requested: page.name, current: penpot.currentPage && penpot.currentPage.name };
`);

  for (let index = 0; index < 10; index += 1) {
    await sleep(120);
    const state = await execute(`
return {
  current: penpot.currentPage && penpot.currentPage.name,
  childCount: penpot.root ? penpot.root.children.length : -1,
};
`);
    if (state.current === name) {
      return state;
    }
  }

  throw new Error(`切换 Penpot 页面超时：${name}`);
}

function installRuntimeCode() {
  return String.raw`
storage.hermesFinance = (() => {
  const C = {
    bg: "#08111F",
    bg2: "#0B1220",
    rail: "#0D1626",
    top: "#101827",
    panel: "#121C2E",
    panel2: "#17243A",
    border: "#26364F",
    border2: "#36506D",
    text: "#F8FAFC",
    muted: "#94A3B8",
    subtle: "#64748B",
    gold: "#F59E0B",
    goldSoft: "#2B2312",
    cyan: "#38BDF8",
    cyanSoft: "#102C3B",
    green: "#22C55E",
    greenSoft: "#0F2A1D",
    red: "#EF4444",
    redSoft: "#351B20",
    violet: "#8B5CF6",
    violetSoft: "#211A35",
  };

  const FONT_UI = "Microsoft YaHei UI";
  const FONT_MONO = "JetBrains Mono";

  function page(name) {
    let page = penpotUtils.getPageByName(name);
    if (!page) {
      page = penpot.createPage();
      page.name = name;
    }
    return page;
  }

  function ensurePages() {
    let dashboard = penpotUtils.getPageByName("01 Dashboard 总览");
    if (!dashboard) {
      const starter = penpotUtils.getPageByName("Page 1");
      if (starter) {
        starter.name = "01 Dashboard 总览";
        dashboard = starter;
      } else {
        dashboard = page("01 Dashboard 总览");
      }
    }

    return {
      design: page("00 Design System"),
      dashboard,
      portfolio: page("02 Portfolio 持仓分析"),
      signal: page("03 Signal Lab 信号解释"),
      report: page("04 Agent Report AI报告"),
      collaboration: page("05 Agent 协作进展"),
    };
  }

  function clearCurrentPage() {
    const root = penpot.root;
    for (const child of [...root.children]) {
      child.remove();
    }
    return root;
  }

  function attach(parent, shape, name, x, y, width, height) {
    shape.name = name;
    parent.appendChild(shape);
    if (typeof width === "number" && typeof height === "number") {
      shape.resize(width, height);
    }
    penpotUtils.setParentXY(shape, x, y);
    return shape;
  }

  function board(parent, name, x, y, width, height, options = {}) {
    const shape = penpot.createBoard();
    attach(parent, shape, name, x, y, width, height);
    shape.fills = options.fill ? [{ fillColor: options.fill, fillOpacity: options.opacity ?? 1 }] : [];
    shape.strokes = options.stroke
      ? [{
          strokeColor: options.stroke,
          strokeOpacity: options.strokeOpacity ?? 1,
          strokeWidth: options.strokeWidth ?? 1,
        }]
      : [];
    shape.borderRadius = options.radius ?? 0;
    shape.clipContent = options.clipContent ?? true;
    return shape;
  }

  function rect(parent, name, x, y, width, height, options = {}) {
    const shape = penpot.createRectangle();
    attach(parent, shape, name, x, y, width, height);
    shape.fills = options.fill && options.fill !== "transparent"
      ? [{ fillColor: options.fill, fillOpacity: options.opacity ?? 1 }]
      : [];
    shape.strokes = options.stroke
      ? [{
          strokeColor: options.stroke,
          strokeOpacity: options.strokeOpacity ?? 1,
          strokeWidth: options.strokeWidth ?? 1,
        }]
      : [];
    shape.borderRadius = options.radius ?? 0;
    return shape;
  }

  function label(parent, name, characters, x, y, width, height, options = {}) {
    const shape = penpot.createText(characters);
    if (!shape) {
      throw new Error("创建文本失败：" + name);
    }
    attach(parent, shape, name, x, y, width, height);
    shape.growType = "fixed";
    shape.fontFamily = options.mono ? FONT_MONO : FONT_UI;
    shape.fontSize = String(options.size ?? 14);
    shape.fontWeight = String(options.weight ?? 400);
    shape.lineHeight = String(options.lineHeight ?? 1.35);
    shape.letterSpacing = "0";
    shape.align = options.align ?? "left";
    shape.verticalAlign = options.verticalAlign ?? "top";
    shape.textTransform = null;
    shape.fills = [{ fillColor: options.color ?? C.text, fillOpacity: options.opacity ?? 1 }];
    return shape;
  }

  function pill(parent, text, x, y, width, tone = "neutral") {
    const tones = {
      neutral: [C.panel2, C.muted, C.border],
      gold: [C.goldSoft, C.gold, "#61450C"],
      green: [C.greenSoft, C.green, "#1D5D39"],
      red: [C.redSoft, C.red, "#6B2730"],
      cyan: [C.cyanSoft, C.cyan, "#1E5671"],
      violet: [C.violetSoft, "#B9A2FF", "#493B70"],
    };
    const [fill, fg, stroke] = tones[tone] || tones.neutral;
    rect(parent, "标签 / " + text, x, y, width, 28, { fill, stroke, radius: 4 });
    label(parent, "标签文字 / " + text, text, x + 10, y + 6, width - 20, 18, {
      size: 12,
      weight: 600,
      color: fg,
      align: "center",
    });
  }

  function sectionTitle(parent, title, x, y, width, subtitle) {
    label(parent, "标题 / " + title, title, x, y, width, 24, {
      size: 16,
      weight: 700,
      color: C.text,
    });
    if (subtitle) {
      label(parent, "说明 / " + title, subtitle, x, y + 26, width, 20, {
        size: 12,
        color: C.subtle,
      });
    }
  }

  function panel(parent, name, x, y, width, height, title, subtitle) {
    const p = board(parent, name, x, y, width, height, {
      fill: C.panel,
      stroke: C.border,
      radius: 8,
    });
    rect(p, "顶部强调线", 0, 0, width, 3, { fill: C.border2 });
    sectionTitle(p, title, 18, 16, width - 36, subtitle);
    return p;
  }

  function divider(parent, x, y, width) {
    rect(parent, "分隔线", x, y, width, 1, { fill: C.border, opacity: 1 });
  }

  function stat(parent, title, value, change, x, y, width, tone = "neutral") {
    rect(parent, "指标底 / " + title, x, y, width, 72, { fill: C.bg2, stroke: C.border, radius: 6 });
    label(parent, "指标名 / " + title, title, x + 12, y + 10, width - 24, 18, {
      size: 11,
      color: C.subtle,
    });
    label(parent, "指标值 / " + title, value, x + 12, y + 29, width - 24, 24, {
      size: 18,
      weight: 700,
      color: C.text,
      mono: true,
    });
    const toneColor = tone === "green" ? C.green : tone === "red" ? C.red : tone === "gold" ? C.gold : C.muted;
    label(parent, "指标变化 / " + title, change, x + 12, y + 54, width - 24, 14, {
      size: 10,
      color: toneColor,
      mono: true,
    });
  }

  function progress(parent, name, x, y, width, value, color, track = C.bg2) {
    rect(parent, "进度轨道 / " + name, x, y, width, 8, { fill: track, radius: 4 });
    rect(parent, "进度值 / " + name, x, y, Math.max(2, Math.round(width * value)), 8, { fill: color, radius: 4 });
  }

  function row(parent, name, y, cells, widths, options = {}) {
    const height = options.height ?? 34;
    rect(parent, "行底 / " + name, 0, y, widths.reduce((a, b) => a + b, 0), height, {
      fill: options.fill ?? "transparent",
      opacity: options.opacity ?? 1,
    });
    let x = 0;
    cells.forEach((cell, index) => {
      label(parent, "单元格 / " + name + " / " + index, cell.text, x + 8, y + 9, widths[index] - 16, 16, {
        size: cell.size ?? 12,
        weight: cell.weight ?? 500,
        color: cell.color ?? C.muted,
        mono: cell.mono ?? false,
        align: cell.align ?? "left",
      });
      x += widths[index];
    });
  }

  return {
    C,
    ensurePages,
    clearCurrentPage,
    board,
    rect,
    label,
    pill,
    panel,
    divider,
    stat,
    progress,
    row,
  };
})();

return {
  installed: true,
  pages: storage.hermesFinance.ensurePages() && penpotUtils.getPages(),
};
`;
}

function designSystemCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const pages = ui.ensurePages();
const root = ui.clearCurrentPage();
const canvas = ui.board(root, "Design System / Finance Agent", 80, 80, 1440, 1024, {
  fill: C.bg,
  radius: 0,
});

ui.label(canvas, "页面标题", "Hermes Finance Agent / UI Design System", 48, 40, 720, 40, {
  size: 28,
  weight: 800,
  color: C.text,
});
ui.label(canvas, "页面说明", "金融小白优先：结论先行、风险解释、信号可追溯、操作需确认。视觉上采用专业高密度工作台，而不是营销页。", 48, 84, 980, 26, {
  size: 14,
  color: C.muted,
});

ui.label(canvas, "标题 / 颜色 Tokens", "颜色 Tokens", 48, 148, 420, 24, { size: 16, weight: 700, color: C.text });
ui.label(canvas, "说明 / 颜色 Tokens", "状态色必须同时表达方向和风险，不只依赖红绿。", 48, 174, 420, 20, { size: 12, color: C.subtle });

const swatches = [
  ["Background", C.bg], ["Surface", C.panel], ["Surface 2", C.panel2], ["Border", C.border],
  ["Primary Amber", C.gold], ["Info Cyan", C.cyan], ["Profit Green", C.green], ["Risk Red", C.red],
  ["AI Violet", C.violet], ["Text", C.text], ["Muted", C.muted], ["Subtle", C.subtle],
];
swatches.forEach(([name, color], index) => {
  const col = index % 4;
  const r = Math.floor(index / 4);
  const x = 48 + col * 190;
  const y = 206 + r * 88;
  ui.rect(canvas, "色块 / " + name, x, y, 160, 42, { fill: color, stroke: C.border, radius: 6 });
  ui.label(canvas, "色名 / " + name, name, x, y + 50, 160, 18, { size: 12, weight: 700, color: C.text });
  ui.label(canvas, "色值 / " + name, color, x, y + 68, 160, 16, { size: 11, mono: true, color: C.subtle });
});

ui.label(canvas, "标题 / 排版", "排版", 48, 520, 500, 24, { size: 16, weight: 700, color: C.text });
ui.label(canvas, "说明 / 排版", "中文使用系统 UI 字体，数字和行情使用等宽字体。", 48, 546, 500, 20, { size: 12, color: C.subtle });
ui.label(canvas, "字体示例 H1", "H1 今日结论：谨慎增持", 48, 582, 560, 42, { size: 30, weight: 800 });
ui.label(canvas, "字体示例 H2", "H2 组合风险：中等偏高", 48, 630, 560, 32, { size: 22, weight: 700 });
ui.label(canvas, "字体示例 Body", "Body 信号解释需要告诉用户为什么，而不是只给 RSI/MACD 数值。", 48, 674, 680, 24, { size: 14, color: C.muted });
ui.label(canvas, "字体示例 Mono", "Mono 510,240.88  +2.31%  MDD -8.6%", 48, 710, 680, 26, { size: 16, mono: true, color: C.cyan });

ui.label(canvas, "标题 / 组件样式", "组件样式", 820, 148, 500, 24, { size: 16, weight: 700, color: C.text });
ui.label(canvas, "说明 / 组件样式", "组件保持 4-8px 圆角，适合密集扫描和重复操作。", 820, 174, 500, 20, { size: 12, color: C.subtle });
const comp = ui.board(canvas, "组件样例区", 820, 206, 520, 392, { fill: C.panel, stroke: C.border, radius: 8 });
ui.pill(comp, "建议买入", 24, 26, 96, "green");
ui.pill(comp, "等待确认", 132, 26, 104, "gold");
ui.pill(comp, "高波动", 248, 26, 90, "red");
ui.pill(comp, "AI 解释", 350, 26, 86, "violet");
ui.stat(comp, "总资产", "¥510,240", "+2.31%", 24, 82, 150, "green");
ui.stat(comp, "最大回撤", "-8.6%", "近30日", 190, 82, 150, "red");
ui.stat(comp, "现金仓位", "18%", "建议保留", 356, 82, 140, "gold");
ui.divider(comp, 24, 182, 472);
ui.row(comp, "表头", 204, [
  { text: "资产", weight: 700, color: C.text },
  { text: "信号", weight: 700, color: C.text },
  { text: "风险", weight: 700, color: C.text },
  { text: "动作", weight: 700, color: C.text },
], [120, 120, 100, 132], { fill: C.bg2 });
ui.row(comp, "表格行", 242, [
  { text: "贵州茅台" },
  { text: "趋势转弱", color: C.gold },
  { text: "中" },
  { text: "减仓 5%", color: C.red },
], [120, 120, 100, 132]);
ui.progress(comp, "风险进度", 24, 312, 300, 0.68, C.gold);
ui.label(comp, "风险文案", "风险条：0-40 低，40-70 中，70+ 高", 340, 306, 150, 28, { size: 11, color: C.subtle });

ui.label(canvas, "标题 / 信息原则", "信息原则", 820, 650, 500, 24, { size: 16, weight: 700, color: C.text });
ui.label(canvas, "说明 / 信息原则", "每个推荐都要能回溯：数据源、信号、风控、AI 推理、人工确认。", 820, 676, 500, 20, { size: 12, color: C.subtle });
[
  "推荐只输出“订单草案”，默认不自动交易。",
  "金融因子由成熟库计算：TA-Lib 优先，ta 作为安装失败时的兜底。",
  "Agent 报告给结论，也给不确定性和反例。",
  "新手模式隐藏公式细节，但保留可展开解释。",
].forEach((rule, index) => {
  ui.rect(canvas, "原则项底 / " + index, 820, 710 + index * 48, 520, 36, { fill: C.panel, stroke: C.border, radius: 6 });
  ui.rect(canvas, "原则项点 / " + index, 838, 723 + index * 48, 10, 10, { fill: index === 0 ? C.red : index === 1 ? C.cyan : index === 2 ? C.violet : C.gold, radius: 5 });
  ui.label(canvas, "原则项文字 / " + index, rule, 862, 718 + index * 48, 450, 18, { size: 13, color: C.muted });
});

return {
  boardId: canvas.id,
  structure: penpotUtils.shapeStructure(root, 2),
};
`;
}

function dashboardBaseCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const pages = ui.ensurePages();
const root = ui.clearCurrentPage();
const canvas = ui.board(root, "Dashboard / 总览工作台 1440", 80, 80, 1440, 1024, {
  fill: C.bg,
  radius: 0,
});

ui.rect(canvas, "左侧导航底", 0, 0, 220, 1024, { fill: C.rail });
ui.rect(canvas, "顶部工具栏底", 220, 0, 1220, 72, { fill: C.top });
ui.rect(canvas, "主区底纹", 220, 72, 1220, 952, { fill: C.bg2 });

ui.label(canvas, "产品名", "Hermes Finance", 24, 24, 174, 24, { size: 18, weight: 800, color: C.text });
ui.label(canvas, "产品副标题", "Agent 投资分析台", 24, 52, 174, 18, { size: 12, color: C.subtle });
["总览", "持仓分析", "协作进展", "AI 报告", "机会池", "信号实验室", "MCP / CLI"].forEach((item, index) => {
  const y = 112 + index * 46;
  if (index === 0) {
    ui.rect(canvas, "导航选中 / " + item, 16, y - 8, 188, 36, { fill: C.goldSoft, stroke: "#5A4212", radius: 6 });
    ui.rect(canvas, "导航选中条 / " + item, 16, y - 8, 4, 36, { fill: C.gold, radius: 2 });
  }
  ui.label(canvas, "导航文字 / " + item, item, 34, y, 150, 18, {
    size: 13,
    weight: index === 0 ? 700 : 500,
    color: index === 0 ? C.gold : C.muted,
  });
});

ui.label(canvas, "页头标题", "总览", 244, 22, 120, 26, { size: 22, weight: 800 });
ui.label(canvas, "页头说明", "数据刷新：09:45:31  /  A股延迟行情 + Crypto 实时行情  /  新手模式", 330, 27, 620, 18, {
  size: 12,
  color: C.subtle,
});
ui.pill(canvas, "只生成订单草案", 1072, 22, 126, "gold");
ui.pill(canvas, "风控已启用", 1210, 22, 102, "green");
ui.rect(canvas, "报告按钮", 1322, 18, 86, 34, { fill: C.gold, stroke: "#D98705", radius: 6 });
ui.label(canvas, "报告按钮文字", "生成报告", 1338, 27, 54, 16, { size: 12, weight: 700, color: "#111827", align: "center" });

storage.hermesFinance.dashboardBoardId = canvas.id;
penpot.openPage(pages.dashboard);
return {
  boardId: canvas.id,
  structure: penpotUtils.shapeStructure(root, 2),
};
`;
}

function dashboardTopPanelsCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.dashboardBoardId);
if (!canvas) throw new Error("找不到 Dashboard 画板");

const summary = ui.panel(canvas, "面板 / AI 今日结论", 244, 96, 604, 204, "AI 今日结论", "面向金融小白：先给动作，再给原因和风险。");
ui.label(summary, "核心结论", "谨慎增持：只对强信号资产小幅加仓", 18, 64, 420, 34, {
  size: 22,
  weight: 800,
  color: C.text,
});
ui.label(summary, "结论说明", "市场趋势偏强，但组合科技股暴露偏高。建议现金保留 18%，先处理高波动持仓，再考虑新增仓位。", 18, 104, 540, 40, {
  size: 13,
  color: C.muted,
  lineHeight: 1.55,
});
ui.pill(summary, "A股：等待回调", 18, 158, 112, "gold");
ui.pill(summary, "BTC：仓位上限 8%", 142, 158, 130, "cyan");
ui.pill(summary, "现金：保持 18%", 284, 158, 116, "green");
ui.pill(summary, "高风险动作需确认", 412, 158, 132, "red");

const risk = ui.panel(canvas, "面板 / 组合风险雷达", 872, 96, 320, 204, "组合风险雷达", "解释风险来源，而不是只给分数。");
[
  ["总风险", 0.68, C.gold, "68 / 100"],
  ["回撤压力", 0.54, C.cyan, "中"],
  ["集中度", 0.76, C.red, "偏高"],
  ["波动率", 0.63, C.gold, "中高"],
].forEach(([name, value, color, txt], index) => {
  const y = 62 + index * 34;
  ui.label(risk, "风险名 / " + name, name, 18, y, 76, 18, { size: 12, color: C.muted });
  ui.progress(risk, name, 96, y + 5, 138, value, color);
  ui.label(risk, "风险值 / " + name, txt, 246, y, 54, 18, { size: 12, color, mono: true, align: "right" });
});
ui.label(risk, "风险解释", "主要风险来自新能源与 BTC 同涨同跌。", 18, 174, 280, 18, { size: 11, color: C.subtle });

const agents = ui.panel(canvas, "面板 / Agent 运行状态", 1216, 96, 184, 204, "Agents", "内部自动消耗数据");
[
  ["数据", "完成", C.green],
  ["信号", "完成", C.green],
  ["风控", "复核中", C.gold],
  ["报告", "待生成", C.cyan],
].forEach(([name, state, color], index) => {
  const y = 62 + index * 31;
  ui.rect(agents, "状态点 / " + name, 18, y + 4, 8, 8, { fill: color, radius: 4 });
  ui.label(agents, "Agent 名 / " + name, name, 34, y, 52, 16, { size: 12, color: C.muted });
  ui.label(agents, "Agent 状态 / " + name, state, 96, y, 66, 16, { size: 12, color, align: "right" });
});
ui.rect(agents, "MCP 服务提示", 18, 172, 148, 20, { fill: C.bg2, stroke: C.border, radius: 4 });
ui.label(agents, "MCP 服务提示文字", "MCP / CLI 可调用", 31, 176, 122, 12, { size: 10, color: C.cyan, align: "center" });

const tape = ui.panel(canvas, "面板 / 市场概览", 244, 324, 1156, 112, "市场概览", "A股、指数、数字货币统一进入基础金融数据缓存。");
[
  ["沪深300", "3,982.42", "+0.76%", C.green],
  ["创业板", "2,141.08", "-0.21%", C.red],
  ["BTC/USDT", "91,240", "+1.84%", C.green],
  ["ETH/USDT", "4,820", "+0.44%", C.green],
  ["USDT/CNY", "7.18", "+0.02%", C.muted],
].forEach(([name, value, change, color], index) => {
  const x = 18 + index * 218;
  ui.rect(tape, "市场项底 / " + name, x, 56, 198, 38, { fill: C.bg2, stroke: C.border, radius: 6 });
  ui.label(tape, "市场名 / " + name, name, x + 12, 64, 76, 16, { size: 12, color: C.subtle });
  ui.label(tape, "市场值 / " + name, value, x + 90, 64, 62, 16, { size: 12, color: C.text, mono: true, align: "right" });
  ui.label(tape, "市场变化 / " + name, change, x + 154, 64, 34, 16, { size: 11, color, mono: true, align: "right" });
});

return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function dashboardBottomPanelsCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.dashboardBoardId);
if (!canvas) throw new Error("找不到 Dashboard 画板");

const holdings = ui.panel(canvas, "面板 / 持仓诊断", 244, 460, 760, 276, "持仓诊断", "AI 只给可解释的建议，最终动作需用户确认。");
const widths = [146, 96, 98, 96, 100, 160];
ui.row(holdings, "持仓表头", 60, [
  { text: "资产", weight: 700, color: C.text },
  { text: "仓位", weight: 700, color: C.text, align: "right" },
  { text: "盈亏", weight: 700, color: C.text, align: "right" },
  { text: "信号", weight: 700, color: C.text },
  { text: "风险", weight: 700, color: C.text },
  { text: "建议", weight: 700, color: C.text },
], widths, { fill: C.bg2, height: 34 });
[
  ["贵州茅台", "16%", "+3.2%", "趋势中性", "低", "继续持有，等待财报"],
  ["宁德时代", "22%", "-4.8%", "转弱", "中高", "减仓 5%，降低集中度"],
  ["沪深300ETF", "26%", "+1.1%", "稳健", "低", "作为底仓保留"],
  ["BTC", "9%", "+8.6%", "强势", "高", "不追高，设止盈线"],
  ["现金", "18%", "0.0%", "防守", "低", "保持流动性"],
].forEach((cells, index) => {
  const y = 98 + index * 32;
  const signalColor = cells[3].includes("强") ? C.green : cells[3].includes("转弱") ? C.red : C.gold;
  const riskColor = cells[4].includes("高") ? C.red : cells[4] === "低" ? C.green : C.gold;
  ui.row(holdings, "持仓行 / " + cells[0], y, [
    { text: cells[0], color: C.text, weight: 600 },
    { text: cells[1], mono: true, align: "right" },
    { text: cells[2], mono: true, align: "right", color: cells[2].startsWith("+") ? C.green : C.red },
    { text: cells[3], color: signalColor },
    { text: cells[4], color: riskColor },
    { text: cells[5], color: C.muted },
  ], widths, { fill: index % 2 === 0 ? "#0F1828" : "transparent", height: 30 });
});

const watch = ui.panel(canvas, "面板 / 观察清单与信号", 1028, 460, 372, 276, "观察清单与信号", "信号层统一输出，Agent 只消费协议结果。");
[
  ["比亚迪", "量价共振", "候选", C.green],
  ["中芯国际", "波动扩大", "观望", C.gold],
  ["黄金ETF", "避险增强", "关注", C.cyan],
  ["SOL", "过热", "降权", C.red],
].forEach(([asset, reason, state, color], index) => {
  const y = 62 + index * 44;
  ui.rect(watch, "观察项底 / " + asset, 18, y, 336, 34, { fill: C.bg2, stroke: C.border, radius: 6 });
  ui.rect(watch, "观察项色条 / " + asset, 18, y, 4, 34, { fill: color, radius: 2 });
  ui.label(watch, "观察资产 / " + asset, asset, 34, y + 8, 78, 16, { size: 12, weight: 700, color: C.text });
  ui.label(watch, "观察原因 / " + asset, reason, 118, y + 8, 114, 16, { size: 12, color: C.muted });
  ui.label(watch, "观察状态 / " + asset, state, 260, y + 8, 74, 16, { size: 12, color, align: "right" });
});
ui.divider(watch, 18, 238, 336);
ui.label(watch, "信号协议提示", "SignalSnapshot v1：趋势、动量、波动、成交量、估值、新闻情绪、风控标签", 18, 248, 336, 18, { size: 10, color: C.subtle });

const consensus = ui.panel(canvas, "面板 / 信号共识", 244, 760, 524, 220, "信号共识", "TA-Lib / ta 计算基础指标，组合成统一信号协议。");
[
  ["趋势", 0.72, C.green, "偏强"],
  ["动量", 0.61, C.gold, "温和"],
  ["波动", 0.69, C.red, "偏高"],
  ["成交量", 0.57, C.cyan, "确认中"],
].forEach(([name, value, color, state], index) => {
  const y = 66 + index * 33;
  ui.label(consensus, "因子名 / " + name, name, 18, y, 58, 18, { size: 12, color: C.muted });
  ui.progress(consensus, name, 84, y + 5, 250, value, color);
  ui.label(consensus, "因子状态 / " + name, state, 350, y, 70, 18, { size: 12, color });
  ui.label(consensus, "因子值 / " + name, String(Math.round(value * 100)), 442, y, 42, 18, { size: 12, mono: true, color: C.text, align: "right" });
});
ui.label(consensus, "共识解释", "结论：趋势与成交量尚可，但波动因子拉高，仓位建议受风控压制。", 18, 194, 486, 18, { size: 11, color: C.subtle });

const action = ui.panel(canvas, "面板 / 操作草案", 792, 760, 608, 220, "操作草案", "这里不是自动下单，是给用户确认前的结构化建议。");
[
  ["减仓", "宁德时代", "5%", "降低组合集中度与波动"],
  ["持有", "沪深300ETF", "不变", "底仓稳定，继续承担市场 beta"],
  ["止盈", "BTC", "设置 96,000", "保护已有浮盈，避免追高"],
].forEach(([act, asset, size, reason], index) => {
  const y = 62 + index * 43;
  ui.rect(action, "草案项底 / " + asset, 18, y, 572, 34, { fill: C.bg2, stroke: C.border, radius: 6 });
  const color = act === "减仓" ? C.red : act === "止盈" ? C.gold : C.green;
  ui.label(action, "草案动作 / " + asset, act, 32, y + 8, 46, 16, { size: 12, weight: 800, color });
  ui.label(action, "草案资产 / " + asset, asset, 92, y + 8, 100, 16, { size: 12, color: C.text, weight: 700 });
  ui.label(action, "草案规模 / " + asset, size, 204, y + 8, 86, 16, { size: 12, color: C.cyan, mono: true });
  ui.label(action, "草案原因 / " + asset, reason, 306, y + 8, 260, 16, { size: 12, color: C.muted });
});
ui.rect(action, "确认按钮", 420, 174, 76, 30, { fill: C.gold, stroke: "#D98705", radius: 6 });
ui.label(action, "确认按钮文字", "确认草案", 432, 182, 52, 14, { size: 12, weight: 700, color: "#111827", align: "center" });
ui.rect(action, "复核按钮", 508, 174, 82, 30, { fill: C.panel2, stroke: C.border2, radius: 6 });
ui.label(action, "复核按钮文字", "要求复核", 522, 182, 54, 14, { size: 12, weight: 700, color: C.text, align: "center" });

return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function skeletonPagesCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const pages = ui.ensurePages();
return {
  pages: penpotUtils.getPages(),
  currentPage: penpot.currentPage && penpot.currentPage.name,
  childCount: penpot.root ? penpot.root.children.length : -1,
};
`;
}

function portfolioPageCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const root = ui.clearCurrentPage();
const canvas = ui.board(root, "Portfolio / 持仓分析 1440", 80, 80, 1440, 1024, { fill: C.bg, radius: 0 });
ui.rect(canvas, "顶部栏", 0, 0, 1440, 72, { fill: C.top });
ui.label(canvas, "页头", "持仓分析", 40, 22, 180, 26, { size: 22, weight: 800 });
ui.label(canvas, "页头说明", "账户：模拟组合 / 估值口径：人民币 / 推荐只生成订单草案", 160, 28, 520, 18, { size: 12, color: C.subtle });
ui.pill(canvas, "新手解释模式", 1106, 20, 112, "cyan");
ui.pill(canvas, "风险优先排序", 1230, 20, 108, "gold");
ui.pill(canvas, "订单需确认", 1350, 20, 72, "red");

const summary = ui.panel(canvas, "面板 / 持仓总览", 40, 96, 1360, 136, "持仓总览", "先看资产状态，再处理风险贡献最大的持仓。");
ui.stat(summary, "总资产", "¥510,240", "+2.31% 本月", 18, 56, 168, "green");
ui.stat(summary, "浮动盈亏", "¥18,420", "+3.74%", 202, 56, 168, "green");
ui.stat(summary, "现金仓位", "18%", "建议不低于15%", 386, 56, 168, "gold");
ui.stat(summary, "风险分", "68", "中等偏高", 570, 56, 168, "red");
ui.stat(summary, "最大回撤", "-8.6%", "近30日", 754, 56, 168, "red");
ui.stat(summary, "建议动作", "3 条", "需人工确认", 938, 56, 168, "cyan");
ui.rect(summary, "组合健康底", 1130, 56, 208, 72, { fill: C.bg2, stroke: C.border, radius: 6 });
ui.label(summary, "组合健康标题", "AI 组合健康度", 1144, 68, 120, 16, { size: 11, color: C.subtle });
ui.label(summary, "组合健康分", "B+", 1144, 89, 48, 30, { size: 24, weight: 800, color: C.gold, mono: true });
ui.progress(summary, "健康度进度", 1198, 99, 112, 0.72, C.gold);
ui.label(summary, "组合健康解释", "先降集中度，再考虑加仓", 1144, 119, 160, 14, { size: 10, color: C.subtle });

const table = ui.panel(canvas, "面板 / 持仓明细", 40, 256, 840, 436, "持仓明细", "按风险贡献排序，右侧建议是订单草案，不直接交易。");
const widths = [118, 78, 86, 86, 86, 92, 108, 140];
ui.row(table, "持仓表头", 60, [
  { text: "资产", weight: 700, color: C.text },
  { text: "仓位", weight: 700, color: C.text, align: "right" },
  { text: "成本", weight: 700, color: C.text, align: "right" },
  { text: "现价", weight: 700, color: C.text, align: "right" },
  { text: "盈亏", weight: 700, color: C.text, align: "right" },
  { text: "风险贡献", weight: 700, color: C.text },
  { text: "AI 建议", weight: 700, color: C.text },
  { text: "原因", weight: 700, color: C.text },
], widths, { fill: C.bg2, height: 34 });
[
  ["宁德时代", "22%", "189.4", "180.3", "-4.8%", "26", "减仓 5%", "集中度偏高"],
  ["贵州茅台", "16%", "1580", "1631", "+3.2%", "12", "继续持有", "波动较低"],
  ["沪深300ETF", "26%", "3.86", "3.90", "+1.1%", "18", "保留底仓", "分散风险"],
  ["BTC", "9%", "84,000", "91,240", "+8.6%", "24", "设止盈线", "波动放大"],
  ["黄金ETF", "5%", "4.88", "5.02", "+2.9%", "6", "小幅关注", "避险增强"],
  ["现金", "18%", "-", "-", "0.0%", "0", "保持", "留出缓冲"],
].forEach((cells, index) => {
  const y = 98 + index * 43;
  const pnlColor = cells[4].startsWith("+") ? C.green : cells[4].startsWith("-") ? C.red : C.muted;
  const riskNum = Number(cells[5]);
  const riskColor = riskNum >= 24 ? C.red : riskNum >= 14 ? C.gold : C.green;
  const actionColor = cells[6].includes("减") ? C.red : cells[6].includes("止盈") ? C.gold : C.cyan;
  ui.row(table, "持仓行 / " + cells[0], y, [
    { text: cells[0], color: C.text, weight: 700 },
    { text: cells[1], mono: true, align: "right" },
    { text: cells[2], mono: true, align: "right" },
    { text: cells[3], mono: true, align: "right" },
    { text: cells[4], mono: true, align: "right", color: pnlColor },
    { text: cells[5] + "%", mono: true, color: riskColor },
    { text: cells[6], color: actionColor, weight: 700 },
    { text: cells[7], color: C.muted },
  ], widths, { fill: index % 2 === 0 ? "#0F1828" : "transparent", height: 34 });
  ui.progress(table, "风险贡献 / " + cells[0], 458, y + 30, 64, Math.min(1, riskNum / 30), riskColor);
});
ui.label(table, "表格脚注", "新手提示：风险贡献不是涨跌幅，而是该资产对组合波动和回撤的影响。", 18, 398, 720, 18, { size: 11, color: C.subtle });

const alloc = ui.panel(canvas, "面板 / 仓位结构", 904, 256, 496, 204, "仓位结构", "用目标仓位约束 AI 建议，避免越涨越买。");
[
  ["A股个股", 0.38, C.gold, "目标 30-36%"],
  ["宽基ETF", 0.26, C.green, "目标 25-35%"],
  ["数字货币", 0.09, C.red, "上限 10%"],
  ["避险资产", 0.05, C.cyan, "目标 5-12%"],
  ["现金", 0.18, C.violet, "底线 15%"],
].forEach(([name, value, color, note], index) => {
  const y = 62 + index * 26;
  ui.label(alloc, "仓位名 / " + name, name, 18, y, 76, 16, { size: 12, color: C.muted });
  ui.progress(alloc, "仓位条 / " + name, 100, y + 5, 220, value, color);
  ui.label(alloc, "仓位值 / " + name, String(Math.round(value * 100)) + "%", 330, y, 42, 16, { size: 12, mono: true, color, align: "right" });
  ui.label(alloc, "仓位目标 / " + name, note, 386, y, 92, 16, { size: 11, color: C.subtle });
});

const risk = ui.panel(canvas, "面板 / 风险归因", 904, 484, 496, 208, "风险归因", "把风险翻译成可执行的处理顺序。");
[
  ["行业集中度", "新能源 + 白酒", 0.76, C.red, "先减仓"],
  ["资产相关性", "宁德时代 / 创业板", 0.64, C.gold, "观察"],
  ["币圈波动", "BTC 7日波动上升", 0.70, C.red, "设止盈"],
  ["现金缓冲", "18% 可承受回撤", 0.42, C.green, "合格"],
].forEach(([name, desc, value, color, action], index) => {
  const y = 66 + index * 34;
  ui.label(risk, "归因名 / " + name, name, 18, y, 86, 16, { size: 12, color: C.text, weight: 700 });
  ui.label(risk, "归因说明 / " + name, desc, 112, y, 150, 16, { size: 11, color: C.muted });
  ui.progress(risk, "归因条 / " + name, 270, y + 5, 106, value, color);
  ui.label(risk, "归因动作 / " + name, action, 390, y, 70, 16, { size: 12, color, align: "right" });
});

const drafts = ui.panel(canvas, "面板 / 调仓草案", 40, 724, 1360, 244, "调仓草案", "所有动作进入确认队列；系统只解释和草拟，不越权下单。");
[
  ["P1", "减仓", "宁德时代", "5%", "把新能源风险贡献从 26% 降到约 19%", "需要确认"],
  ["P2", "止盈", "BTC", "96,000 附近", "保护浮盈，避免高波动回撤吞掉利润", "自动提醒"],
  ["P3", "观察", "比亚迪", "不建仓", "信号候选，但组合已有新能源暴露", "等待回调"],
  ["P4", "保留", "沪深300ETF", "不变", "作为稳定底仓，降低单票风险", "无需动作"],
].forEach(([pri, act, asset, size, reason, state], index) => {
  const y = 64 + index * 40;
  ui.rect(drafts, "草案底 / " + asset, 18, y, 1324, 32, { fill: index % 2 === 0 ? C.bg2 : "#0F1828", stroke: C.border, radius: 6 });
  const color = act === "减仓" ? C.red : act === "止盈" ? C.gold : act === "观察" ? C.cyan : C.green;
  ui.label(drafts, "草案优先级 / " + asset, pri, 34, y + 8, 34, 16, { size: 12, mono: true, color: C.subtle });
  ui.label(drafts, "草案动作 / " + asset, act, 88, y + 8, 54, 16, { size: 12, weight: 800, color });
  ui.label(drafts, "草案资产 / " + asset, asset, 162, y + 8, 120, 16, { size: 12, color: C.text, weight: 700 });
  ui.label(drafts, "草案规模 / " + asset, size, 300, y + 8, 120, 16, { size: 12, mono: true, color: C.cyan });
  ui.label(drafts, "草案原因 / " + asset, reason, 450, y + 8, 610, 16, { size: 12, color: C.muted });
  ui.label(drafts, "草案状态 / " + asset, state, 1228, y + 8, 82, 16, { size: 12, color, align: "right" });
});
ui.rect(drafts, "确认全部按钮", 1120, 202, 96, 30, { fill: C.gold, stroke: "#D98705", radius: 6 });
ui.label(drafts, "确认全部文字", "确认草案", 1138, 210, 60, 14, { size: 12, weight: 700, color: "#111827", align: "center" });
ui.rect(drafts, "导出按钮", 1232, 202, 110, 30, { fill: C.panel2, stroke: C.border2, radius: 6 });
ui.label(drafts, "导出按钮文字", "导出给 Hermes", 1248, 210, 78, 14, { size: 12, weight: 700, color: C.text, align: "center" });
return {
  boardId: canvas.id,
  currentPage: penpot.currentPage && penpot.currentPage.name,
  childCount: root.children.length,
};
`;
}

function signalPageCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const root = ui.clearCurrentPage();
const canvas = ui.board(root, "Signal Lab / 信号解释 1440", 80, 80, 1440, 1024, { fill: C.bg, radius: 0 });
ui.rect(canvas, "顶部栏", 0, 0, 1440, 72, { fill: C.top });
ui.label(canvas, "页头", "信号实验室", 40, 22, 220, 26, { size: 22, weight: 800 });
ui.label(canvas, "页头说明", "把成熟库计算出的指标统一成 SignalSnapshot，Agent 只消费解释后的信号。", 206, 28, 620, 18, { size: 12, color: C.subtle });
ui.pill(canvas, "TA-Lib 优先", 1092, 20, 100, "cyan");
ui.pill(canvas, "ta 兜底", 1204, 20, 72, "gold");
ui.pill(canvas, "信号可追溯", 1288, 20, 100, "green");
const pipeline = ui.panel(canvas, "流程 / 信号流水线", 40, 104, 1360, 190, "信号流水线", "所有指标由成熟库计算，再统一映射为 SignalSnapshot。");
["原始数据", "复权清洗", "TA-Lib/ta", "因子归一", "风控覆盖", "Agent 消费"].forEach((step, index) => {
  const x = 30 + index * 215;
  ui.rect(pipeline, "流程节点 / " + step, x, 82, 150, 46, { fill: C.bg2, stroke: index === 2 ? C.cyan : C.border, radius: 6 });
  ui.label(pipeline, "流程文字 / " + step, step, x + 16, 96, 118, 18, { size: 13, weight: 700, color: index === 2 ? C.cyan : C.text, align: "center" });
  if (index < 5) {
    ui.rect(pipeline, "流程箭头线 / " + index, x + 164, 104, 36, 2, { fill: C.border2 });
    ui.rect(pipeline, "流程箭头点 / " + index, x + 198, 99, 8, 12, { fill: C.border2, radius: 2 });
  }
});
ui.label(pipeline, "流水线提示", "原则：指标计算不手写；我们只做数据准备、参数管理、归一化、解释和审计。", 30, 150, 760, 18, { size: 11, color: C.subtle });
ui.rect(pipeline, "协议徽标", 1120, 146, 190, 24, { fill: C.violetSoft, stroke: "#493B70", radius: 4 });
ui.label(pipeline, "协议徽标文字", "SignalSnapshot v1", 1144, 151, 142, 12, { size: 11, color: "#B9A2FF", mono: true, align: "center" });

const matrix = ui.panel(canvas, "矩阵 / 多资产信号", 40, 326, 880, 642, "多资产信号矩阵", "每个资产展示趋势、动量、波动、成交量、情绪、风控标签。");
const widths = [116, 92, 92, 92, 92, 92, 92, 136];
ui.row(matrix, "信号表头", 60, [
  { text: "资产", weight: 700, color: C.text },
  { text: "趋势", weight: 700, color: C.text },
  { text: "动量", weight: 700, color: C.text },
  { text: "波动", weight: 700, color: C.text },
  { text: "成交量", weight: 700, color: C.text },
  { text: "情绪", weight: 700, color: C.text },
  { text: "风控", weight: 700, color: C.text },
  { text: "结论", weight: 700, color: C.text },
], widths, { fill: C.bg2, height: 34 });
[
  ["宁德时代", "42", "38", "76", "51", "48", "高", "减仓优先"],
  ["贵州茅台", "55", "50", "32", "44", "62", "低", "继续持有"],
  ["沪深300ETF", "63", "57", "39", "53", "52", "低", "底仓保留"],
  ["BTC", "78", "72", "84", "68", "61", "高", "强势但限仓"],
  ["黄金ETF", "66", "58", "41", "49", "70", "中", "避险关注"],
  ["比亚迪", "69", "64", "73", "71", "55", "中高", "候选不追"],
  ["SOL", "81", "79", "90", "77", "54", "极高", "降权观察"],
].forEach((cells, index) => {
  const y = 100 + index * 52;
  const trend = Number(cells[1]);
  const momentum = Number(cells[2]);
  const vol = Number(cells[3]);
  const volume = Number(cells[4]);
  const sentiment = Number(cells[5]);
  const riskText = cells[6];
  const riskColor = riskText.includes("极") || riskText.includes("高") ? C.red : riskText === "中" ? C.gold : C.green;
  ui.rect(matrix, "信号行底 / " + cells[0], 0, y, 804, 42, { fill: index % 2 === 0 ? "#0F1828" : "transparent" });
  ui.label(matrix, "信号资产 / " + cells[0], cells[0], 12, y + 13, 98, 16, { size: 12, color: C.text, weight: 700 });
  [[trend, C.green, 124, "趋势"], [momentum, C.gold, 216, "动量"], [vol, C.red, 308, "波动"], [volume, C.cyan, 400, "成交量"], [sentiment, C.violet, 492, "情绪"]].forEach(([value, color, x, name]) => {
    ui.progress(matrix, name + " / " + cells[0], x, y + 17, 56, value / 100, color);
    ui.label(matrix, name + "值 / " + cells[0], String(value), x + 58, y + 11, 24, 16, { size: 11, mono: true, color });
  });
  ui.label(matrix, "风控 / " + cells[0], riskText, 596, y + 13, 64, 16, { size: 12, color: riskColor, weight: 700 });
  ui.label(matrix, "结论 / " + cells[0], cells[7], 688, y + 13, 126, 16, { size: 12, color: cells[7].includes("减") || cells[7].includes("降") ? C.red : cells[7].includes("限") ? C.gold : C.cyan });
});
ui.label(matrix, "矩阵脚注", "数值范围 0-100：不是单个指标原值，而是按资产类型归一化后的信号强度。", 18, 592, 760, 18, { size: 11, color: C.subtle });

const explain = ui.panel(canvas, "解释 / 单资产展开", 948, 326, 452, 642, "单资产解释", "给小白看的自然语言解释，同时保留指标来源。");
ui.label(explain, "选中资产", "当前选中：宁德时代", 18, 62, 260, 24, { size: 18, weight: 800, color: C.text });
ui.pill(explain, "建议：减仓 5%", 308, 58, 116, "red");
ui.rect(explain, "解释主结论底", 18, 102, 416, 78, { fill: C.bg2, stroke: C.border, radius: 6 });
ui.label(explain, "解释主结论标题", "为什么不是继续补仓？", 34, 118, 240, 18, { size: 14, weight: 800, color: C.text });
ui.label(explain, "解释主结论正文", "趋势和动量没有确认反转，波动因子却明显偏高；你的组合里新能源占比已经偏重，继续补仓会放大回撤。", 34, 144, 368, 32, { size: 12, color: C.muted, lineHeight: 1.45 });
[
  ["趋势", "MA20 下方，ADX 走弱", C.gold],
  ["动量", "RSI 42，MACD 仍在零轴下", C.red],
  ["波动", "ATR 百分位 76，高于近半年中位数", C.red],
  ["成交量", "放量下跌后缩量反弹", C.gold],
  ["风控", "单资产风险贡献 26%，超过阈值", C.red],
].forEach(([name, desc, color], index) => {
  const y = 210 + index * 54;
  ui.rect(explain, "解释因子底 / " + name, 18, y, 416, 40, { fill: "#0F1828", stroke: C.border, radius: 6 });
  ui.rect(explain, "解释因子点 / " + name, 32, y + 15, 10, 10, { fill: color, radius: 5 });
  ui.label(explain, "解释因子名 / " + name, name, 52, y + 10, 56, 16, { size: 12, weight: 800, color });
  ui.label(explain, "解释因子描述 / " + name, desc, 116, y + 10, 292, 16, { size: 12, color: C.muted });
});

const quality = ui.panel(canvas, "面板 / 数据质量与来源", 948, 826, 452, 142, "数据质量与来源", "Agent 报告必须说明数据新鲜度和缺口。");
[
  ["行情数据", "T+0 / 09:45:31", C.green],
  ["复权因子", "已校验", C.green],
  ["财务数据", "T+1 更新", C.gold],
  ["新闻情绪", "样本偏少", C.gold],
].forEach(([name, state, color], index) => {
  const x = 18 + (index % 2) * 208;
  const y = 58 + Math.floor(index / 2) * 38;
  ui.rect(quality, "质量项底 / " + name, x, y, 190, 28, { fill: C.bg2, stroke: C.border, radius: 6 });
  ui.rect(quality, "质量点 / " + name, x + 12, y + 10, 8, 8, { fill: color, radius: 4 });
  ui.label(quality, "质量名 / " + name, name, x + 28, y + 7, 70, 14, { size: 11, color: C.text, weight: 700 });
  ui.label(quality, "质量状态 / " + name, state, x + 102, y + 7, 74, 14, { size: 11, color, align: "right" });
});
return {
  boardId: canvas.id,
  currentPage: penpot.currentPage && penpot.currentPage.name,
  childCount: root.children.length,
};
`;
}

function reportPageCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const root = ui.clearCurrentPage();
const canvas = ui.board(root, "Agent Report / AI报告 1440", 80, 80, 1440, 1024, { fill: C.bg, radius: 0 });
ui.rect(canvas, "顶部栏", 0, 0, 1440, 72, { fill: C.top });
ui.label(canvas, "页头", "AI 分析报告", 40, 22, 220, 26, { size: 22, weight: 800 });
ui.label(canvas, "页头说明", "报告时间：2026-05-13 09:45 / 面向金融小白的可解释投资建议 / 仅生成订单草案", 212, 28, 720, 18, { size: 12, color: C.subtle });
ui.pill(canvas, "置信度 72", 1092, 20, 86, "gold");
ui.pill(canvas, "风控通过 3/4", 1190, 20, 106, "cyan");
ui.pill(canvas, "需人工确认", 1308, 20, 100, "red");
storage.hermesFinance.reportBoardId = canvas.id;
return {
  boardId: canvas.id,
  currentPage: penpot.currentPage && penpot.currentPage.name,
  childCount: root.children.length,
};
`;
}

function reportSummaryEvidenceCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.reportBoardId);
if (!canvas) throw new Error("找不到 AI 报告画板");
const summary = ui.panel(canvas, "报告 / 今日摘要", 40, 104, 640, 232, "今日摘要", "一句话结论、建议动作、主要风险。");
ui.label(summary, "摘要主结论", "结论：谨慎增持，但先降风险再加仓", 18, 62, 460, 32, { size: 22, weight: 800, color: C.text });
ui.label(summary, "摘要正文", "组合整体仍可持有，但新能源和数字货币的波动贡献偏高。建议先减仓宁德时代 5%，为 BTC 设置止盈线，再观察比亚迪，不建议追高。", 18, 104, 570, 44, { size: 13, color: C.muted, lineHeight: 1.5 });
ui.stat(summary, "推荐动作", "3", "2 条需确认", 18, 154, 130, "cyan");
ui.stat(summary, "预估风险分", "68 -> 59", "执行后", 164, 154, 150, "green");
ui.stat(summary, "现金仓位", "18%", "保留缓冲", 330, 154, 130, "gold");
ui.stat(summary, "最大单票", "22%", "偏高", 476, 154, 130, "red");

const evidence = ui.panel(canvas, "报告 / 推荐依据", 708, 104, 692, 232, "推荐依据", "把 AI 的判断拆成数据、信号、风控、反例。");
[
  ["数据", "行情已刷新；复权因子已校验；新闻样本偏少", C.green],
  ["信号", "趋势中性偏弱，波动因子抬升，成交量确认不足", C.gold],
  ["风控", "宁德时代风险贡献 26%，BTC 波动分 84", C.red],
  ["反例", "若新能源板块放量突破，减仓可能错过短线反弹", C.cyan],
].forEach(([name, desc, color], index) => {
  const y = 62 + index * 38;
  ui.rect(evidence, "依据项底 / " + name, 18, y, 650, 30, { fill: index % 2 === 0 ? C.bg2 : "#0F1828", stroke: C.border, radius: 6 });
  ui.rect(evidence, "依据点 / " + name, 34, y + 11, 8, 8, { fill: color, radius: 4 });
  ui.label(evidence, "依据名 / " + name, name, 52, y + 7, 48, 14, { size: 12, weight: 800, color });
  ui.label(evidence, "依据描述 / " + name, desc, 116, y + 7, 510, 14, { size: 12, color: C.muted });
});
ui.label(evidence, "依据脚注", "报告必须展示反例：AI 需要告诉用户什么情况下建议会失效。", 18, 204, 520, 16, { size: 11, color: C.subtle });
return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function reportDetailsCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.reportBoardId);
if (!canvas) throw new Error("找不到 AI 报告画板");
const details = ui.panel(canvas, "报告 / 资产明细", 40, 368, 860, 600, "资产明细", "逐个解释为什么推荐持有、减仓、观望。");
const widths = [110, 74, 78, 92, 100, 124, 330];
ui.row(details, "资产明细表头", 60, [
  { text: "资产", weight: 700, color: C.text },
  { text: "动作", weight: 700, color: C.text },
  { text: "仓位", weight: 700, color: C.text, align: "right" },
  { text: "信号分", weight: 700, color: C.text },
  { text: "风险", weight: 700, color: C.text },
  { text: "置信度", weight: 700, color: C.text },
  { text: "给小白看的解释", weight: 700, color: C.text },
], widths, { fill: C.bg2, height: 34 });
[
  ["宁德时代", "减仓", "22%", "42", "高", "74", "它不是不能买，而是你已经买得偏多；先把风险降下来。"],
  ["BTC", "止盈", "9%", "78", "高", "69", "趋势强但波动很大，适合设置保护线，不适合继续追。"],
  ["沪深300ETF", "持有", "26%", "63", "低", "77", "它像组合里的压舱石，可以分散个股风险。"],
  ["贵州茅台", "持有", "16%", "55", "低", "71", "波动较低，当前没有强烈卖出信号。"],
  ["比亚迪", "观察", "0%", "69", "中高", "61", "信号不错，但和现有新能源持仓高度相关。"],
  ["现金", "保留", "18%", "-", "低", "90", "现金不是浪费，它是给回撤和机会留空间。"],
].forEach((cells, index) => {
  const y = 100 + index * 58;
  const actionColor = cells[1] === "减仓" ? C.red : cells[1] === "止盈" ? C.gold : cells[1] === "观察" ? C.cyan : C.green;
  const riskColor = cells[4].includes("高") ? C.red : cells[4] === "低" ? C.green : C.gold;
  ui.rect(details, "资产明细底 / " + cells[0], 0, y, 818, 44, { fill: index % 2 === 0 ? "#0F1828" : "transparent" });
  ui.label(details, "资产 / " + cells[0], cells[0], 12, y + 14, 98, 16, { size: 12, weight: 700, color: C.text });
  ui.label(details, "动作 / " + cells[0], cells[1], 122, y + 14, 58, 16, { size: 12, weight: 800, color: actionColor });
  ui.label(details, "仓位 / " + cells[0], cells[2], 196, y + 14, 54, 16, { size: 12, mono: true, color: C.muted, align: "right" });
  if (cells[3] !== "-") {
    ui.progress(details, "信号分 / " + cells[0], 274, y + 18, 50, Number(cells[3]) / 100, actionColor);
    ui.label(details, "信号值 / " + cells[0], cells[3], 328, y + 12, 24, 16, { size: 11, mono: true, color: actionColor });
  } else {
    ui.label(details, "信号值 / " + cells[0], "-", 310, y + 12, 24, 16, { size: 11, mono: true, color: C.subtle });
  }
  ui.label(details, "风险 / " + cells[0], cells[4], 378, y + 14, 62, 16, { size: 12, color: riskColor, weight: 700 });
  ui.progress(details, "置信度 / " + cells[0], 468, y + 18, 60, Number(cells[5]) / 100, C.cyan);
  ui.label(details, "置信度值 / " + cells[0], cells[5] + "%", 532, y + 12, 40, 16, { size: 11, mono: true, color: C.cyan });
  ui.label(details, "解释 / " + cells[0], cells[6], 600, y + 8, 210, 28, { size: 11, color: C.muted, lineHeight: 1.35 });
});
ui.rect(details, "报告说明底", 18, 540, 824, 38, { fill: C.violetSoft, stroke: "#493B70", radius: 6 });
ui.label(details, "报告说明", "所有建议都引用 SignalSnapshot、RiskDecision、OrderDraft 三类协议；后端可以把这页内容直接转成 Hermes 可读 JSON。", 34, 552, 760, 14, { size: 11, color: "#B9A2FF" });
return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function reportApprovalCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.reportBoardId);
if (!canvas) throw new Error("找不到 AI 报告画板");
const approval = ui.panel(canvas, "报告 / 审批清单", 928, 368, 472, 600, "审批清单", "订单草案、确认门槛、需要用户补充的信息。");
ui.label(approval, "审批状态标题", "本次需要你确认 2 个动作", 18, 62, 300, 22, { size: 18, weight: 800, color: C.text });
ui.label(approval, "审批状态说明", "系统不会自动下单；确认后只把草案交给 Hermes 或交易适配器。", 18, 92, 390, 18, { size: 12, color: C.muted });
[
  ["减仓", "宁德时代", "5%", "通过", C.red],
  ["止盈", "BTC", "96,000", "通过", C.gold],
  ["观察", "比亚迪", "不建仓", "无需确认", C.cyan],
].forEach(([act, asset, size, state, color], index) => {
  const y = 130 + index * 58;
  ui.rect(approval, "审批项底 / " + asset, 18, y, 436, 46, { fill: "#0F1828", stroke: C.border, radius: 6 });
  ui.label(approval, "审批动作 / " + asset, act, 34, y + 14, 48, 16, { size: 12, weight: 800, color });
  ui.label(approval, "审批资产 / " + asset, asset, 96, y + 14, 96, 16, { size: 12, color: C.text, weight: 700 });
  ui.label(approval, "审批规模 / " + asset, size, 208, y + 14, 86, 16, { size: 12, mono: true, color: C.cyan });
  ui.label(approval, "审批状态 / " + asset, state, 338, y + 14, 84, 16, { size: 12, color, align: "right" });
});

ui.label(approval, "风控检查标题", "风控检查", 18, 326, 160, 18, { size: 14, weight: 800, color: C.text });
[
  ["单票上限", "执行后 17%", C.green],
  ["币圈上限", "BTC 9%，未超 10%", C.green],
  ["现金底线", "18%，高于 15%", C.green],
  ["新闻样本", "偏少，需保守", C.gold],
].forEach(([name, state, color], index) => {
  const y = 360 + index * 34;
  ui.rect(approval, "检查点 / " + name, 18, y + 5, 8, 8, { fill: color, radius: 4 });
  ui.label(approval, "检查名 / " + name, name, 36, y, 90, 16, { size: 12, color: C.muted });
  ui.label(approval, "检查状态 / " + name, state, 150, y, 250, 16, { size: 12, color, align: "right" });
});

ui.rect(approval, "批准按钮", 238, 540, 96, 32, { fill: C.gold, stroke: "#D98705", radius: 6 });
ui.label(approval, "批准按钮文字", "批准草案", 256, 549, 60, 14, { size: 12, weight: 700, color: "#111827", align: "center" });
ui.rect(approval, "复核按钮", 350, 540, 86, 32, { fill: C.panel2, stroke: C.border2, radius: 6 });
ui.label(approval, "复核按钮文字", "要求复核", 364, 549, 58, 14, { size: 12, weight: 700, color: C.text, align: "center" });
return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function collaborationPageCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const root = ui.clearCurrentPage();
const canvas = ui.board(root, "Agent Collaboration / 协作进展 1440", 80, 80, 1440, 1024, { fill: C.bg, radius: 0 });
ui.rect(canvas, "顶部栏", 0, 0, 1440, 72, { fill: C.top });
ui.label(canvas, "页头", "Agent 协作进展", 40, 22, 240, 26, { size: 22, weight: 800 });
ui.label(canvas, "页头说明", "展示 AI 助手之间的任务进展、公开讨论摘要、分歧点和最终共识。", 250, 28, 620, 18, { size: 12, color: C.subtle });
ui.pill(canvas, "实时协作中", 1088, 20, 100, "green");
ui.pill(canvas, "6 个 Agent", 1200, 20, 86, "cyan");
ui.pill(canvas, "可审计摘要", 1298, 20, 108, "gold");
storage.hermesFinance.collaborationBoardId = canvas.id;
return {
  boardId: canvas.id,
  currentPage: penpot.currentPage && penpot.currentPage.name,
  childCount: root.children.length,
};
`;
}

function collaborationAgentsCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.collaborationBoardId);
if (!canvas) throw new Error("找不到 Agent 协作画板");

const map = ui.panel(canvas, "面板 / Agent 协作地图", 40, 104, 880, 350, "Agent 协作地图", "看到每个 AI 小人负责什么，以及当前卡在哪一步。");
[
  ["数据管家", "刷新行情 / 清洗复权", "完成", C.green, 46, 82],
  ["信号分析师", "计算趋势动量波动", "完成", C.green, 302, 82],
  ["风险官", "仓位与回撤检查", "复核中", C.gold, 558, 82],
  ["基本面研究员", "财报与估值摘要", "样本不足", C.gold, 174, 212],
  ["交易草案员", "生成订单草案", "等待共识", C.cyan, 430, 212],
  ["总协调员", "裁决分歧 / 出报告", "进行中", C.violet, 686, 212],
].forEach(([name, task, state, color, x, y], index) => {
  ui.rect(map, "Agent 节点底 / " + name, x, y, 178, 92, { fill: C.bg2, stroke: color, radius: 8, strokeOpacity: 0.7 });
  ui.rect(map, "Agent 头像 / " + name, x + 16, y + 18, 38, 38, { fill: color, opacity: 0.18, stroke: color, radius: 19 });
  ui.label(map, "Agent 头像字 / " + name, name.slice(0, 2), x + 22, y + 29, 26, 14, { size: 12, weight: 800, color, align: "center" });
  ui.label(map, "Agent 名 / " + name, name, x + 66, y + 18, 92, 18, { size: 13, weight: 800, color: C.text });
  ui.label(map, "Agent 任务 / " + name, task, x + 66, y + 42, 96, 26, { size: 11, color: C.muted, lineHeight: 1.3 });
  ui.label(map, "Agent 状态 / " + name, state, x + 16, y + 68, 140, 14, { size: 11, color, align: "center" });
});
[
  [224, 128, 302, 128],
  [480, 128, 558, 128],
  [642, 174, 724, 212],
  [352, 258, 430, 258],
  [608, 258, 686, 258],
].forEach(([x1, y1, x2, y2], index) => {
  ui.rect(map, "协作连线 / " + index, x1, y1, x2 - x1, 2, { fill: C.border2 });
  ui.rect(map, "协作箭头 / " + index, x2 - 8, y2 - 4, 8, 10, { fill: C.border2, radius: 2 });
});

const progress = ui.panel(canvas, "面板 / 当前任务进度", 948, 104, 452, 350, "当前任务进度", "用户看到的是可解释进度，不是黑盒等待。");
[
  ["行情刷新", 1, C.green, "已完成，A股延迟行情 + Crypto 实时行情"],
  ["指标计算", 1, C.green, "TA-Lib 主路径完成，ta 兜底未触发"],
  ["风险复核", 0.72, C.gold, "集中度偏高，等待总协调员裁决"],
  ["报告生成", 0.58, C.cyan, "摘要已生成，审批清单待确认"],
  ["Hermes 输出", 0.22, C.violet, "等待订单草案确认后推送"],
].forEach(([name, value, color, note], index) => {
  const y = 64 + index * 52;
  ui.label(progress, "进度名 / " + name, name, 18, y, 86, 16, { size: 12, weight: 700, color: C.text });
  ui.progress(progress, "进度条 / " + name, 112, y + 5, 150, value, color);
  ui.label(progress, "进度值 / " + name, String(Math.round(value * 100)) + "%", 274, y, 42, 16, { size: 12, mono: true, color, align: "right" });
  ui.label(progress, "进度说明 / " + name, note, 18, y + 22, 398, 16, { size: 11, color: C.subtle });
});
return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function collaborationDiscussionCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.collaborationBoardId);
if (!canvas) throw new Error("找不到 Agent 协作画板");

const discussion = ui.panel(canvas, "面板 / 讨论摘要", 40, 484, 880, 352, "Agent 讨论摘要", "展示可公开的讨论摘要：观点、证据、分歧，不展示原始链式思考。");
[
  ["信号分析师", "宁德时代趋势偏弱，动量没有确认反转；BTC 趋势强但波动高。", C.cyan],
  ["风险官", "组合新能源暴露偏高，若继续补仓，最大回撤压力会超过用户风险偏好。", C.red],
  ["基本面研究员", "财报数据没有明显恶化，但新闻样本偏少，不能把短线反弹当趋势反转。", C.gold],
  ["交易草案员", "建议宁德时代减仓 5%，BTC 设置止盈线，沪深300ETF 保留底仓。", C.green],
  ["总协调员", "采纳风控优先原则：先降风险，再小幅观察机会池，不追高。", C.violet],
].forEach(([role, text, color], index) => {
  const y = 62 + index * 52;
  ui.rect(discussion, "消息底 / " + role + index, 18, y, 844, 40, { fill: index % 2 === 0 ? C.bg2 : "#0F1828", stroke: C.border, radius: 6 });
  ui.rect(discussion, "消息头像 / " + role + index, 34, y + 10, 20, 20, { fill: color, opacity: 0.2, stroke: color, radius: 10 });
  ui.label(discussion, "消息角色 / " + role + index, role, 66, y + 11, 98, 14, { size: 11, weight: 800, color });
  ui.label(discussion, "消息文本 / " + role + index, text, 174, y + 8, 650, 24, { size: 12, color: C.muted, lineHeight: 1.35 });
});
ui.rect(discussion, "讨论边界提示底", 18, 312, 844, 24, { fill: C.violetSoft, stroke: "#493B70", radius: 4 });
ui.label(discussion, "讨论边界提示", "说明：这里是可审计摘要，不展示模型隐藏推理；每条摘要都应能关联数据、信号或风控证据。", 34, 318, 780, 12, { size: 10, color: "#B9A2FF" });

const disputes = ui.panel(canvas, "面板 / 分歧与裁决", 948, 484, 452, 352, "分歧与裁决", "让用户知道 AI 不是一拍脑袋，而是经过争议处理。");
[
  ["分歧 1", "BTC 是否继续加仓？", "不加仓，改为止盈保护", C.gold],
  ["分歧 2", "宁德时代是否补仓？", "不补仓，先减 5%", C.red],
  ["分歧 3", "是否新增比亚迪？", "列入机会池，等待回调", C.cyan],
].forEach(([label, question, decision, color], index) => {
  const y = 64 + index * 76;
  ui.rect(disputes, "分歧底 / " + label, 18, y, 416, 58, { fill: "#0F1828", stroke: C.border, radius: 6 });
  ui.label(disputes, "分歧标签 / " + label, label, 34, y + 10, 54, 14, { size: 11, mono: true, color });
  ui.label(disputes, "分歧问题 / " + label, question, 98, y + 10, 260, 14, { size: 12, color: C.text, weight: 700 });
  ui.label(disputes, "分歧裁决 / " + label, decision, 98, y + 32, 260, 14, { size: 12, color });
});
ui.rect(disputes, "裁决按钮", 284, 302, 132, 30, { fill: C.panel2, stroke: C.border2, radius: 6 });
ui.label(disputes, "裁决按钮文字", "查看证据链", 314, 310, 72, 14, { size: 12, weight: 700, color: C.text, align: "center" });
return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function collaborationTimelineCode() {
  return String.raw`
const ui = storage.hermesFinance;
const C = ui.C;
const canvas = penpotUtils.findShapeById(storage.hermesFinance.collaborationBoardId);
if (!canvas) throw new Error("找不到 Agent 协作画板");

const timeline = ui.panel(canvas, "面板 / 决策轨迹", 40, 864, 1360, 104, "决策轨迹", "从数据刷新到报告输出的可视化轨迹。");
[
  ["09:30", "数据刷新", C.green],
  ["09:34", "信号计算", C.green],
  ["09:38", "风险复核", C.gold],
  ["09:41", "Agent 讨论", C.cyan],
  ["09:44", "形成共识", C.violet],
  ["09:45", "等待确认", C.red],
].forEach(([time, step, color], index) => {
  const x = 32 + index * 214;
  ui.rect(timeline, "轨迹点 / " + step, x, 58, 14, 14, { fill: color, radius: 7 });
  if (index < 5) {
    ui.rect(timeline, "轨迹线 / " + index, x + 18, 64, 174, 2, { fill: C.border2 });
  }
  ui.label(timeline, "轨迹时间 / " + step, time, x - 6, 28, 46, 14, { size: 11, mono: true, color: C.subtle, align: "center" });
  ui.label(timeline, "轨迹步骤 / " + step, step, x - 28, 78, 72, 14, { size: 11, color, align: "center" });
});
ui.rect(timeline, "Hermes 推送状态", 1168, 24, 160, 28, { fill: C.bg2, stroke: C.border, radius: 6 });
ui.label(timeline, "Hermes 推送状态文字", "Hermes：等待用户确认", 1184, 32, 128, 12, { size: 11, color: C.gold, align: "center" });
return {
  boardId: canvas.id,
  childCount: canvas.children.length,
};
`;
}

function inspectCurrentPageCode() {
  return String.raw`
return {
  currentPage: penpot.currentPage && penpot.currentPage.name,
  childCount: penpot.root ? penpot.root.children.length : -1,
  children: penpot.root ? penpot.root.children.map((shape) => ({
    id: shape.id,
    name: shape.name,
    type: shape.type,
    childCount: shape.children ? shape.children.length : 0,
  })) : [],
  pages: penpotUtils.getPages(),
};
`;
}

async function main() {
  await postRpc({
    jsonrpc: "2.0",
    id: nextId++,
    method: "initialize",
    params: {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: {
        name: "hermes-finance-penpot-generator",
        version: "1.1.0",
      },
    },
  });

  await postRpc({
    jsonrpc: "2.0",
    method: "notifications/initialized",
    params: {},
  });

  const install = await execute(installRuntimeCode());

  await switchToPage("00 Design System");
  const design = await execute(designSystemCode());

  await switchToPage("01 Dashboard 总览");
  const dashboardBase = await execute(dashboardBaseCode());
  const dashboardTop = await execute(dashboardTopPanelsCode());
  const dashboardBottom = await execute(dashboardBottomPanelsCode());

  const skeleton = await execute(skeletonPagesCode());

  await switchToPage("02 Portfolio 持仓分析");
  const portfolio = await execute(portfolioPageCode());

  await switchToPage("03 Signal Lab 信号解释");
  const signal = await execute(signalPageCode());

  await switchToPage("04 Agent Report AI报告");
  const report = await execute(reportPageCode());
  const reportSummaryEvidence = await execute(reportSummaryEvidenceCode());
  const reportDetails = await execute(reportDetailsCode());
  const reportApproval = await execute(reportApprovalCode());

  await switchToPage("05 Agent 协作进展");
  const collaboration = await execute(collaborationPageCode());
  const collaborationAgents = await execute(collaborationAgentsCode());
  const collaborationDiscussion = await execute(collaborationDiscussionCode());
  const collaborationTimeline = await execute(collaborationTimelineCode());

  await switchToPage("01 Dashboard 总览");

  let exportResult = null;
  try {
    exportResult = await callTool("export_shape", {
      shapeId: dashboardBase.boardId,
      format: "png",
      mode: "shape",
      filePath: exportPath,
    });
  } catch (error) {
    exportResult = { error: error instanceof Error ? error.message : String(error) };
  }

  const verify = await execute(
    "return { pages: penpotUtils.getPages(), currentPage: penpot.currentPage && penpot.currentPage.name, root: penpotUtils.shapeStructure(penpot.root, 2), dashboard: penpotUtils.shapeStructure(penpotUtils.findShapeById(storage.hermesFinance.dashboardBoardId), 2) };",
  );

  const pageChecks = {};
  for (const pageName of [
    "00 Design System",
    "01 Dashboard 总览",
    "02 Portfolio 持仓分析",
    "03 Signal Lab 信号解释",
    "04 Agent Report AI报告",
    "05 Agent 协作进展",
  ]) {
    await switchToPage(pageName);
    pageChecks[pageName] = await execute(inspectCurrentPageCode());
  }

  await switchToPage("01 Dashboard 总览");

  process.stdout.write(
    JSON.stringify(
      {
        ok: true,
        exportPath,
        install,
        designBoardId: design.boardId,
        dashboardBoardId: dashboardBase.boardId,
        dashboardTop,
        dashboardBottom,
        skeleton,
        portfolio,
        signal,
        report,
        reportSummaryEvidence,
        reportDetails,
        reportApproval,
        collaboration,
        collaborationAgents,
        collaborationDiscussion,
        collaborationTimeline,
        export: exportResult,
        verify,
        pageChecks,
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  process.stderr.write((error instanceof Error ? error.stack : String(error)) + "\n");
  process.exit(1);
});
