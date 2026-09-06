/**
 * 第三方文本进 agent 上下文前的字符层清洗 + `<perception_data>` 围栏（TS 侧镜像）。
 *
 * 规则与 backend `miloco/perception/fence.py` 1:1：去零宽 / 双向控制 / tag 字符；C0/C1 控制符
 * 与 U+2800 等“渲染为空白”的填充符折成空格；能拼出标签形状的全角字母 / 数字 / `<>/|_`
 * 折半角（刻意不做 NFKC——它会把中文全角标点折成半角、改写住户可见文案）；`<perception_data`
 * 围栏标记与 `<system>` / `<|…|>` 类特殊 token 删到不动点（轮次有上限，超限破坏性兜底）；
 * `\n\nHuman:` 一类伪造轮次标记去牙。改动任一侧务必同步另一侧。
 *
 * 用在插件自己往 prompt 里贴的第三方材料上：今日感知日志（digest LLM 写的 markdown）、
 * 待回应习惯建议（habit-suggest LLM 写的 title / suggestion）。后端发来的感知消息在后端
 * 已经清洗 + 围栏，这里不重复处理。
 */

/** 围栏标签：源码常量，与后端 `PERCEPTION_LABEL` 同名。 */
export const PERCEPTION_LABEL = "perception_data";

// 零宽 / 双向 / 格式控制字符 → 删除（无宽度，删掉能让拆字的标记重新拼回被认出）。
const INVISIBLE =
  /[\u00AD\u034F\u061C\u180B-\u180E\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\u206A-\u206F\uFE00-\uFE0F\uFEFF\uFFF9-\uFFFB\u{E0000}-\u{E007F}\u{E0100}-\u{E01EF}]/gu;

// “渲染为空白 / 不可打印”但并非零宽 → 折成空格（保留 \t \n \r）。
const BLANK =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u115F\u1160\u17B4\u17B5\u2028\u2029\u2800\u3164\uFFA0]/g;

// 全角 → 半角，只折能拼出标签形状的那一组。
const FULLWIDTH = /[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A\uFF1C\uFF1E\uFF0F\uFF5C\uFF3F]/g;
function foldFullwidth(ch: string): string {
  return String.fromCharCode(ch.charCodeAt(0) - 0xfee0);
}

// 伪造轮次边界：空行 + 完整角色词 + 冒号。
const TURN_INDICATOR =
  /((?:\r\n|\r|\n)[ \t]*(?:\r\n|\r|\n)[ \t]*)(human|assistant|system|user)[ \t]*:/gi;
const LEADING_TURN_INDICATOR = /^(\s*)(human|assistant|system|user)[ \t]*:/i;

// 对话 / 工具调用标记，只认标签形状；量词有界、不相邻。
const TAG_ATTRS =
  "(?:[ \\t]+[\\w:.-]{1,40}[ \\t]*=[ \\t]*(?:\"[^\"]{0,200}\"|'[^']{0,200}'|[^\\s\"'>]{1,200})){0,8}";
const SPECIAL_TOKEN = new RegExp(
  "<[ \\t]*/?[ \\t]*(?:" +
    "(?:[a-z][\\w.-]{0,30}:)?(?:transcript|conversation|function_calls|function_results" +
    "|invoke|tool_use|tool_result|system|human|user|assistant)" +
    "|[a-z][\\w.-]{0,30}:(?:parameter|result)" +
    `)\\b${TAG_ATTRS}[ \\t]*/?>` +
    "|<\\|[^|<>\\r\\n]{1,64}\\|>",
  "gi",
);
const MARKER = new RegExp(`<\\s*/?\\s*${PERCEPTION_LABEL}(?![A-Za-z0-9_])(?:[^<>]{0,200}>)?`, "gi");

const MAX_STRIP_PASSES = 5;

function stripMarkers(text: string): string {
  for (let i = 0; i < MAX_STRIP_PASSES; i++) {
    const stripped = text.replace(MARKER, "[removed]").replace(SPECIAL_TOKEN, "[removed]");
    if (stripped === text) return text;
    text = stripped;
  }
  // 逐层嵌套的敌意输入：破坏性兜底，把残余 <>| 折成空格。
  MARKER.lastIndex = 0;
  SPECIAL_TOKEN.lastIndex = 0;
  if (MARKER.test(text) || SPECIAL_TOKEN.test(text)) {
    text = text.replace(/[<>|]/g, " ");
  }
  MARKER.lastIndex = 0;
  SPECIAL_TOKEN.lastIndex = 0;
  return text;
}

/**
 * 字符层清洗；保留换行（多行 markdown 也走这里）。`maxChars` 含截断后缀，截断放最后。
 */
export function sanitizeForPrompt(text: string, maxChars?: number): string {
  if (!text) return text;
  let out = text.replace(FULLWIDTH, foldFullwidth).replace(INVISIBLE, "").replace(BLANK, " ");
  out = stripMarkers(out);
  out = out.replace(TURN_INDICATOR, "$1$2 -");
  if (maxChars !== undefined && out.length > maxChars) {
    const suffix = "…[truncated]";
    out = maxChars > suffix.length ? out.slice(0, maxChars - suffix.length) + suffix : out.slice(0, maxChars);
  }
  return out;
}

/** 单行 free-text 进 key:value 行：清洗 + 折叠所有空白为单空格（对齐后端 `oneline`）。 */
export function onelineForPrompt(text: string, maxChars?: number): string {
  if (!text) return text;
  return sanitizeForPrompt(text, maxChars).split(/\s+/).filter(Boolean).join(" ");
}

/** 清洗后放进 `<perception_data>\n…\n</perception_data>`。 */
export function fenceForPrompt(text: string, maxChars?: number): string {
  const body = sanitizeForPrompt(text, maxChars).replace(LEADING_TURN_INDICATOR, "$1$2 -");
  return `<${PERCEPTION_LABEL}>\n${body}\n</${PERCEPTION_LABEL}>`;
}
