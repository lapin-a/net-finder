# net-finder

한국(DART·KRX)과 미국(SEC EDGAR) 공시 데이터를 모아, 그레이엄식 **넷넷(net-net)** 분석에 쓰는 지표를 분기·연간으로 정리합니다.

## 지표

| 지표 | 계산 |
|---|---|
| 유동자산, 총부채 | 재무상태표 |
| 발행주식수 | DART 일괄: KRX 상장주식수(보통주+우선주) / DART 회사별: 주식총수 현황 / EDGAR: 표지(dei) |
| **주당순유동자산** | (유동자산 − 총부채) ÷ 발행주식수 |
| 순이익 연평균성장률(CAGR) | 3년·5년·10년. 처음·마지막이 모두 흑자일 때만 계산 |
| 순이익 상태 | 흑자 / 흑자전환 / 적자전환 / 적자지속 (N년 전 → 현재) |
| 순이익 연평균 증감 | (현재 − N년 전) ÷ N. 적자 구간에서도 계산되고 방향이 맞음 |
| 자사주매입 | 금액, 매입 주식수, 주식수 전년 대비 증가율 |

## 데이터 출처

| 출처 | 내용 | 인증 | 한도 |
|---|---|---|---|
| [OpenDART](https://opendart.fss.or.kr) | 한국 상장사 재무제표, 주식수, 자기주식 | API 키 | 하루 20,000건 |
| [KRX Open API](https://openapi.krx.co.kr) | 코스피·코스닥·코넥스 일별 종가, 시가총액, 상장주식수 | API 키 (서비스별 이용 신청) | 서비스별 |
| [SEC EDGAR](https://www.sec.gov/os/accessing-edgar-data) | 미국 상장사 XBRL 재무 데이터(companyfacts) | User-Agent(이름·이메일) | 초당 10회 |

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env`에 값을 넣습니다.

```
DART_API_KEY=...
EDGAR_USER_AGENT=이름 이메일
KRX_API_KEY=...
```

KRX는 **유가증권·코스닥·코넥스 일별매매정보** 서비스를 이용 신청해야 합니다.

## 사용법

### 목록

```bash
python main.py dart-corps        # DART 회사 고유번호·회사명 → data/dart/corp_codes.csv
python main.py edgar-tickers     # SEC 티커·회사명·CIK → data/edgar/tickers.csv
```

### 한 회사 분석

```bash
python main.py analyze-dart 삼성전자 2015 2025   # 회사명 또는 고유번호, 시작·끝 연도
python main.py analyze-edgar AAPL                # 티커
python main.py analyze-edgar microsoft           # 회사명 또는 그 일부, 대소문자 무관
```

→ `data/analysis/{dart,edgar}/<회사>_분기.csv`, `<회사>_연간.csv`

### 일괄 분석

```bash
# 한국 상장사 전체: 유동자산·총부채·순이익(다중회사 API, 100개씩) + KRX 발행주식수
python main.py analyze-dart-batch 2015 2025

# + 연간 자사주매입 (회사별 호출, 연도당 약 5,500건). 적힌 연도 순서대로 수집
python main.py analyze-dart-batch 2015 2025 --buyback 2025 2024

# 미국: 티커가 A로 시작하는 회사 전체
python main.py analyze-edgar-batch A
```

→ `data/analysis/dart/batch_상장사_{분기,연간}.csv`, `data/analysis/edgar/batch_<글자>_{분기,연간,실패}.csv`

API 응답은 `data/cache/`에 저장됩니다. 한도 초과나 네트워크 오류로 멈춰도 같은 명령을 다시 실행하면 받은 부분은 건너뛰고 이어서 받습니다. DART 일일 한도(오류 020)에 걸리면 거기까지 저장하고 멈춥니다.

### 원본 데이터

```bash
python main.py dart-filings 00126380 20250101 20251231   # 공시 목록
python main.py dart-fs 00126380 2024                     # 사업보고서 전체 재무제표
python main.py edgar-filings AAPL 10-K 10-Q              # 공시 목록
python main.py edgar-facts AAPL                          # XBRL 재무 데이터 전체
```

## 결과 해석 시 주의

**분기·기간**
- 분기 순이익·자사주매입은 그 분기 3개월치입니다. 4분기는 연간값에서 3분기 누적값을 빼서 구합니다.
- `최근4분기` 열은 직전 4개 분기 합계이고, 분기표의 CAGR·상태·증감은 이 값을 N년 전 같은 분기와 비교합니다.
- EDGAR는 회사의 회계연도 기준입니다. 예: 애플 2025Q1 = 2024년 10~12월.

**발행주식수**
- DART 일괄은 분기말(휴장이면 직전 거래일) KRX 상장주식수입니다. 우선주는 종목코드 앞 5자리가 같고 이름이 보통주로 시작하면 합산합니다.
- DART 회사별 분석은 반기·사업보고서에만 주식수가 있는 경우가 많아, 빈 분기에는 직전 값이 들어갑니다.
- 주식분할 전 연도는 분할 전 주식수 그대로입니다. 예: 삼성전자 2018년 50:1, 애플 2020년 4:1.

**순이익·재무제표**
- 연결재무제표 우선, 없으면 별도재무제표입니다. 순이익은 비지배지분 포함 당기순이익입니다.
- 은행·보험사는 유동자산을 구분하지 않아 주당순유동자산이 빈칸입니다.
- 금융 자회사가 있는 회사(현대차 등)는 연결 기준 부채가 커서 주당순유동자산이 크게 음수로 나옵니다.

**자사주매입**
- 주식수 증가율은 연간표에서 전년 대비, 분기표에서 연초누적을 전년 같은 분기와 비교합니다. 전년 값이 0이면 빈칸입니다.
- DART: 자기주식 수량이 미공시이면 매입 금액이 0일 때만 0주로 보고, 금액이 있으면 빈칸입니다. 일괄 분석은 현재 2024·2025 연간만 수집했습니다.
- EDGAR: 회사가 연도마다 항목명을 바꾸는 경우가 많아 표준 항목 3가지를 모두 합칩니다. 회사 자체 항목으로만 공시하는 회사는 빈칸입니다.

**EDGAR 일괄**
- 10-K/10-Q 제출 회사만 대상입니다. 해외 기업(20-F, 40-F)과 재무 데이터가 없는 ETF·펀드 등은 빠지거나 `실패` 파일에 기록됩니다.

## 구조

```
main.py              실행 명령
analysis.py          출처별 데이터를 같은 형태로 맞추고 분기·연간 지표표 생성
collectors/
  dart.py            OpenDART (재무제표, 다중회사 주요계정, 주식총수, 자기주식)
  edgar.py           SEC EDGAR (티커, 공시 목록, companyfacts)
  krx.py             KRX Open API (일별매매정보, 분기말 상장주식수)
TODO.md              남은 작업
```

## 남은 작업

[TODO.md](TODO.md) 참고. 다음 우선순위는 KRX 종가를 붙여 **주가가 주당순유동자산보다 싼 종목을 걸러내는 스크리너**입니다.
