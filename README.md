# 공시 데이터 수집기 (DART / EDGAR)

- **DART**: 금융감독원 OpenDART API — 회사 고유번호, 공시 목록, 재무제표
- **EDGAR**: 미국 SEC — 티커→CIK, 공시 목록, XBRL 재무 데이터

## 설정

```bash
pip install -r requirements.txt
cp .env.example .env   # 그다음 키와 User-Agent 입력
```

## 사용법

```bash
python main.py dart-corps                              # 전체 회사 고유번호
python main.py dart-filings 00126380 20250101 20251231 # 삼성전자 공시 목록
python main.py dart-fs 00126380 2024                   # 삼성전자 2024 사업보고서 재무제표
python main.py edgar-tickers                           # 전체 티커·회사명·CIK
python main.py edgar-filings AAPL 10-K 10-Q            # 애플 공시 목록
python main.py edgar-facts AAPL                        # 애플 XBRL 재무 데이터
```

결과는 `data/dart/`, `data/edgar/` 에 저장됩니다.

## 분기/연간 분석

유동자산, 총부채, 발행주식수, 순이익(연평균성장률), 자사주매입을 분기표와 연간표로 정리합니다.

```bash
python main.py analyze-dart 삼성전자 2015 2025      # 회사명 또는 고유번호, 시작·끝 연도
python main.py analyze-edgar AAPL                   # 공시된 전체 기간
python main.py analyze-edgar microsoft              # 회사명 또는 그 일부, 대소문자 무관
```

결과: `data/analysis/{dart,edgar}/<회사>_분기.csv`, `<회사>_연간.csv`

- 분기 순이익·자사주매입은 해당 분기 3개월치 값입니다. 4분기는 연간값에서 3분기까지의 누적값을 빼서 구합니다.
- `최근4분기` 열은 직전 4개 분기의 합계이고, 분기 CAGR은 이 값을 N년 전 같은 분기와 비교한 것입니다.
- 순이익 CAGR은 3년·5년·10년 세 가지를 모두 계산합니다. 비교할 과거 데이터가 없으면 빈칸입니다.
- 순이익이 0 이하인 구간이 있으면 CAGR은 빈칸입니다. 대신 두 열을 함께 봅니다.
  - `순이익상태_N년`: 흑자 / 흑자전환 / 적자전환 / 적자지속 (N년 전 → 현재)
  - `순이익연평균증감_N년`: (현재 − N년 전) ÷ N. 금액 기준이라 적자 구간에서도 계산되고, 방향도 맞습니다.
- DART 발행주식수는 반기·사업보고서에만 있는 경우가 많아, 빈 분기에는 직전 값이 들어갑니다.
- 자사주매입 주식수의 증가율은 연간표에서 전년 대비, 분기표에서 연초누적을 전년 같은 분기와 비교한 값입니다. 전년 값이 0이면 빈칸입니다.
- DART 자사주 수량은 반기·사업보고서에만 있는 경우가 많고, 2018년 이전은 수량 데이터가 없는 경우가 있습니다. 매입 금액이 0이면 0주로 표시하고, 금액이 있는데 수량이 없으면 빈칸입니다.
- EDGAR 자사주 수량 항목명은 회사마다 다릅니다. 표준 항목이 없는 회사(예: 알파벳)는 빈칸입니다.
- EDGAR는 회사의 회계연도 기준입니다. 예: 애플 2025Q1 = 2024년 10~12월
