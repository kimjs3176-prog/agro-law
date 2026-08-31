# org-legal-search — 기관용 내규·국가법령 종합 검색 (범용 템플릿)

한 기관의 **내규(사규)** 와 **국가법령**을 한 화면에서 검색·상호연결해 열람하는
웹서비스의 범용 템플릿과 세팅 가이드입니다. 특정 기관 종속 데이터·브랜딩을 제거하고
검증된 **구조만** 남겼습니다.

- 스킬 설명·세팅 순서: [`SKILL.md`](SKILL.md)
- 코드 템플릿: [`templates/`](templates/)
- 상세 문서: [`reference/`](reference/)
  - [architecture.md](reference/architecture.md) · [data-format.md](reference/data-format.md)
    · [customization.md](reference/customization.md) · [deployment.md](reference/deployment.md)

## 30초 요약
```bash
cp -r templates/  ../my-org-legal-search && cd ../my-org-legal-search
grep -rn "\[기관 설정\]" .        # 바꿀 지점 찾기
cp .env.example .env             # LAW_OC(법제처 키) 등 채우기
pip install -r requirements.txt && python run_local.py
```
자세한 내용은 `SKILL.md` 를 참고하세요. 실제 키(`.env`)는 커밋하지 마세요.
