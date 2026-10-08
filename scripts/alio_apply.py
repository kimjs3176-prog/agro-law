#!/usr/bin/env python3
"""ALIO 동기화 결과 반영 — scripts/alio_sync.mjs 가 만든 changes.json 을 읽어
regulations/<규정>/ (index.html·text.txt·original.*) 와 regulations_manifest.json 을 갱신한다.

변환은 업로드 화면(/upload)과 같은 api_server._convert_upload 를 쓴다.
 · HWPX·DOCX → 그대로 변환(본문·서식)
 · HWP·PDF·ZIP 등 변환기가 읽지 못하는 형식 → alio-mcp(kordoc)가 추출한 본문(.md)으로 변환,
   원본 파일은 original.* 로 함께 보관(원본 내려받기)
이전 개정본은 manifest history 에 쌓인다(업로드와 같은 규칙).

사용: python scripts/alio_apply.py alio_incoming/changes.json [--dry-run]
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import api_server as A  # noqa: E402

DIRECT_EXT = {".hwpx", ".docx", ".html", ".htm"}


def md_to_text(md: str) -> str:
    """kordoc 마크다운 → 변환기용 평문. '**제1조(목적)**'·'### 제1장' 같은 표식이 남으면
    조문 머리를 인식하지 못하므로 걷어낸다. 표는 셀을 ' | '로 이은 한 줄로 둔다."""
    out = []
    for ln in (md or "").splitlines():
        t = ln.rstrip()
        if re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", t):
            continue                                   # 표 구분선 |---|---|
        t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t)        # 제목 표식
        t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)     # 굵게
        t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)  # 기울임
        if t.strip().startswith("|") and t.strip().endswith("|"):
            t = " | ".join(c.strip() for c in t.strip().strip("|").split("|"))
        t = t.replace("\\", "")
        out.append(t)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def revision_label(enf: str, kind: str) -> str:
    """'2026-08-05' → '2026년도 8월 개정'(기존 manifest 표기와 맞춤)."""
    m = re.match(r"(\d{4})-(\d{2})", enf or "")
    if not m:
        return "ALIO 반영"
    return f"{m.group(1)}년도 {int(m.group(2))}월 {'제정' if kind == 'new' else '개정'}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    changes_path = os.path.abspath(sys.argv[1])
    dry = "--dry-run" in sys.argv
    base_dir = os.path.dirname(changes_path)
    ch = json.load(open(changes_path, encoding="utf-8"))
    man = json.load(open(A.REG_MANIFEST_PATH, encoding="utf-8"))
    now = A._now_kst()
    applied, failed = [], []

    for it in ch.get("items", []):
        title = it["title"].strip()
        src_path = os.path.join(base_dir, it["file"])
        raw = open(src_path, "rb").read()
        ext = os.path.splitext(it.get("file_name") or src_path)[1].lower() or ".bin"
        prev = next((m for m in man if A._norm_key(m.get("title", "")) == A._norm_key(title)), None)
        revision = revision_label(it.get("alio_enf", ""), it["kind"])
        category = (prev or {}).get("category") or A._guess_reg_category(title)
        meta = {"category": category, "revision": revision,
                "effective_date": it.get("alio_enf", ""),
                "department": (prev or {}).get("department", ""),
                "note": f"ALIO 공개본 자동 반영(시행 {it.get('alio_enf', '')})",
                "uploader": "ALIO 자동 동기화", "uploaded_at": now}
        try:
            if ext in DIRECT_EXT:
                conv = A._convert_upload(it.get("file_name") or f"{title}{ext}", raw, title, meta)
            elif ext == ".pdf" and not it.get("text_md"):
                # 스캔 PDF 등 본문 추출 불가 → 업로드와 같이 '원본 PDF 열기' 화면으로 등록
                conv = A._convert_upload(it.get("file_name") or f"{title}.pdf", raw, title, meta)
            elif it.get("text_md"):
                md = md_to_text(open(os.path.join(base_dir, it["text_md"]), encoding="utf-8").read())
                conv = A._convert_upload(f"{title}.md", md.encode("utf-8"), title, meta)
                conv["warning"] = ""
            else:
                raise ValueError(it.get("warning") or "본문을 추출하지 못했습니다")
        except Exception as e:
            failed.append({"title": title, "error": str(e)})
            print(f"⚠ {title}: 변환 실패 — {e}")
            continue

        slug = (prev or {}).get("slug") or A._reg_slug(title)
        entry = dict(prev or {})
        entry.pop("history", None)
        entry.update({
            "title": title, "revision": revision, "category": category, "slug": slug,
            "src": it.get("file_name") or os.path.basename(src_path),
            "html": f"/regulations/{slug}/index.html",
            "pdf": entry.get("pdf") or f"pdf/{slug}.pdf",
            "effective_date": it.get("alio_enf", ""),
            "note": meta["note"], "uploader": meta["uploader"], "uploaded_at": now,
            "original": f"/regulations/{slug}/original{ext}",
            "searchable": bool(conv.get("text")),
            "source": "ALIO",
            "alio_idx": it.get("alio_idx", ""), "alio_enf": it.get("alio_enf", ""),
            "alio_mod": it.get("alio_mod", ""), "alio_title": it.get("alio_title", ""),
        })
        man, replaced = A._merge_manifest(man, entry)
        applied.append({"title": title, "kind": it["kind"], "revision": revision,
                        "searchable": bool(conv.get("text"))})
        print(f"{'✏️ 개정' if replaced else '🆕 신규'} {title} → {revision}"
              + ("" if conv.get("text") else " (본문 검색 불가)"))
        if dry:
            continue
        d = os.path.join(A.REG_DIR, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(conv["view_html"])
        tp = os.path.join(d, "text.txt")
        if conv.get("text"):
            with open(tp, "w", encoding="utf-8") as f:
                f.write(conv["text"])
        elif os.path.exists(tp):
            os.remove(tp)                     # 옛 본문이 최신 개정으로 오인되지 않게
        for nm in os.listdir(d):              # 확장자가 바뀐 옛 원본 정리
            if nm.startswith("original.") and nm != f"original{ext}":
                os.remove(os.path.join(d, nm))
        with open(os.path.join(d, f"original{ext}"), "wb") as f:
            f.write(raw)

    # 변경 없는 규정에도 ALIO 추적값(idx·시행일)을 기록 → 다음 실행부터 정확히 비교
    tracked = 0
    for t, tr in (ch.get("tracking") or {}).items():
        for m in man:
            if A._norm_key(m.get("title", "")) == A._norm_key(t):
                # 수정일(alio_mod)만 바뀐 경우는 PR 소음이 되므로 idx·시행일·명칭 변화만 기록
                if any(m.get(k) != tr.get(k) for k in ("alio_idx", "alio_enf", "alio_title")):
                    if not any(a["title"] == m.get("title") for a in applied):
                        m.update(tr)
                        tracked += 1
                break

    if not dry and (applied or tracked):
        A._save_reg_manifest(man)
    summary = {"applied": applied, "failed": failed, "tracking_updated": tracked}
    with open(os.path.join(base_dir, "apply_result.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"반영 {len(applied)}건 · 실패 {len(failed)}건 · 추적값 갱신 {tracked}건" + (" (dry-run)" if dry else ""))


if __name__ == "__main__":
    main()
