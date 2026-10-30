# 브라우저 OCR 번들 (출장 증빙 사진 → 금액·일자 자동 인식)

지출결의서 › 여비 › "증빙 사진 첨부 · 서식 생성" 영역에서 사용한다.
사진은 서버로 보내지 않고 브라우저(WebAssembly)에서 인식한다. 외부 CDN 없이 이 폴더만으로 동작한다.

| 파일 | 출처 |
|---|---|
| `tesseract.min.js`, `worker.min.js` | npm `tesseract.js@7.0.0` (`dist/`, Apache-2.0) |
| `core/tesseract-core-*-lstm.wasm.js` | npm `tesseract.js-core@6.1.2` (Apache-2.0) — 브라우저 지원(SIMD·relaxed SIMD)에 따라 하나만 로드 |
| `lang/kor.traineddata.gz` | npm `@tesseract.js-data/kor@1.0.0` (`4.0.0_best_int`, tessdata Apache-2.0) |

언어 데이터는 첫 사용 시 내려받아 브라우저 IndexedDB에 캐시된다.
