# 데이터 형식

## 1) 내규 목록 — `regulations_manifest.json`
규정 메타의 **배열**. 대시보드·목록·분류 필터의 원천.

```json
[
  {
    "title": "인사규정",                       // 규정 정식 명칭(검색·표시 기준)
    "revision": "2025년 1월 제정",              // 현행 개정 라벨(표시용)
    "category": "규정",                         // 정관|규정|규칙|세칙|예규|매뉴얼|기타
    "slug": "인사규정",                          // 폴더/URL용(공백→언더스코어)
    "src": "인사규정(2025년 1월 제정).hwpx",     // 원본 파일명(참고용)
    "html": "/regulations/인사규정/index.html",  // 본문 HTML 경로
    "pdf": ""                                    // 원본 PDF 경로(선택, REG_PDF_BASE_URL 접두)
  }
]
```
- `category` 는 프론트 `RULE_TREE_ORDER`(정관·규정·규칙·세칙·예규·매뉴얼·기타) 버킷과
  맞아야 대시보드 분류 그리드/필터에 바르게 들어갑니다. 벗어나면 '기타'로 분류됩니다.
- 개정이력을 표시하려면 항목에 `history: [{revision, replaced_at|effective_date}]`,
  `effective_date` 등을 추가할 수 있습니다(있으면 카드 개정 메타에 사용).

## 2) 규정 본문 — `regulations/<slug>/index.html`
- HWP/HWPX를 HTML로 변환한 문서. 백엔드가 **텍스트를 추출**해 조문 단위로 검색·표시합니다.
- 조문은 `제N조(제목) 내용` 형태면 파서가 잘 인식합니다. `별표/별지/부칙` 은 검색 밀도
  계산에서 제외됩니다.
- 같은 폴더의 `styles.css` 는 열람용 서식(검색 기능과 무관, 없어도 됨).
- 변환 팁: 한컴오피스 "다른 이름으로 저장 → 웹 페이지(HTML)" 또는 hwp5html 등.

## 3) 의미 검색 벡터(선택) — `regulations_vectors.{bin,json}`
- `scripts/build_embeddings.py` 로 `regulations/` 전체를 임베딩해 생성.
- `.json` = 청크 메타(규정명·조문번호·미리보기 등), `.bin` = float32 벡터 매트릭스.
- 파일이 있으면 내규 검색에 "의미" 결과가 함께 표시되고, 없으면 키워드 검색만 동작
  (`semantic_available=false`). 질의 임베딩에는 Gemini 키를 사용합니다.

## 4) 내규 검색 응답 계약 — `GET /api/internal/search?query=…`
프론트가 기대하는 형태:
```jsonc
{
  "success": true,
  "structured": [                 // 규정 단위 결과(권장)
    { "title": "인사규정", "category": "규정", "org": "…", "content": "규정 전문 텍스트" }
  ],
  "text": "…",                    // structured 대신 텍스트 블록으로 줄 수도 있음
  "name_matches": [               // 규정명에 질의가 포함된 규정
    { "title": "인사규정", "category": "규정", "revision": "…", "exact": false }
  ],
  "semantic": [                   // 의미 검색 조문(선택)
    { "title": "…", "no": 3, "art_title": "…", "preview": "…", "score": 0.82 }
  ],
  "semantic_available": true
}
```
백엔드는 로컬 `regulations/` 와(또는) `SAGYU_MCP_URL` MCP 서버에서 이 형태로 조립합니다.
MCP 없이 로컬 파일만으로도 동작하도록 되어 있습니다.
