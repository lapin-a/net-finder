# 미국 주가 출처 비교 (2026-10-08 조사)

미국 넷넷 스크리너(분기말 종가 × 공시 주식수 = 시가총액)와 주식분할 검증에 쓸 주가 출처 후보.
가격·한도는 자주 바뀌므로 결정 전에 공식 페이지에서 다시 확인할 것.

## 필요 조건

- **분할 미반영(원래) 종가**: 공시 주식수에 곱해야 하므로 분할 반영값은 안 됨
- **분할 이력**: 주식분할 보정 검증용
- **2009년부터, 약 7,000종목**: EDGAR 일괄 결과 범위
- **상장폐지 종목**: 과거 스크리닝이 살아남은 종목 쪽으로 쏠리지 않도록
- 분기말 종가만 있으면 되므로, 날짜 하나로 전 종목을 받는 API면 호출이 약 70번이면 끝남

## 1. 무료

| 출처 | 무료 범위 | 이력 | 원래 종가 | 상장폐지 | 평가 |
|---|---|---|---|---|---|
| Alpaca | 계정만 만들면 무료 | 2016년부터 | ✅ 기본값 (`adjustment` 옵션) | ? | 무료 중 가장 쓸 만함. 2009~2015년 없음. 여러 종목 동시 요청 가능 |
| Massive (옛 Polygon.io) | 분당 5회 | 2년 | ✅ `adjusted=false` | ✅ | 날짜별 전 종목 일괄 조회 가능. 2024년 이후만 |
| FMP | 하루 250회 | 5년 | ✅ 원래/분할/배당 선택 | ✅ | 7,000종목에 약 한 달 |
| SimFin | 일괄 CSV | 5년 | ? | ? | 파일 하나로 받음 |
| Tiingo | 월 500종목, 하루 1,000회 | 30년 넘음 | ✅ | ✅ | 전체를 받는 데 14개월 |
| Twelve Data | 하루 800회 | 주요 종목 1980년경부터 | ? | ? | 비상업 용도만 |
| yfinance (Yahoo) | 비공식 | 길다 | ❌ 분할 반영값 | ❌ | 요청이 자주 막힘 |
| Stooq | 일괄 다운로드 | 길다 | 불확실 | 불확실 | 보안문자를 사람이 풀어야 함 |

## 2. 저가 유료 (한 달만 결제하고 해지)

| 출처 | 가격 | 이력 | 원래 종가 | 상장폐지 | 방식 |
|---|---|---|---|---|---|
| Tiingo Power | 월 $30 | 30년 넘음 | ✅ `close` + `splitFactor` | ✅ | 종목별, 하루 10만 건 |
| EODHD | 월 €19.99 | 길다 | ✅ `close` / `adjusted_close` | ✅ | 날짜별 일괄 가능 (확인 필요) |
| FMP Premium | 월 $59 (연간 결제 기준) | 30년 넘음 | ✅ | ✅ | 종목별 |
| Alpha Vantage | 월 $49.99부터 | 20년 넘음 | ✅ 원래 가격 API 따로 | 상장폐지 목록 제공 | 종목별 |
| Massive Developer | 월 $79 | 10년 | ✅ | ✅ | 날짜별 일괄 |

## 3. 연구용 고급

| 출처 | 가격 | 특징 |
|---|---|---|
| CRSP (WRDS) | 대학·기관 구독 | 1925년부터 원래 종가와 발행주식수가 같이 있음, 상장폐지 포함. 학계 표준 |
| Sharadar (Nasdaq Data Link) | 비공개 | 1998년부터, 상장폐지 포함, CIK가 연결돼 있어 티커 변경 문제 없음 |
| Norgate Data | 상장폐지 포함 등급 연 $630 | 1990년부터, 전용 프로그램 설치 필요 (API 아님) |
| Databento | 쓴 만큼 결제 또는 월 $199 | 2018년경부터 (2010년까지 확장 중) |

## 제외

- Marketstack: 무료가 월 100회
- Finnhub: 무료는 실시간 위주
- IEX Cloud: 2024년 서비스 종료

## 추천

| 상황 | 추천 |
|---|---|
| 무료 | Alpaca로 2016년 이후를 채우고, 2009~2015년은 비워 둠 |
| 소액으로 확실하게 | Tiingo Power 한 달($30)로 전 종목 전 기간을 받고 해지 |
| 대학 소속 | CRSP. 원래 종가와 주식수가 같이 있어 EDGAR 주식수까지 검증 가능 |

## 주의: 티커 변경

EDGAR 결과는 현재 티커 기준이다. 과거 티커가 달랐던 회사(예: FB → META)는 티커만으로는 옛 가격과 연결이 안 될 수 있다.
CIK가 연결된 출처(Sharadar, CRSP)가 아니면 회사별 확인이 필요하다.

## 출처

- [Tiingo pricing](https://www.tiingo.com/about/pricing)
- [Alpaca historical data docs](https://py-alpaca-api.readthedocs.io/en/latest/stock/history.html)
- [Alpaca market data API v2](https://alpaca.markets/blog/market-data-apiv2/)
- [Massive/Polygon pricing](https://api.qveris.ai/guides/polygon-pricing-optimized)
- [FMP pricing plans](https://site.financialmodelingprep.com/de/pricing-plans)
- [FMP delisted companies](https://site.financialmodelingprep.com/how-to/how-to-handle-delisted-companies-and-historical-symbols-with-a-free-api)
- [EODHD pricing (G2)](https://www.g2.com/products/eodhd-financial-data-apis/pricing)
- [Alpha Vantage limits](https://www.macroption.com/alpha-vantage-api-limits/)
- [SimFin prices](https://simfin.com/en/prices)
- [Twelve Data / Marketstack / Finnhub comparison](https://blog.apilayer.com/analyzing-the-top-free-apis-for-stock-data/)
- [Norgate subscribe](https://norgatedata.com/subscribe/subscribe.php)
- [Databento US Equities](https://databento.com/blog/introducing-databento-us-equities)
- [CRSP (McMaster library)](https://library.mcmaster.ca/databases/crsp)
- [Sharadar via QuantRocket](https://www.quantrocket.com/docs/data/fundamental/sharadar/)
- [yfinance Close change](https://kerryback.substack.com/p/02-online-data)
- [Stooq intro](https://www.quantstart.com/articles/an-introduction-to-stooq-pricing-data)
