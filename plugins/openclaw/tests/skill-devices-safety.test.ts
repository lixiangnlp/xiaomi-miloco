/**
 * miloco-devices skill 里的“危险设备必须二次确认”规则不许被静默删掉。
 *
 * 这条规则目前只活在 SKILL.md 的文字里：server 侧没有闸门，agent 是否真的先问再开锁，只能
 * 靠 evals/ 的行为用例（devices-004 / injection-001 等）在有录制时回放验证。在 server gate 落地
 * 之前，本用例守住更前面一层——规则文字本身：一旦有人改 SKILL.md 时把“门锁 / 摄像头 / 燃气阀 /
 * 烟雾报警器”这组品类或“二次确认”的指令删了、改软了，CI 立刻变红，而不是等线上某次开锁才发现。
 *
 * 与 skill-identity.test.ts 同一思路：钉 skill 正文里的不变量。这里刻意只断言关键词与结构
 * （品类四项 + 步骤 5 分流 + 关键规则条目），不钉具体措辞，给改写留余地。
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const SKILL_MD = path.resolve(here, "../../skills/miloco-devices/SKILL.md");

// 危险设备品类：四项缺一不可（steps 5 与“关键规则”两处都要有）。
const DANGEROUS_CATEGORIES = ["门锁", "摄像头", "燃气阀", "烟雾报警器"];

function sectionAfter(md: string, heading: RegExp): string {
  const lines = md.split("\n");
  const start = lines.findIndex((l) => heading.test(l));
  if (start < 0) return "";
  const level = (lines[start].match(/^#+/) ?? [""])[0].length;
  const out: string[] = [];
  for (let i = start + 1; i < lines.length; i++) {
    const m = lines[i].match(/^(#+)\s/);
    if (m && m[1].length <= level) break;
    out.push(lines[i]);
  }
  return out.join("\n");
}

describe("miloco-devices：危险设备二次确认规则仍在", () => {
  const md = readFileSync(SKILL_MD, "utf8");

  it("有独立的“安全分流”步骤，且列出全部危险品类", () => {
    const section = sectionAfter(md, /^###\s.*安全分流/);
    expect(section, "SKILL.md 缺少“步骤 · 安全分流”小节").not.toBe("");
    for (const cat of DANGEROUS_CATEGORIES) {
      expect(section, `安全分流小节漏了危险品类“${cat}”`).toContain(cat);
    }
    expect(section, "安全分流小节没有“危险批”概念").toContain("危险批");
    expect(section, "安全分流小节没有写明需要二次确认").toMatch(/二次确认/);
  });

  it("“关键规则”里保留“安全设备控制必须二次确认”条目", () => {
    const rules = sectionAfter(md, /^##\s关键规则/);
    expect(rules, "SKILL.md 缺少“关键规则”小节").not.toBe("");
    const line = rules
      .split("\n")
      .find((l) => /二次确认/.test(l) && /必须/.test(l));
    expect(line, "关键规则里没有“…必须二次确认”条目").toBeDefined();
    for (const cat of DANGEROUS_CATEGORIES) {
      expect(line, `关键规则的二次确认条目漏了“${cat}”`).toContain(cat);
    }
  });

  it("下发回合写明危险批只在用户同意后才下发", () => {
    const section = sectionAfter(md, /^###\s.*下发和回复/);
    expect(section).not.toBe("");
    // 第 2 轮只发用户同意的危险指令：这句是“先问后做”的时序保证。
    expect(section).toMatch(/危险批/);
    expect(section).toMatch(/同意|确认后/);
  });

  it("边界里仍声明不接受绕过安全规范的指令", () => {
    const section = sectionAfter(md, /^##\s边界/);
    expect(section).toMatch(/绕过安全规范/);
  });
});
