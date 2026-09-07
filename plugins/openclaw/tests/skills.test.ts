import fs, { mkdirSync, mkdtempSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  _resetSkillCache,
  _setSkillsDirOverride,
  estimateTokens,
  extractSections,
  loadSkillBody,
  skillFilePath,
  stripFrontmatter,
} from "../src/services/skills.js";

const SAMPLE = `---
name: demo
metadata:
  version: "1.0"
---

# demo

导语。

## 甲

甲的正文。

### 甲一

甲一正文，含代码：

\`\`\`bash
echo hi
\`\`\`

## 乙

乙的正文。

## 丙

丙的正文。
`;

function writeSkill(root: string, name: string, content: string): string {
  const dir = path.join(root, name);
  mkdirSync(dir, { recursive: true });
  const file = path.join(dir, "SKILL.md");
  writeFileSync(file, content, "utf8");
  return file;
}

describe("stripFrontmatter", () => {
  it("剥掉首部 YAML frontmatter，正文从 H1 开始", () => {
    const body = stripFrontmatter(SAMPLE);
    expect(body.startsWith("\n# demo")).toBe(true);
    expect(body).not.toContain("name: demo");
  });

  it("无 frontmatter 原样返回", () => {
    expect(stripFrontmatter("# x\n正文")).toBe("# x\n正文");
  });

  it("正文中间的 --- 分隔线不会被当成 frontmatter 结束符误删", () => {
    const md = "---\na: 1\n---\n正文\n\n---\n\n后半";
    expect(stripFrontmatter(md)).toBe("正文\n\n---\n\n后半");
  });
});

describe("extractSections", () => {
  const body = stripFrontmatter(SAMPLE);

  it("按标题抽取小节，含子标题与代码块，止于下一同级标题", () => {
    const out = extractSections(body, ["甲"]);
    expect(out).toContain("## 甲");
    expect(out).toContain("### 甲一");
    expect(out).toContain("echo hi");
    expect(out).not.toContain("## 乙");
  });

  it("多个标题按传入顺序拼接；子标题可单独抽取", () => {
    const out = extractSections(body, ["丙", "甲一"]);
    expect(out.indexOf("## 丙")).toBeLessThan(out.indexOf("### 甲一"));
    expect(out).not.toContain("甲的正文");
  });

  it("缺少任一必需标题时返回空串", () => {
    expect(extractSections(body, ["不存在", "乙"])).toBe("");
    expect(extractSections(body, ["乙", "不存在"])).toBe("");
    expect(extractSections(body, ["不存在"])).toBe("");
  });

  it("标题匹配对 devices skill 里含反引号的标题同样成立", () => {
    const md = "## A\n\n### 智能音箱：`play-text` vs `execute-text-directive`\n\n内容\n\n### 其他\n\nx";
    const out = extractSections(md, ["智能音箱：`play-text` vs `execute-text-directive`"]);
    expect(out).toContain("内容");
    expect(out).not.toContain("其他");
  });
});

describe("estimateTokens", () => {
  it("CJK 按 1 字 1 token，其余按 4 字符 1 token", () => {
    expect(estimateTokens("你好世界")).toBe(4);
    expect(estimateTokens("abcdefgh")).toBe(2);
    expect(estimateTokens("你好 abc")).toBe(2 + 1);
    expect(estimateTokens("")).toBe(0);
  });
});

describe("loadSkillBody", () => {
  let root: string;

  beforeEach(() => {
    root = mkdtempSync(path.join(tmpdir(), "miloco-skills-"));
    _setSkillsDirOverride(root);
  });

  afterEach(() => {
    _setSkillsDirOverride(undefined);
    _resetSkillCache();
    rmSync(root, { recursive: true, force: true });
  });

  it("读取正文并剥 frontmatter；sections 只返回指定小节", () => {
    writeSkill(root, "demo", SAMPLE);
    const full = loadSkillBody("demo");
    expect(full.startsWith("# demo")).toBe(true);
    expect(full).toContain("丙的正文");
    const part = loadSkillBody("demo", { sections: ["乙"] });
    expect(part).toBe("## 乙\n\n乙的正文。");
  });

  it("文件缺失 → 空串且不抛", () => {
    expect(() => loadSkillBody("nope")).not.toThrow();
    expect(loadSkillBody("nope")).toBe("");
    expect(skillFilePath("nope")).toBeUndefined();
  });

  it("按 mtime 缓存：mtime 不变复用旧正文，mtime 变化重新读取", () => {
    const file = writeSkill(root, "demo", SAMPLE);
    // 先把 mtime 钉到整毫秒：statSync 的 mtimeMs 可能带亚毫秒精度，utimesSync 只到毫秒，
    // 不钉住会让“拨回原值”对不上。
    const t0 = new Date(Math.floor(Date.now() / 1000) * 1000 - 60_000);
    utimesSync(file, t0, t0);
    expect(loadSkillBody("demo")).toContain("丙的正文");
    expect(statSync(file).mtime.getTime()).toBe(t0.getTime());

    // 改内容但把 mtime 拨回原值 → 命中缓存，仍是旧文
    writeFileSync(file, "# demo\n\n新版正文", "utf8");
    utimesSync(file, t0, t0);
    expect(loadSkillBody("demo")).toContain("丙的正文");

    // mtime 前进 → 缓存失效，读到新文
    const t1 = new Date(t0.getTime() + 5_000);
    utimesSync(file, t1, t1);
    expect(loadSkillBody("demo")).toBe("# demo\n\n新版正文");
  });

  it("路径在 stat 后被替换时仍读取已打开的文件，下次调用读取新文件", () => {
    const file = writeSkill(root, "demo", "# 原始正文");
    const replacement = path.join(root, "replacement.md");
    writeFileSync(replacement, "# 替换正文");
    const timestamp = new Date(Math.floor(Date.now() / 1000) * 1000);
    utimesSync(file, timestamp, timestamp);
    utimesSync(replacement, timestamp, timestamp);
    const fstat = fs.fstatSync.bind(fs);
    const spy = vi.spyOn(fs, "fstatSync").mockImplementationOnce((fd) => {
      const result = fstat(fd);
      fs.renameSync(replacement, file);
      return result;
    });
    try {
      expect(loadSkillBody("demo")).toBe("# 原始正文");
      // Identical mtime on a different inode must not reuse the old cache entry.
      expect(loadSkillBody("demo")).toBe("# 替换正文");
    } finally {
      spy.mockRestore();
    }
  });

  it("默认解析：能从仓库 plugins/skills/ 源目录读到 miloco-notify（未 sync 也可用）", () => {
    _setSkillsDirOverride(undefined);
    const body = loadSkillBody("miloco-notify");
    expect(body.startsWith("# miloco-notify")).toBe(true);
    expect(body).not.toContain("name: miloco-notify");
  });
});
