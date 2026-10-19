#!/usr/bin/env node
// ALIO 내규 변경 감지 · 현행본 수집
// ─────────────────────────────────────────────────────────────────────────────
// alio-mcp(https://github.com/chromehearts79/alio-mcp)의 ALIO 클라이언트로 우리 기관이
// ALIO(공공기관 경영정보 공개시스템)에 공개한 내부규정 목록을 받아 regulations_manifest.json
// 과 비교하고, 새로 생기거나 개정된 규정의 현행본 파일을 내려받아 본문을 추출한다.
// 반영(변환·manifest 갱신)은 scripts/alio_apply.py 가 맡는다.
//
// 사용:
//   node scripts/alio_sync.mjs --alio <alio-mcp 경로> [--apba C0422] [--out alio_incoming]
//                              [--manifest regulations_manifest.json] [--max 30]
// 결과: <out>/changes.json, <out>/report.md, <out>/files/* (원본), <out>/files/*.md (추출 본문)
//
// 오프라인 시험: ALIO_SYNC_FIXTURE=<dir> 이면 <dir>/rules.json·<dir>/files/* 를 ALIO 대신 쓴다.
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((a, v, i, arr) => {
    if (v.startsWith("--")) a.push([v.slice(2), arr[i + 1] && !arr[i + 1].startsWith("--") ? arr[i + 1] : "1"]);
    return a;
  }, [])
);
const ALIO_DIR = path.resolve(args.alio || process.env.ALIO_MCP_DIR || "alio-mcp");
const APBA = args.apba || process.env.ALIO_APBA_ID || "C0422";          // 한국농업기술진흥원
const OUT = path.resolve(args.out || "alio_incoming");
const MANIFEST = path.resolve(args.manifest || "regulations_manifest.json");
const MAX = Number(args.max || 30);                                      // 한 번에 받을 최대 건수
const ORG_PREFIX = /^\s*(?:\(재\)|재단법인)?\s*한국농업기술진흥원\s*/;   // 규정명 앞 기관명
// 서비스에 싣지 않는 문서(예: 매뉴얼) — ALIO 에 있어도 신규로 가져오지 않는다. ALIO_EXCLUDE 로 바꿀 수 있음(정규식)
const EXCLUDE_RE = new RegExp(process.env.ALIO_EXCLUDE || "(매뉴얼|메뉴얼)");

// ── ALIO 클라이언트(alio-mcp) 또는 시험용 픽스처 ─────────────────────────────
async function loadClient() {
  const fx = process.env.ALIO_SYNC_FIXTURE;
  if (fx) {
    const rules = JSON.parse(await fs.readFile(path.join(fx, "rules.json"), "utf8"));
    return {
      org: { apbaId: APBA, name: rules.org || "한국농업기술진흥원", apbaType: "" },
      listRules: async () => rules.rules,
      fetchDocument: async (rule, dir) => {
        const fileName = rule._file;
        const buf = await fs.readFile(path.join(fx, "files", fileName));
        await fs.mkdir(dir, { recursive: true });
        const dest = path.join(dir, `${rule.idx}${path.extname(fileName)}`);
        await fs.writeFile(dest, buf);
        return { path: dest, fileName, fileNo: rule._fileNo || "0", buf };
      },
      extract: async (buf, fileName) => {
        // 픽스처는 같은 이름의 .md 를 추출 결과로 쓴다(없으면 빈 본문)
        try { return { markdown: await fs.readFile(path.join(fx, "files", fileName + ".md"), "utf8") }; }
        catch { return { markdown: "" }; }
      },
      baseTitle: (t) => (t || "").replace(/[(（[][^)）\]]*\d{4}[^)）\]]*[)）\]]/g, "").replace(/\s+/g, ""),
    };
  }
  const client = await import(pathToFileURL(path.join(ALIO_DIR, "src", "alio-client.js")).href);
  const ruleText = await import(pathToFileURL(path.join(ALIO_DIR, "src", "rule-text.js")).href);
  const orgs = await client.listOrgs();
  const org = orgs.find((o) => o.apbaId === APBA);
  if (!org) throw new Error(`ALIO 기관 목록에 ${APBA} 가 없습니다`);
  return {
    org,
    listRules: async () => client.markSuperseded(await client.searchRules(org)).filter((r) => !r.superseded),
    fetchDocument: async (rule, dir) => {
      const files = await client.getRuleFiles(rule);
      const pick = client.pickLatestFile(files);
      if (!pick) return null;
      const { buf } = await client.fetchRuleFile(pick.fileNo);
      await fs.mkdir(dir, { recursive: true });
      const ext = (pick.fileName.match(/\.\w+$/) || [".bin"])[0].toLowerCase();
      const dest = path.join(dir, `${rule.idx}${ext}`);
      await fs.writeFile(dest, buf);
      return { path: dest, fileName: pick.fileName, fileNo: pick.fileNo, buf };
    },
    extract: (buf, fileName) => ruleText.extractDocument(buf, fileName),
    baseTitle: client.baseTitle,
  };
}

// ── 비교 도우미 ─────────────────────────────────────────────────────────────
const ymd = (s) => {                                   // "2026.08.05" / "2026-08-05" → "2026-08-05"
  const m = String(s || "").match(/(\d{4})\D?(\d{1,2})\D?(\d{1,2})/);
  return m ? `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}` : "";
};
const ym = (s) => ymd(s).slice(0, 7);
// manifest 항목의 현행 개정 시점(YYYY-MM): ALIO 추적값 → 시행일 → "2023년도 7월 개정" 표기 → 원본 파일명
function entryMonth(e) {
  if (e.alio_enf) return ym(e.alio_enf);
  if (e.effective_date) return ym(e.effective_date);
  for (const s of [e.revision, e.src]) {
    const m = String(s || "").match(/((?:19|20)\d{2})\s*년도?\s*(\d{1,2})\s*월/);
    if (m) return `${m[1]}-${m[2].padStart(2, "0")}`;
  }
  return "";
}

// 규정명 비교 키. ALIO 와 우리 목록은 같은 규정이라도 표기가 조금씩 다르다
//  · 전각 문자: "R＆D" ↔ "R&D"                       → NFKC 정규화
//  · 기관명 접두: "한국농업기술진흥원 감사규정"         → ORG_PREFIX 제거
//  · 괄호 별칭: "지식재산권 … 규칙(구 직무발명 보상규칙)" → loose 키에서 괄호 전체 제거
//  · 소소한 개칭: "농업기계 임대 규칙" ↔ "농업기계 임대 운영규칙" → 같은 종류(규칙·지침…)·같은 머리글자 안에서 유사도 비교
const nfkc = (t) => String(t || "").normalize("NFKC");
const stripOrg = (t) => nfkc(t).replace(ORG_PREFIX, "");
const looseKey = (t) => stripOrg(t).replace(/[(\[][^)\]]*[)\]]/g, "").replace(/[\s·ㆍ.,_\-]/g, "").toLowerCase();
const KIND_RE = /(규정|규칙|지침|요령|세칙|기준|정관|편람|매뉴얼|요강|방침|지시)$/;
const kindOf = (t) => (looseKey(t).match(KIND_RE) || [])[1] || "";
function dice(a, b) {                                   // 글자 2-gram Dice 계수(0~1)
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const grams = (s) => { const m = new Map(); for (let i = 0; i < s.length - 1; i++) { const g = s.slice(i, i + 2); m.set(g, (m.get(g) || 0) + 1); } return m; };
  const A = grams(a), B = grams(b);
  let hit = 0;
  for (const [g, n] of A) hit += Math.min(n, B.get(g) || 0);
  return (2 * hit) / (a.length - 1 + b.length - 1);
}
export const FUZZY_MIN = 0.85;                          // 이보다 낮으면 다른 규정으로 본다

// ALIO 규정 ↔ manifest 항목 1:1 대응. 단계: ALIO 번호 → 정확한 이름 → 괄호 무시 이름 → 유사 이름
export function matchRules(rules, manifest, baseTitle) {
  const exactKey = (t) => baseTitle(stripOrg(t)).replace(/\s+/g, "").toLowerCase();
  const used = new Set(), out = new Map();              // out: rule → { entry, how }
  const take = (r, e, how) => { out.set(r, { entry: e, how }); used.add(e); };
  const free = (pred) => manifest.filter((e) => !used.has(e) && pred(e));
  const unique = (arr) => (arr.length === 1 ? arr[0] : null);

  for (const r of rules) {
    const e = unique(free((e) => e.alio_idx && String(e.alio_idx) === String(r.idx)));
    if (e) take(r, e, "idx");
  }
  for (const [how, key] of [["title", exactKey], ["alias", looseKey]]) {
    for (const r of rules) {
      if (out.has(r)) continue;
      const k = key(r.title);
      const e = k && unique(free((e) => key(e.title) === k));
      // 반대쪽에서도 유일해야 한다(ALIO 쪽 같은 이름 둘이 한 항목을 다투면 보류)
      if (e && rules.filter((x) => !out.has(x) && key(x.title) === k).length === 1) take(r, e, how);
    }
  }
  for (const r of rules) {
    if (out.has(r)) continue;
    const k = looseKey(r.title), kind = kindOf(r.title);
    if (!kind) continue;
    // 짧은 이름은 한두 글자 차이가 곧 다른 규정("감사규정 시행세칙"↔"인사규정 시행세칙")이므로
    // 8자 이상이고 앞 3글자가 같은 경우에만 유사도를 본다
    if (k.length < 8) continue;
    const cands = free((e) => kindOf(e.title) === kind && looseKey(e.title).slice(0, 3) === k.slice(0, 3))
      .map((e) => ({ e, s: dice(k, looseKey(e.title)) }))
      .sort((a, b) => b.s - a.s);
    if (cands[0] && cands[0].s >= FUZZY_MIN && (!cands[1] || cands[0].s - cands[1].s >= 0.05)) take(r, cands[0].e, "fuzzy");
  }
  return out;
}

async function main() {
  const manifest = JSON.parse(await fs.readFile(MANIFEST, "utf8"));
  const cli = await loadClient();

  const allRules = await cli.listRules();
  const rules = allRules.filter((r) => !EXCLUDE_RE.test(String(r.title || "")));
  const excluded = allRules.length - rules.length;
  console.error(`[alio-sync] ${cli.org.name}(${APBA}) ALIO 공개 규정 ${allRules.length}건` + (excluded ? ` (제외 ${excluded}건)` : ""));
  const matched = matchRules(rules, manifest, cli.baseTitle);

  const added = [], revised = [], same = [], renamed = [], tracking = {};
  for (const r of rules) {
    const enf = ymd(r.enfDate);
    const hit = matched.get(r);
    if (!hit) { added.push({ rule: r, enf }); continue; }
    const e = hit.entry;
    if (hit.how === "alias" || hit.how === "fuzzy") renamed.push({ alio_title: r.title, title: e.title, how: hit.how });
    tracking[e.title] = { alio_idx: String(r.idx), alio_enf: enf, alio_mod: ymd(r.modDate), alio_title: r.title };
    const cur = entryMonth(e);
    // ALIO 시행월이 우리 현행본보다 늦으면 개정, 같은 idx·시행일이면 그대로
    if (e.alio_idx && String(e.alio_idx) === String(r.idx) && e.alio_enf === enf) { same.push(e.title); continue; }
    if (enf && cur && enf.slice(0, 7) > cur) revised.push({ rule: r, enf, entry: e, cur });
    else same.push(e.title);
  }
  // ALIO 공개 목록에서 찾지 못한 우리 규정(폐지·비공개·명칭 변경 확인용 — 자동으로 지우지 않는다)
  const matchedEntries = new Set([...matched.values()].map((v) => v.entry));
  const missing = manifest.filter((e) => !matchedEntries.has(e)).map((e) => e.title);

  const todo = [...revised, ...added].slice(0, MAX);
  const skipped = revised.length + added.length - todo.length;
  const items = [], errors = [];
  const filesDir = path.join(OUT, "files");
  for (const t of todo) {
    const r = t.rule;
    try {
      const doc = await cli.fetchDocument(r, filesDir);
      if (!doc) { errors.push({ title: r.title, error: "ALIO에 첨부 파일이 없음" }); continue; }
      let mdPath = "", warn = "";
      try {
        const { markdown } = await cli.extract(doc.buf, doc.fileName);
        if (markdown && markdown.trim()) {
          mdPath = doc.path + ".md";
          await fs.writeFile(mdPath, markdown);
        } else warn = "본문을 추출하지 못함(스캔 PDF 등)";
      } catch (e) { warn = `본문 추출 실패: ${e.message || e}`; }
      items.push({
        kind: t.entry ? "revised" : "new",
        title: t.entry ? t.entry.title : r.title.replace(ORG_PREFIX, "").trim() || r.title,
        alio_title: r.title, alio_idx: String(r.idx), alio_enf: t.enf, alio_mod: ymd(r.modDate),
        alio_category: r.category || "", prev_month: t.cur || "",
        file: path.relative(OUT, doc.path), file_name: doc.fileName, file_no: String(doc.fileNo),
        text_md: mdPath ? path.relative(OUT, mdPath) : "", warning: warn,
      });
      console.error(`  ${t.entry ? "✏️ 개정" : "🆕 신규"} ${r.title} (시행 ${t.enf})${warn ? " ⚠ " + warn : ""}`);
    } catch (e) {
      errors.push({ title: r.title, error: String(e.message || e) });
      console.error(`  ⚠ ${r.title}: ${e.message || e}`);
    }
  }

  const today = new Date().toISOString().slice(0, 10);
  const changes = {
    checked_at: today, org: cli.org.name, apba_id: APBA, alio_total: rules.length,
    items, errors, skipped, unchanged: same.length, tracking, renamed, missing, excluded,
  };
  await fs.mkdir(OUT, { recursive: true });
  await fs.writeFile(path.join(OUT, "changes.json"), JSON.stringify(changes, null, 2));

  const L = [];
  L.push(`## ALIO 내규 동기화 리포트 (${today})`, "");
  L.push(`- 기관: **${cli.org.name}** (\`${APBA}\`) · ALIO 공개 규정 ${rules.length}건 · 변경 없음 ${same.length}건`
    + (missing.length ? ` · ALIO에 없는 서비스 규정 ${missing.length}건` : "")
    + (excluded ? ` · 제외(매뉴얼 등) ${excluded}건` : ""));
  L.push(`- 🆕 신규 ${items.filter((i) => i.kind === "new").length}건 · ✏️ 개정 ${items.filter((i) => i.kind === "revised").length}건`
    + (errors.length ? ` · ⚠ 실패 ${errors.length}건` : "") + (skipped ? ` · 다음 실행으로 미룸 ${skipped}건` : ""), "");
  if (items.length) {
    L.push("| 구분 | 규정 | ALIO 시행일 | 이전 현행본 | 원본 파일 | 비고 |", "|---|---|---|---|---|---|");
    for (const i of items)
      L.push(`| ${i.kind === "new" ? "🆕 신규" : "✏️ 개정"} | ${i.title} | ${i.alio_enf} | ${i.prev_month || "-"} | ${i.file_name} | ${i.warning || ""} |`);
    L.push("");
  }
  if (errors.length) { L.push("**받지 못한 규정**", ""); errors.forEach((e) => L.push(`- ${e.title}: ${e.error}`)); L.push(""); }
  if (renamed.length) {
    L.push(`<details><summary>명칭이 조금 달라 같은 규정으로 대응한 ${renamed.length}건</summary>`, "");
    L.push("| ALIO 규정명 | 서비스 규정명 | 대응 방식 |", "|---|---|---|");
    renamed.forEach((x) => L.push(`| ${x.alio_title} | ${x.title} | ${x.how === "alias" ? "괄호·표기 차이" : "유사 명칭"} |`));
    L.push("", "</details>", "");
  }
  if (missing.length) {
    L.push(`<details><summary>ALIO 공개 목록에서 찾지 못한 서비스 규정 ${missing.length}건 (폐지·비공개·명칭 변경 여부 확인)</summary>`, "");
    missing.forEach((t) => L.push(`- ${t}`));
    L.push("", "자동으로 삭제하지 않습니다. 폐지된 규정이면 업로드 화면에서 정리하세요.", "</details>", "");
  }
  L.push("> 출처: ALIO(alio.go.kr) 공공기관 내부규정 공개 — 수집 도구 alio-mcp. 병합 전에 각 규정의 본문 변환 결과를 확인하세요.");
  await fs.writeFile(path.join(OUT, "report.md"), L.join("\n") + "\n");
  console.log(`changes=${items.length}`);
}

// 테스트에서 matchRules 만 가져다 쓸 수 있도록 직접 실행할 때만 동작
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((e) => { console.error(`[alio-sync] 실패: ${e.stack || e}`); process.exit(1); });
}
