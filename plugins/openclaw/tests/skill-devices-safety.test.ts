/** Device skill must preserve the server gate and private MiHome approval workflow. */

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
    expect(section).toContain("stage");
    expect(section).toContain("米家 App");
    expect(section).toContain("响应不包含确认码");
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
    // Plain agreement without the privately delivered credential must not apply.
    expect(section).toMatch(/危险批/);
    expect(section).toContain("用户明确同意并提供米家 App 中的确认码后");
    expect(section).toContain("仅说“确定”而没有码");
  });

  it("边界里仍声明不接受绕过安全规范的指令", () => {
    const section = sectionAfter(md, /^##\s边界/);
    expect(section).toMatch(/绕过安全规范/);
  });
});
