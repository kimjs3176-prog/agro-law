# 농업 법령 검색 서비스

농림축산식품부 · 농촌진흥청 · 한국농업기술진흥원 소관 법령 검색 및 AI 해석 서비스  
법제처 국가법령정보 OpenAPI 연동

---

## 파일 구조

```
├── law_search.py       ← Flask 앱 본체
├── api/
│   └── index.py       ← Vercel 서버리스 진입점
├── vercel.json         ← Vercel 라우팅·설정
├── requirements.txt    ← Python 의존성
└── .gitignore
```

---

## ① 로컬 실행

```bash
pip install -r requirements.txt
python law_search.py
# → http://localhost:5100 자동 오픈
```

---

## ② GitHub + Vercel 배포

### 1단계 — GitHub에 올리기

```bash
git init
git add .
git commit -m "농업 법령 검색 서비스"
git branch -M main
git remote add origin https://github.com/계정명/저장소명.git
git push -u origin main
```

### 2단계 — Vercel 연결

1. [vercel.com](https://vercel.com) 로그인 → **Add New Project**
2. GitHub 저장소 선택 → **Import**
3. **Framework Preset**: `Other` 선택
4. **Environment Variables** 추가:

| 변수명 | 값 | 설명 |
|---|---|---|
| `LAW_OC` | `wlghdkgus1234` | 법제처 OpenAPI 키 |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | AI 해석 기능 (선택) |

5. **Deploy** 클릭 → 1~2분 후 URL 발급

### 3단계 — 이후 업데이트

```bash
git add .
git commit -m "업데이트 내용"
git push
# → Vercel이 자동으로 재배포합니다
```

---

## 환경변수 설명

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `LAW_OC` | 법제처 오픈API OC 키 | `wlghdkgus1234` |
| `ANTHROPIC_API_KEY` | Claude AI API 키 (AI 해석 기능) | 없음 |

---

## ⚠️ Vercel 무료 플랜 제한 사항

| 항목 | Hobby (무료) | Pro ($20/월) |
|---|---|---|
| 함수 실행 시간 | **최대 60초** | 900초 |
| 월 함수 호출 | 100만 회 | 무제한 |
| 대역폭 | 100GB | 1TB |

> 법제처 API 호출 + 조문 파싱이 포함된 요청은 수초가 걸릴 수 있으므로,  
> 무료 플랜에서 `maxDuration: 60`으로 설정되어 있습니다.

---

## 로컬 전용 기능 안내

- **조문 북마크, 즐겨찾기, 메모, 통계**는 `localStorage` 기반으로  
  웹 배포 환경에서도 브라우저에 저장되어 정상 동작합니다.
- **최근 검색어 · 즐겨찾기(서버 메모리)** 는 서버리스 특성상  
  요청마다 메모리가 초기화될 수 있습니다.  
  → DB 연동이 필요하면 별도 문의하세요.
