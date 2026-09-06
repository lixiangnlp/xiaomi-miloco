import fs from "node:fs";
import path from "node:path";
import { kPluginRootDir } from "../config.js";
import { logger } from "../utils/logger.js";

/**
 * 读取插件自带 skill 的 SKILL.md 正文，供 before_prompt_build 按 profile 预注入。
 *
 * 背景：rule / suggestion 会话几乎总以一次通知收尾，原先 agent 要先花一轮加载
 * `miloco-notify`、再花一轮加载 `miloco-devices`（TTS 经它下发），危险预警的热路径
 * 白白多两次模型往返。profile 是 harness 在首轮之前就掌握的信号，故直接把 skill
 * 正文塞进系统上下文，省掉加载回合（见 knowledge/03-features/openclaw-integration.md
 * “Hook 机制”）。
 *
 * 路径解析：`openclaw.plugin.json` 以 `"skills": ["./skills"]` 声明 skill 目录，
 * `scripts/sync-skills.mjs` 在 prebuild 时把 monorepo 的 `plugins/skills/` 复制到
 * `<plugin>/skills/`。源码 / 测试环境下该副本可能尚未生成，回退读 `plugins/skills/`
 * 源目录，保证正文单一来源、不在 TS 里复制 skill 散文。
 *
 * 缓存按文件 mtime 失效：正文不变时每轮只付一次 stat 的成本。任何失败（文件缺失 /
 * 读取异常）都返回空串并打一条 warn（同一 skill 只警告一次，避免热路径刷屏），
 * 调用方回退到“指针形态”（只提示 agent 去读 skill）。
 */

const SKILLS_DIR_CANDIDATES: readonly string[] = [
  path.join(kPluginRootDir, "skills"),
  path.resolve(kPluginRootDir, "..", "skills"),
];

let skillsDirOverride: string | undefined;

/** 仅为测试之用：强制指定 skill 根目录（传 undefined 恢复默认解析）。 */
export function _setSkillsDirOverride(dir: string | undefined): void {
  skillsDirOverride = dir;
  cache.clear();
  warned.clear();
}

/** 解析某 skill 的 SKILL.md 路径：按候选目录顺序取第一个存在的；都不存在返回 undefined。 */
export function skillFilePath(name: string): string | undefined {
  const dirs = skillsDirOverride ? [skillsDirOverride] : SKILLS_DIR_CANDIDATES;
  for (const dir of dirs) {
    const file = path.join(dir, name, "SKILL.md");
    if (fs.existsSync(file)) return file;
  }
  return undefined;
}

/** 去掉文件头部的 YAML frontmatter（`---` … `---`）；无 frontmatter 原样返回。 */
export function stripFrontmatter(md: string): string {
  const m = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/.exec(md);
  return m ? md.slice(m[0].length) : md;
}

const HEADING_RE = /^(#{1,6})\s+(.+?)\s*$/;

/**
 * 按标题文本抽取若干小节（含标题行，直到下一个同级或更高级标题为止），按传入顺序拼接。
 * 标题匹配去掉 `#` 前缀后按 trim 全等比较；找不到的标题跳过。全部找不到返回空串。
 * 用于把 skill 正文中“预注入只需要的那几节”切出来，同时保持文本单一来源。
 */
export function extractSections(md: string, headings: readonly string[]): string {
  const lines = md.split(/\r?\n/);
  const out: string[] = [];
  for (const wanted of headings) {
    const target = wanted.trim();
    let start = -1;
    let level = 0;
    for (let i = 0; i < lines.length; i++) {
      const m = HEADING_RE.exec(lines[i]);
      if (m && m[2].trim() === target) {
        start = i;
        level = m[1].length;
        break;
      }
    }
    if (start < 0) continue;
    let end = lines.length;
    for (let i = start + 1; i < lines.length; i++) {
      const m = HEADING_RE.exec(lines[i]);
      if (m && m[1].length <= level) {
        end = i;
        break;
      }
    }
    out.push(lines.slice(start, end).join("\n").trim());
  }
  return out.join("\n\n");
}

/**
 * 粗略估算 token 数：CJK 字符按 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token
 * （与 cli/src/miloco_cli/catalog.py 的估算口径一致）。只用于预算判断与日志。
 */
export function estimateTokens(text: string): number {
  let cjk = 0;
  let other = 0;
  for (const ch of text) {
    const cp = ch.codePointAt(0) ?? 0;
    if ((cp >= 0x3000 && cp <= 0x9fff) || (cp >= 0xff00 && cp <= 0xffef)) cjk++;
    else other++;
  }
  return cjk + Math.ceil(other / 4);
}

type CacheEntry = { file: string; mtimeMs: number; text: string };
const cache = new Map<string, CacheEntry>();
const warned = new Set<string>();

function warnOnce(name: string, message: string): void {
  if (warned.has(name)) return;
  warned.add(name);
  logger.warn(message);
}

/**
 * 读取 skill 正文（已去 frontmatter）；传 `sections` 时只返回这些标题下的小节。
 * 按 mtime 缓存；任何失败返回空串（并 warn 一次），绝不抛。
 */
export function loadSkillBody(
  name: string,
  opts?: { sections?: readonly string[] },
): string {
  const key = opts?.sections ? `${name}|${opts.sections.join("")}` : name;
  try {
    const file = skillFilePath(name);
    if (!file) {
      warnOnce(name, `skill ${name} 的 SKILL.md 不存在，预注入回退为指针形态`);
      return "";
    }
    const mtimeMs = fs.statSync(file).mtimeMs;
    const hit = cache.get(key);
    if (hit && hit.file === file && hit.mtimeMs === mtimeMs) return hit.text;

    const body = stripFrontmatter(fs.readFileSync(file, "utf8")).trim();
    const text = opts?.sections ? extractSections(body, opts.sections) : body;
    if (!text) {
      warnOnce(name, `skill ${name} 正文为空或未匹配到指定小节，预注入回退为指针形态`);
    } else {
      warned.delete(name);
    }
    cache.set(key, { file, mtimeMs, text });
    return text;
  } catch (err) {
    warnOnce(
      name,
      `读取 skill ${name} 失败，预注入回退为指针形态：${(err as Error).message}`,
    );
    return "";
  }
}

/** 仅为测试 / hot-reload 之用：清缓存。 */
export function _resetSkillCache(): void {
  cache.clear();
  warned.clear();
}
