#!/usr/bin/env python3
"""ALIO 동기화 오프라인 종단 시험 — 네트워크 없이 픽스처로 sync → apply → 재실행까지 확인.

실제 manifest 를 임시 작업 폴더에 복사해 돌리므로 저장소는 바뀌지 않는다.
사용: python scripts/test_alio_sync.py   (node 필요, 실패 시 종료 코드 1)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.environ.get("NODE", "node")

ARTICLES = "제1조(목적) 이 규칙은 시험을 위한 것이다.\n\n제2조(정의) 이 규칙에서 쓰는 용어의 뜻은 다음과 같다.\n"


def run(cmd, cwd, env=None):
    p = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})},
                       capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"명령 실패: {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p


def main():
    fails = []

    def check(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(work, "regulations"))
        for f in ("api_server.py", "hwpx_base.py", "reg_chunks.py", "regulations_manifest.json"):
            shutil.copy(os.path.join(ROOT, f), work)
        shutil.copytree(os.path.join(ROOT, "scripts"), os.path.join(work, "scripts"))
        man0 = json.load(open(os.path.join(work, "regulations_manifest.json"), encoding="utf-8"))

        fx = os.path.join(tmp, "fixture")
        os.makedirs(os.path.join(fx, "files"))
        rules = [
            # 기관명 접두 + 옛 시행일 → 변경 없음
            {"idx": "101", "title": "한국농업기술진흥원 감사규정", "enfDate": "2000.01.01", "modDate": "2000.01.01"},
            # 괄호 별칭 차이 → 같은 규정(명칭 차이로 보고)
            {"idx": "102", "title": "지식재산권 관리에 관한 업무처리 규칙", "enfDate": "2023.11.20", "modDate": "2023.11.20"},
            # 소소한 개칭 → 같은 규정(유사 명칭)
            {"idx": "103", "title": "종자산업진흥센터 농업기계 임대 운영규칙", "enfDate": "2023.06.26", "modDate": "2023.06.26"},
            # 전각 문자 차이 → 같은 규정
            {"idx": "104", "title": "농업실용화기술R&D지원 관리지침", "enfDate": "2023.01.11", "modDate": "2023.01.11"},
            # 새 시행일 → 개정 반영
            {"idx": "105", "title": "직원창업규칙", "enfDate": "2026.10.01", "modDate": "2026.10.01",
             "_file": "창업.hwp"},
            # 매뉴얼은 서비스에 싣지 않음 → 신규로 가져오지 않음(제외)
            {"idx": "107", "title": "한국농업기술진흥원 시험 사업 운영 매뉴얼", "enfDate": "2026.09.01", "modDate": "2026.09.01"},
            # 처음 보는 규정 → 신규 반영
            {"idx": "106", "title": "한국농업기술진흥원 시험용 신규 지침", "enfDate": "2026.09.15", "modDate": "2026.09.15",
             "_file": "신규.hwp"},
        ]
        json.dump({"org": "한국농업기술진흥원", "rules": rules},
                  open(os.path.join(fx, "rules.json"), "w", encoding="utf-8"), ensure_ascii=False)
        for name in ("창업.hwp", "신규.hwp"):
            open(os.path.join(fx, "files", name), "wb").write(b"HWP Document File (fixture)")
            open(os.path.join(fx, "files", name + ".md"), "w", encoding="utf-8").write(
                "# 시험 규칙\n\n**" + ARTICLES.replace("\n\n", "**\n\n**", 1) + "\n| 구분 | 내용 |\n|---|---|\n| 가 | 나 |\n")

        out = os.path.join(tmp, "incoming")
        env = {"ALIO_SYNC_FIXTURE": fx}
        sync = [NODE, "scripts/alio_sync.mjs", "--out", out]

        print("1) 변경 감지")
        run(sync, work, env)
        ch = json.load(open(os.path.join(out, "changes.json"), encoding="utf-8"))
        kinds = {i["title"]: i["kind"] for i in ch["items"]}
        check(kinds == {"직원창업규칙": "revised", "시험용 신규 지침": "new"}, f"신규·개정 2건만 감지 {kinds}")
        check(ch.get("excluded") == 1, f"매뉴얼 제외 {ch.get('excluded')}건")
        ren = {r["alio_title"]: r["how"] for r in ch["renamed"]}
        check(ren.get("지식재산권 관리에 관한 업무처리 규칙") == "alias", "괄호 별칭 → 같은 규정")
        check(ren.get("종자산업진흥센터 농업기계 임대 운영규칙") == "fuzzy", "유사 명칭 → 같은 규정")
        check("농업실용화기술R＆D지원 관리지침" in ch["tracking"], "전각 R＆D → 같은 규정")
        check(len(ch["missing"]) == len(man0) - 5, f"ALIO에 없는 규정 수 {len(ch['missing'])}")

        print("2) 반영")
        run([sys.executable, "scripts/alio_apply.py", os.path.join(out, "changes.json")], work)
        res = json.load(open(os.path.join(out, "apply_result.json"), encoding="utf-8"))
        arts = {a["title"]: a["articles"] for a in res["applied"]}
        check(not res["failed"], f"변환 실패 없음 {res['failed']}")
        check(arts == {"직원창업규칙": 2, "시험용 신규 지침": 2}, f"조문 인식 {arts}")
        man = json.load(open(os.path.join(work, "regulations_manifest.json"), encoding="utf-8"))
        e = next(m for m in man if m["title"] == "직원창업규칙")
        check(e["revision"] == "2026년도 10월 개정" and e["history"][0].get("src"), "개정 라벨·이력(src 포함)")
        check(os.path.exists(os.path.join(work, "regulations", e["slug"], "original.hwp")), "원본 보관")
        report = open(os.path.join(out, "report.md"), encoding="utf-8").read()
        check("### 변환 결과" in report and "명칭이 조금 달라" in report, "리포트에 변환 결과·명칭 차이 표시")

        print("3) 재실행 → 변경 없음")
        out2 = os.path.join(tmp, "incoming2")
        run([NODE, "scripts/alio_sync.mjs", "--out", out2], work, env)
        ch2 = json.load(open(os.path.join(out2, "changes.json"), encoding="utf-8"))
        check(not ch2["items"], f"재실행 변경 0건 ({len(ch2['items'])})")

    if fails:
        sys.exit(f"\n실패 {len(fails)}건")
    print("\n모두 통과")


if __name__ == "__main__":
    main()
