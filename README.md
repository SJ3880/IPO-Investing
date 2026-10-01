# IPO 동향 분석

2020년 이후 코스피·코스닥 IPO(직상장 + 스팩합병상장)를 단계별 탭으로 보여주는 사이트입니다.
한눈에(단계별 건수·다가오는 일정·월별 추이·분기별 시장 온도) / 심사중 / 심사승인 / 수요예측·청약 / 상장완료 / 철회·미승인.
GitHub Actions가 평일 16:40(KST)마다 자동으로 데이터를 새로 받고 사이트를 다시 올립니다.

## API 키 등록 (Settings → Secrets and variables → Actions → New repository secret)
| 이름 | 용도 | 발급 |
|---|---|---|
| `DART_API_KEY` | 유통가능물량, 사업 내용, 주관사·공모금액·구주매출, 최근 공시 | opendart.fss.or.kr → 인증키 신청 (무료, 즉시) |
| `ANTHROPIC_API_KEY` | 사업 내용을 3~4문단으로 AI 요약 (선택) | console.anthropic.com → API Keys (유료, 종목당 1회만 요약) |

- `DART_API_KEY`가 없으면: 해당 칸이 비어 있고 나머지는 정상 작동
- `ANTHROPIC_API_KEY`가 없으면: 요약 대신 증권신고서 '사업의 개요' 원문 앞부분을 보여줌
- 키를 등록한 뒤 Actions → "데이터 업데이트 & 배포" → Run workflow 로 한 번 돌리면 반영

## 파일 구성
| 파일 | 역할 |
|---|---|
| `index.html` | 화면 |
| `scripts/update.py` | 데이터 수집 (KIND·38·네이버) |
| `scripts/dart.py` | DART 연동·AI 요약 |
| `scripts/valuation.py` | 증권신고서 공모가 산정 근거(PER·비교회사·할인율) 읽기 |
| `scripts/model.py` | 수익 확률 계산 |
| `scripts/kind.py` | KIND 예비심사·공모진행·신규상장 수집 |
| `scripts/pipeline.py` | 단계별 현황·월별 추이·일정 계산 → `data/pipeline.json` |
| `data/listing_track.csv` | **상장트랙 직접 입력** (자동 판별이 틀렸을 때) |
| `data/offer_prices.csv` | **공모가 직접 입력** (자동으로 못 찾은 경우) |
| `data/missing_offer.csv` | 공모가를 못 찾은 종목 목록 |
| `data/ipos.json`, `data/prices/`, `data/detail/` | 자동 생성 데이터 |

## 공모가가 '–'로 나올 때
`data/offer_prices.csv` → 연필(✏️) → `종목코드,회사명,공모가` 형식으로 한 줄씩 추가 → Commit changes

## 3개월 수익 확률이란
최근 5년 코스닥 공모주 중 지금 종목과 처지(상장 후 경과일, 시초가·공모가 대비 위치, 최근 20일 흐름, 고점 대비 낙폭)가
가장 비슷했던 사례들이 그 뒤 60거래일 동안 오른 비율입니다. 사이트 하단 설명에 검증 결과(예측 vs 실제)가 함께 나옵니다.
상장폐지 종목이 빠져 있고 실적·시장 흐름은 반영하지 않으므로 참고용 통계일 뿐입니다.
