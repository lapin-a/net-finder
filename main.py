"""수집 실행 스크립트.

예시:
    python main.py dart-corps
    python main.py dart-filings 00126380 20250101 20251231
    python main.py dart-fs 00126380 2024
    python main.py edgar-tickers
    python main.py edgar-filings AAPL 10-K 10-Q
    python main.py edgar-facts AAPL

    # 분기/연간 분석 (유동자산, 총부채, 발행주식수, 순이익 CAGR, 자사주매입)
    python main.py analyze-dart 삼성전자 2015 2025
    python main.py analyze-edgar AAPL
    python main.py analyze-edgar microsoft       # 회사명(일부)도 가능
    python main.py analyze-edgar-batch A         # 티커가 A로 시작하는 회사 전체
    python main.py analyze-dart-batch 2015 2025  # 상장사 전체 (유동자산·총부채·순이익 + KRX 발행주식수)
    python main.py analyze-dart-batch 2015 2025 --buyback 2025 2024  # + 연간 자사주매입 (회사별 호출)
    python main.py detect-splits                 # 일괄 결과에서 주식분할·병합 후보 탐지
    python main.py fill-edgar-buyback            # EDGAR 일괄 결과의 연간 자사주매입 주식수 빈칸을 10-K XBRL 원문으로 채움
    python main.py fill-edgar-buyback A B        # 해당 글자 파일만
    python main.py fill-edgar-buyback --recheck  # 이미 XBRL·추정으로 채운 칸도 지금 규칙으로 다시 확인
    python main.py fill-edgar-buyback-quarterly  # 분기 자사주매입 주식수 빈칸(·0주)을 10-Q·10-K XBRL 원문으로 채움
    python main.py fix-edgar-buyback             # companyfacts 자사주매입 주식수 중 이상한 값을 XBRL 원문 값으로 교체
    python main.py estimate-edgar-buyback        # 그래도 남은 자사주매입 주식수 빈칸(연간·분기)을 금액 ÷ 기간 평균 주가로 추정
    python main.py refresh-edgar PRTH WTBA       # 일부 회사만 companyfacts로 다시 계산해 글자별 파일에 반영
    python main.py fill-edgar-ends               # 글자별 결과에 보고기간 종료일 열 추가 (값은 그대로)
    python main.py fill-edgar-balance            # 글자별 결과의 발행주식수·우선주·비지배지분·주당순유동자산만 다시 계산 (나머지 열은 그대로)
    python main.py analyze-foreign-batch         # 해외 기업(20-F·40-F) → edgar/해외_분기.csv, 해외_연간.csv (달러 환산)
    python main.py foreign-share-check 2026-09 --no-profit --no-buyback   # 해외 결과(같은 조건의 screen-edgar 파일) 중 확인 필요 종목의 표지 뒤 공시에서 발행주식수 문장 찾기 (검토용)
    python main.py merge-edgar                   # 글자별 결과를 edgar/연간.csv, edgar/분기.csv로 합침
    python main.py us-prices                     # 미국 월별 주가 (Alpaca, 2016년부터) → edgar/주가_월별.csv

    # 넷넷 스크리너 (analyze-dart-batch 결과 + KRX 종가·시가총액)
    python main.py screen-dart                   # 최신 연도, 기본 조건
    python main.py screen-dart 2024 --ratio 1 --profit 흑자 --profit-years 5 --no-buyback
    python main.py screen-dart 2025 --price-date 20261007   # 연말 대신 해당일(직전 거래일) 주가로 비교
    python main.py screen-edgar                  # 미국: 최신 월말 주가 + 회사별로 그때 공시됐을 최신 분기
    python main.py screen-edgar 2024-12 --no-buyback
"""
import csv
import json
import math
import os
import re
import statistics
import sys
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

import analysis
from collectors import AlpacaClient, DartClient, EdgarClient, KrxClient

DATA_DIR = Path(__file__).parent / "data"


def save(name: str, data) -> None:
    path = DATA_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    count = len(data) if isinstance(data, list) else 1
    print(f"저장 완료: {path} ({count}건)")


def save_csv(name: str, rows: list[dict], fields: list[str]) -> None:
    path = DATA_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: 엑셀에서 한글이 깨지지 않도록 BOM 포함
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"저장 완료: {path} ({len(rows)}건)")


def resolve_corp(dart: DartClient, query: str) -> tuple[str, str]:
    """회사명 또는 고유번호(8자리) → (고유번호, 회사명)."""
    path = DATA_DIR / "dart/corp_codes.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig") as f:
            corps = list(csv.DictReader(f))
    else:
        corps = dart.corp_codes()
    matches = [c for c in corps if query in (c["corp_code"], c["corp_name"])]
    if len(matches) != 1:
        found = ", ".join(f"{c['corp_name']}({c['corp_code']})" for c in matches[:10])
        sys.exit(f"'{query}' 회사를 하나로 특정할 수 없습니다: {found or '없음'} → 고유번호로 입력하세요")
    return matches[0]["corp_code"], matches[0]["corp_name"]


def resolve_ticker(edgar: EdgarClient, query: str) -> tuple[str, str]:
    """티커 또는 회사명 → (CIK, 대표 티커).

    티커 일치 → 회사명 일치 → 회사명 일부 포함 순으로 찾고, 대소문자는 구분하지 않는다.
    """
    path = DATA_DIR / "edgar/tickers.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig") as f:
            companies = list(csv.DictReader(f))
    else:
        companies = edgar.tickers()

    q = query.strip().lower()
    for test in (
        lambda c: c["ticker"].lower() == q,
        lambda c: c["name"].lower() == q,
        lambda c: q in c["name"].lower(),
    ):
        by_cik = {}
        for c in companies:  # 같은 회사의 여러 티커(GOOGL/GOOG)는 하나로
            if test(c):
                by_cik.setdefault(c["cik"], c)
        if len(by_cik) == 1:
            c = next(iter(by_cik.values()))
            return c["cik"], c["ticker"]
        if len(by_cik) > 1:
            found = ", ".join(f"{c['name']}({c['ticker']})" for c in list(by_cik.values())[:10])
            sys.exit(f"'{query}' 회사가 여러 개입니다: {found} → 티커로 입력하세요")
    sys.exit(f"'{query}' 회사를 찾을 수 없습니다")


def analyze_edgar_batch(edgar: EdgarClient, prefix: str) -> None:
    """티커가 prefix로 시작하는 회사 전체를 분석해 통합 CSV로 저장."""
    prefix = prefix.upper()
    companies = {}
    for c in edgar.tickers():
        if c["ticker"].startswith(prefix):
            companies.setdefault(c["cik"], c)  # 같은 회사 여러 티커는 첫 티커로

    q_all, a_all, failed = [], [], []
    for i, (cik, c) in enumerate(companies.items(), 1):
        try:
            quarters, annual = analysis.edgar_records(edgar.company_facts(cik))
        except Exception as e:  # 재무 데이터가 없는 회사(펀드, 신규상장 등) 등
            failed.append({"ticker": c["ticker"], "name": c["name"], "cik": cik, "error": str(e)[:200]})
            continue
        head = {"티커": c["ticker"], "회사명": c["name"]}
        q_all += [{**head, **row} for row in analysis.quarterly_table(quarters)]
        a_all += [{**head, **row} for row in analysis.annual_table(annual)]
        if i % 50 == 0:
            print(f"{i}/{len(companies)} 진행 (실패 {len(failed)})", flush=True)

    tag = f"edgar/batch_{prefix}"
    save_csv(f"analysis/{tag}_분기.csv", q_all, list(q_all[0]) if q_all else [])
    save_csv(f"analysis/{tag}_연간.csv", a_all, list(a_all[0]) if a_all else [])
    save_csv(f"analysis/{tag}_실패.csv", failed, ["ticker", "name", "cik", "error"])


FX_URL = "https://api.frankfurter.dev/v1"  # 유럽중앙은행 기준환율 (영업일, 키 없음)


def usd_fx():
    """fx(통화, 'YYYY-MM-DD') → 1 단위의 달러 가치. 그날(휴일이면 10일 안의 직전 영업일) 환율, 없으면 None.

    Frankfurter(ECB) 30개 통화. 대만 달러·아르헨티나 페소 등은 없음. 연도별로 data/cache/fx/usd_<연도>.json에 저장 (올해는 매번 다시 받음).
    """
    rates = {}
    for year in range(2009, date.today().year + 1):
        cache = DATA_DIR / f"cache/fx/usd_{year}.json"
        if cache.exists() and year < date.today().year:
            rates |= json.loads(cache.read_text(encoding="utf-8"))
            continue
        resp = requests.get(f"{FX_URL}/{year}-01-01..{year}-12-31", params={"base": "USD"}, timeout=60)
        resp.raise_for_status()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(resp.json()["rates"]), encoding="utf-8")
        rates |= resp.json()["rates"]
    days = sorted(rates)

    def fx(currency: str, day: str):
        if currency == "USD":
            return 1.0
        d = date.fromisoformat(day)
        for back in range(11):
            per_usd = rates.get((d - timedelta(days=back)).isoformat(), {}).get(currency)
            if per_usd:
                return 1 / per_usd
        return None
    fx.last_day = days[-1] if days else None
    return fx


def analyze_foreign_batch(edgar: EdgarClient, workers: int = 6) -> None:
    """해외 기업(20-F·40-F)을 분석해 edgar/해외_분기.csv, 해외_연간.csv로 저장한다 (미국 글자별 파일과 따로).

    대상: SEC 티커 목록에서 미국 합친 표(분기.csv)에 없는 회사 중 유동자산을 20-F·40-F로 공시한 회사.
    금액은 달러 (기간 말 환율, analysis.foreign_records). 분기표에 '제출일'(그 기간이 처음 공시된 날), '통화', '환율' 열.
    """
    fx = usd_fx()
    us = {r["CIK"] for r in _load_csv(DATA_DIR / "analysis/edgar/분기.csv")}
    companies = {}
    for c in edgar.tickers():
        if c["cik"] not in us:
            companies.setdefault(c["cik"], c)
    print(f"미국 표에 없는 회사 {len(companies)}곳", flush=True)

    def one(cik):
        try:
            facts = edgar.company_facts(cik)
        except requests.HTTPError as e:
            return None, f"companyfacts {e.response.status_code}"
        if "facts" not in facts:
            return None, None
        taxonomy = analysis.foreign_taxonomy(facts)
        ca_tag = "AssetsCurrent" if taxonomy == "us-gaap" else "CurrentAssets"
        if taxonomy is None or not any(e["form"] in ("20-F", "20-F/A", "40-F", "40-F/A")
                                       for e in analysis._foreign_entries(facts, taxonomy, [ca_tag])):
            return None, None  # 해외 공시가 아님 (펀드, 미국 회사 등)
        try:
            return analysis.foreign_records(facts, fx), taxonomy
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:150]}"

    q_all, a_all, failed, done = [], [], [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (cik, c), (records, info) in zip(companies.items(), pool.map(one, companies)):
            done += 1
            if done % 200 == 0:
                print(f"{done}/{len(companies)} 진행, 해외 기업 {len({r['CIK'] for r in q_all})}곳", flush=True)
            if records is None:
                if info:
                    failed.append({"ticker": c["ticker"], "name": c["name"], "cik": cik, "error": info})
                continue
            quarters, annual = records
            head = {"CIK": cik, "티커": c["ticker"], "회사명": c["name"]}
            for (key, rec), row in zip(sorted(quarters.items()), analysis.quarterly_table(quarters)):
                extra = {"제출일": rec["filed"], "통화": rec.get("currency"), "환율": rec.get("fx"), "회계기준": info,
                         "가중평균주식수": rec.get("weighted_shares")}
                q_all.append({**head, "기간": row.pop("기간"), "종료일": row.pop("종료일"), **extra, **row})
            for row in analysis.annual_table(annual):
                rec = annual[row["연도"]]
                a_all.append({**head, "연도": row.pop("연도"), "종료일": row.pop("종료일", rec.get("end")),
                              "통화": rec.get("currency"), "환율": rec.get("fx"), "회계기준": info, **row})
    save_csv("analysis/edgar/해외_분기.csv", q_all, list(q_all[0]) if q_all else [])
    save_csv("analysis/edgar/해외_연간.csv", a_all, list(a_all[0]) if a_all else [])
    save_csv("analysis/edgar/해외_실패.csv", failed, ["ticker", "name", "cik", "error"])
    print(f"해외 기업 {len({r['CIK'] for r in q_all})}곳 (분기표 {len(q_all)}행, 연간표 {len(a_all)}행), 실패 {len(failed)}곳")


BUYBACK_PRICE_RANGE = (0.1, 10_000)  # 달러/주


def _parsed_10k(edgar: EdgarClient, cik: str, accn: str) -> dict:
    """10-K·10-Q XBRL 원문에서 뽑은 자사주매입 후보 (data/cache/edgar_xbrl에 저장해 두고 재사용)."""
    cache = DATA_DIR / "cache/edgar_xbrl" / f"{accn}.json"
    if cache.exists():
        parsed = json.loads(cache.read_text(encoding="utf-8"))
        if parsed.get("v") == 3 or "error" in parsed:  # 예전 형식(다른 차원·자기주식 잔액 없음)은 다시 받음
            return parsed
    try:
        parsed = analysis.xbrl_buyback_facts(edgar.filing_instance(cik, accn))
    except (FileNotFoundError, SyntaxError) as e:  # XBRL 없는 옛 공시, 깨진 파일 (ParseError는 SyntaxError 하위)
        parsed = {"error": str(e)[:200]}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
    return parsed


def _search_10k_buyback(edgar: EdgarClient, cik: str, years: set[int], estimate: bool = True) -> tuple[dict, int]:
    """한 회사의 빈 연도들을 최신 10-K부터 거꾸로 찾는다. 반환: ({연도: (주식수, 출처)}, 확인한 10-K 수)

    estimate면 매입 주식수 항목을 끝내 못 찾은 연도를 자기주식 잔액 증가분(추정)으로 채운다.
    """
    found, estimated, remaining, searched = {}, {}, set(years), 0
    periods = analysis.edgar_periods(edgar.company_facts(cik))  # 회계연도를 분기·연간표와 같은 기준으로
    tenks = sorted(edgar.filings(cik, ("10-K",), include_older=True), key=lambda x: x["reportDate"], reverse=True)
    for filing in tenks:
        if not remaining or not filing["reportDate"]:
            break
        report_year = int(filing["reportDate"][:4])
        if report_year < min(remaining) - 1:
            break
        if not any(report_year - 3 <= y <= report_year + 1 for y in remaining):
            continue
        parsed = _parsed_10k(edgar, cik, filing["accessionNumber"])
        searched += 1
        if "error" in parsed:
            continue
        fy = periods.get(filing["accessionNumber"], (None, None))[1]
        for year, value in analysis.annual_buyback_from_xbrl(parsed, fy).items():
            if year in remaining:
                found[year] = value
                remaining.discard(year)
        for year, value in analysis.treasury_increase_from_xbrl(parsed, fy).items():
            estimated.setdefault(year, value)  # 최신 10-K 값 우선
    if not estimate:
        return found, searched
    return {**{y: estimated[y] for y in remaining if y in estimated}, **found}, searched


def _load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _save_edgar_annual(path: Path, rows: list[dict]) -> None:
    """EDGAR 연간표 저장: 자사주매입 주식수 전년 대비 증가율을 다시 계산하고 출처 열을 주식수 옆에 둔다."""
    prev = {}
    for r in sorted(rows, key=lambda r: (r["티커"], int(r["연도"]))):
        p = prev.get((r["티커"], int(r["연도"]) - 1))
        r["자사주매입주식수_전년대비(%)"] = analysis._growth(analysis._num(r["자사주매입주식수"]),
                                                   analysis._num(p and p["자사주매입주식수"]))
        prev[(r["티커"], int(r["연도"]))] = r
    fields = [k for k in rows[0] if k != "자사주매입주식수_출처"] if rows else []
    if fields:
        fields.insert(fields.index("자사주매입주식수") + 1, "자사주매입주식수_출처")
    save_csv(f"analysis/edgar/{path.name}", rows, fields)


def _save_edgar_quarterly(path: Path, rows: list[dict]) -> None:
    """EDGAR 분기표 저장: 자사주매입 주식수 연초누적·전년 동기 대비를 다시 계산하고 출처 열을 주식수 옆에 둔다."""
    num, src_col = analysis._num, "자사주매입주식수(분기)_출처"
    by_key = {(r["티커"], r["기간"]): r for r in rows}
    for r in rows:
        fy, q = int(r["기간"][:4]), int(r["기간"][-1])
        parts = [by_key.get((r["티커"], f"{fy}Q{k}"), {}).get("자사주매입주식수(분기)", "") for k in range(1, q + 1)]
        r["자사주매입주식수(연초누적)"] = "" if "" in parts else sum(num(v) for v in parts)
    for r in rows:
        prev = by_key.get((r["티커"], f"{int(r['기간'][:4]) - 1}{r['기간'][4:]}"))
        r["자사주매입주식수_전년동기대비(%)"] = analysis._growth(num(r["자사주매입주식수(연초누적)"]),
                                                     num(prev and prev["자사주매입주식수(연초누적)"]))
    fields = [k for k in rows[0] if k != src_col] if rows else []
    if fields:
        fields.insert(fields.index("자사주매입주식수(분기)") + 1, src_col)
    save_csv(f"analysis/edgar/{path.name}", rows, fields)


def _letter_paths(kind: str) -> list[Path]:
    """글자별 일괄 결과 파일. batch_AA는 A 파일과 겹치므로 제외."""
    return [p for p in sorted((DATA_DIR / "analysis/edgar").glob(f"batch_*_{kind}.csv"))
            if len(p.stem.split("_")[1]) == 1]


def refresh_edgar_tickers(edgar: EdgarClient, tickers: list[str], workers: int = 6) -> None:
    """일부 회사만 companyfacts를 다시 받아 글자별 일괄 결과(연간·분기)의 해당 행을 바꾼다.

    분석 코드를 고친 뒤 일부 회사만 다시 계산할 때 사용. 연간표의 XBRL 보충값은 지워지므로 fill-edgar-buyback을 다시 실행.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    merged = DATA_DIR / "analysis/edgar/분기.csv"  # SEC 티커 목록에서 빠진 회사(상장 폐지 등)는 합친 표의 CIK
    if merged.exists():
        for r in _load_csv(merged):
            if r["CIK"]:
                ciks.setdefault(r["티커"], r["CIK"])
    by_letter = {}
    for ticker in (t.upper() for t in tickers):
        if ticker not in ciks:
            print(f"{ticker}: CIK를 찾을 수 없어 건너뜀")
            continue
        by_letter.setdefault(ticker[0], []).append(ticker)

    def tables(ticker):
        try:
            quarters, annual = analysis.edgar_records(edgar.company_facts(ciks[ticker]))
        except requests.HTTPError as e:  # companyfacts가 없어진 회사
            print(f"{ticker}: companyfacts 못 받음 ({e.response.status_code}), 건너뜀")
            return None
        return {"분기": analysis.quarterly_table(quarters), "연간": analysis.annual_table(annual)}

    for letter, group in sorted(by_letter.items()):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            computed = {t: v for t, v in zip(group, pool.map(tables, group)) if v is not None}
        group = [t for t in group if t in computed]
        for kind in ("분기", "연간"):
            path = DATA_DIR / f"analysis/edgar/batch_{letter}_{kind}.csv"
            rows = _load_csv(path)
            for ticker in group:
                at = next((i for i, r in enumerate(rows) if r["티커"] == ticker), None)
                if at is None:
                    print(f"{ticker}: {path.name}에 없음")
                    continue
                head = {"티커": ticker, "회사명": rows[at]["회사명"]}
                new = [{**head, **row} for row in computed[ticker][kind]]
                if kind == "연간":
                    for r in new:
                        r["자사주매입주식수_출처"] = "companyfacts" if r["자사주매입주식수"] not in ("", None) else ""
                rows = rows[:at] + new + [r for r in rows[at:] if r["티커"] != ticker]
            if kind == "연간":
                _save_edgar_annual(path, rows)
            else:
                save_csv(f"analysis/edgar/{path.name}", rows, list(rows[0]))


def fill_edgar_ends(edgar: EdgarClient, workers: int = 6) -> None:
    """글자별 일괄 결과(연간·분기)에 보고기간 종료일 열을 넣는다. 값은 다시 계산하지 않는다 (채운 자사주매입 유지).

    종료일 열이 생기기 전에 만든 결과용. 종료일은 edgar_records와 같은 규칙(edgar_periods, 겹치면 늦은 종료일).
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}

    def ends(cik):
        by_period = {}
        for accn, (end, fy, q) in sorted(analysis.edgar_periods(edgar.company_facts(cik)).items(), key=lambda x: x[1][0]):
            by_period[(fy, q)] = end
        return by_period

    for letter in sorted({p.stem.split("_")[1] for p in _letter_paths("분기")}):
        quarterly = _load_csv(DATA_DIR / f"analysis/edgar/batch_{letter}_분기.csv")
        annual = _load_csv(DATA_DIR / f"analysis/edgar/batch_{letter}_연간.csv")
        tickers = sorted({r["티커"] for r in quarterly + annual if r["티커"] in ciks})
        with ThreadPoolExecutor(max_workers=workers) as pool:
            found = dict(zip(tickers, pool.map(lambda t: ends(ciks[t]), tickers)))
        missing = 0
        for rows, key in ((quarterly, "기간"), (annual, "연도")):
            for i, r in enumerate(rows):
                fy, q = (int(r[key][:4]), int(r[key][-1])) if key == "기간" else (int(r[key]), 4)
                end = found.get(r["티커"], {}).get((fy, q), "")
                missing += not end
                head = list(r)[:list(r).index(key) + 1]
                rows[i] = {**{k: r[k] for k in head}, "종료일": end, **{k: v for k, v in r.items() if k not in head}}
        save_csv(f"analysis/edgar/batch_{letter}_분기.csv", quarterly, list(quarterly[0]))
        _save_edgar_annual(DATA_DIR / f"analysis/edgar/batch_{letter}_연간.csv", annual)
        print(f"{letter}: 회사 {len(tickers)}곳, 종료일 못 찾은 행 {missing}", flush=True)


EDGAR_BALANCE_COLUMNS = {  # 지금 규칙으로 다시 계산하는 열: 앞 열 → 그 뒤에 둘 열
    "총부채": ["총부채_출처", "우선주", "우선주_출처", "비지배지분"],
    "발행주식수": ["발행주식수_출처", "발행주식수_기준일"],  # 분기표만
}


def fill_edgar_balance(edgar: EdgarClient, workers: int = 6) -> None:
    """글자별 일괄 결과(연간·분기)의 재무상태표 파생 열만 지금 규칙으로 다시 계산한다. 나머지 열(채운 자사주매입)은 그대로.

    다시 계산하는 열: 총부채(없으면 부채와자본총계 − 자본총계, 총부채_출처)·우선주(·우선주_출처)·비지배지분, 발행주식수(분기표는 '발행주식수_출처'·'발행주식수_기준일'), 주당순유동자산.
    - 발행주식수: 표지 값이 없는 분기는 3개월 가중평균, 둘 다 없으면 직전 값을 최대 3분기까지
    - 주당순유동자산 = (유동자산 − 총부채 − 우선주 − 비지배지분) ÷ 발행주식수
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    redone = ["총부채_출처", "우선주", "우선주_출처", "비지배지분", "발행주식수", "발행주식수_출처", "발행주식수_기준일", "주당순유동자산"]

    def tables(cik):
        quarters, annual = analysis.edgar_records(edgar.company_facts(cik))
        return ({r["기간"]: r for r in analysis.quarterly_table(quarters)},
                {str(r["연도"]): r for r in analysis.annual_table(annual)})

    totals = Counter()
    for letter in sorted({p.stem.split("_")[1] for p in _letter_paths("분기")}):
        quarterly = _load_csv(DATA_DIR / f"analysis/edgar/batch_{letter}_분기.csv")
        annual = _load_csv(DATA_DIR / f"analysis/edgar/batch_{letter}_연간.csv")
        tickers = sorted({r["티커"] for r in quarterly + annual if r["티커"] in ciks})
        with ThreadPoolExecutor(max_workers=workers) as pool:
            found = dict(zip(tickers, pool.map(lambda t: tables(ciks[t]), tickers)))
        stats = Counter()
        for rows, which, key in ((quarterly, 0, "기간"), (annual, 1, "연도")):
            for i, r in enumerate(rows):
                new = found.get(r["티커"], ({}, {}))[which].get(r[key])
                if new is None:  # 지금 companyfacts에 없는 기간: 원래 값 유지
                    stats["못 찾은 행"] += 1
                    new = r
                for col in ("총부채", "발행주식수", "주당순유동자산"):
                    if str(new.get(col) if new.get(col) is not None else "") != r.get(col, ""):
                        stats[f"{key} {col} 바뀜"] += 1
                out = {}
                for k, v in r.items():  # 원래 열 순서를 따르고, 다시 계산한 열은 정해진 자리에 넣음
                    if k in ("총부채", "발행주식수", "주당순유동자산"):
                        out[k] = new.get(k)
                    elif k not in redone:
                        out[k] = v
                    for extra in EDGAR_BALANCE_COLUMNS.get(k, []):
                        if not extra.startswith("발행주식수_") or which == 0:
                            out[extra] = new.get(extra)
                rows[i] = out
        _save_edgar_quarterly(DATA_DIR / f"analysis/edgar/batch_{letter}_분기.csv", quarterly)
        _save_edgar_annual(DATA_DIR / f"analysis/edgar/batch_{letter}_연간.csv", annual)
        totals += stats
        print(f"{letter}: 회사 {len(tickers)}곳, " + ", ".join(f"{k} {v}" for k, v in stats.items()), flush=True)
    print("합계: " + ", ".join(f"{k} {v:,}" for k, v in totals.items()))


def _search_quarter_buyback(edgar: EdgarClient, cik: str, gaps: set, no_buyback: set) -> tuple[dict, int]:
    """한 회사의 빈 분기들을 10-Q·10-K XBRL 원문에서 찾는다. 반환: ({(회계연도, 분기): (주식수, 출처)}, 확인한 공시 수)

    분기는 companyfacts의 공시별 회계연도·분기(분기표와 같은 기준)로 공시를 고른다. 찾는 순서:
    1) 그 분기 공시의 3개월 값  2) 누적값 − 직전 분기 누적값 (직전 분기까지 매입이 없었으면 누적값 그대로)
    3) 다음 해 같은 분기 공시의 전년 비교값. 4분기는 10-K 연간값 − 3분기 누적값.
    no_buyback: 매입 금액이 0이거나 없는 분기 (누적값 차감 때 0주로 봄)
    """
    periods = analysis.edgar_periods(edgar.company_facts(cik))
    accns = {}
    for accn, (end, fy, q) in sorted(periods.items(), key=lambda x: x[0]):
        accns.setdefault((fy, q), []).append((accn, end))
    values, loaded, searched = {}, set(), 0

    def load(fy, q):
        nonlocal searched
        if (fy, q) in loaded:
            return
        loaded.add((fy, q))
        for accn, end in accns.get((fy, q), []):
            parsed = _parsed_10k(edgar, cik, accn)
            searched += 1
            if "error" not in parsed:
                for key, value in analysis.quarter_buyback_from_xbrl(parsed, end, fy, q).items():
                    values.setdefault(key, value)

    def ytd(fy, q):
        if q == 0 or all((fy, k) in no_buyback for k in range(1, q + 1)):
            return 0, None
        load(fy, q)
        return values.get(("ytd", fy, q)) or (None, None)

    found = {}
    for fy, q in sorted(gaps):
        load(fy, q)
        if ("3m", fy, q) in values:
            value, source = values[("3m", fy, q)]
            found[(fy, q)] = (value, f"{source}, 3개월")
            continue
        cum, source = values.get(("ytd", fy, q)) or (None, None)
        if cum is not None and q > 1:
            prev, _ = ytd(fy, q - 1)
            if prev is not None and cum - prev > 0:
                found[(fy, q)] = (cum - prev, f"{source}, 누적 차감")
                continue
        load(fy + 1, q)
        if ("3m", fy, q) in values:
            value, source = values[("3m", fy, q)]
            found[(fy, q)] = (value, f"{source}, 다음 해 비교값")
        elif q == 1 and ("ytd", fy, q) in values:
            value, source = values[("ytd", fy, q)]
            found[(fy, q)] = (value, f"{source}, 다음 해 비교값")
    return found, searched


def fill_edgar_buyback_quarterly(edgar: EdgarClient, letters: list[str], workers: int = 6,
                                 only: set[str] | None = None) -> None:
    """EDGAR 일괄 분기표에서 자사주매입 금액은 있는데 주식수가 비었거나 0주인 칸을 10-Q·10-K XBRL 원문으로 채운다.

    채운 뒤 연초누적과 전년 동기 대비를 다시 계산한다. 출처는 '자사주매입주식수(분기)_출처' 열.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    num, (lo, hi) = analysis._num, BUYBACK_PRICE_RANGE
    src_col = "자사주매입주식수(분기)_출처"
    paths = _letter_paths("분기")
    if letters:
        paths = [p for p in paths if p.stem.split("_")[1] in {l.upper() for l in letters}]

    for path in paths:
        rows = _load_csv(path)
        gaps, no_buyback = {}, {}
        for r in rows:
            if r.get(src_col) is None:
                r[src_col] = "companyfacts" if r["자사주매입주식수(분기)"] != "" else ""
            fy, q = int(r["기간"][:4]), int(r["기간"][-1])
            a, s = num(r["자사주매입(분기)"]), num(r["자사주매입주식수(분기)"])
            if not a or a <= 0:
                no_buyback.setdefault(r["티커"], set()).add((fy, q))
            elif (s is None and r[src_col] == "") or (s == 0 and r[src_col] == "companyfacts"):
                gaps.setdefault(r["티커"], set()).add((fy, q))

        filled = searched = rejected = failed = 0
        jobs = [(t, ciks[t], g) for t, g in gaps.items() if t in ciks and (only is None or t in only)]
        by_key = {(r["티커"], r["기간"]): r for r in rows}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_search_quarter_buyback, edgar, cik, g, no_buyback.get(t, set())) for t, cik, g in jobs]
            for i, ((ticker, _, _), future) in enumerate(zip(jobs, futures), 1):
                try:
                    found, n = future.result()
                except Exception as e:  # companyfacts가 없어진 회사 등
                    failed += 1
                    print(f"{ticker}: {str(e)[:100]}", flush=True)
                    continue
                searched += n
                for (fy, q), (shares, source) in found.items():
                    r = by_key[(ticker, f"{fy}Q{q}")]
                    if not lo <= num(r["자사주매입(분기)"]) / shares <= hi:
                        rejected += 1
                        continue
                    r["자사주매입주식수(분기)"], r[src_col] = shares, source
                    filled += 1
                if i % 50 == 0:
                    print(f"{path.stem}: {i}/{len(jobs)}개 회사, 공시 {searched}건 확인, {filled}칸 채움", flush=True)

        _save_edgar_quarterly(path, rows)
        print(f"{path.stem}: 빈칸 {sum(len(g) for g in gaps.values())}칸 중 {filled}칸 채움, "
              f"평균 매입가 이상으로 제외 {rejected}칸, 실패 {failed}개 회사 (공시 {searched}건 확인)", flush=True)


def fix_edgar_buyback(edgar: EdgarClient, workers: int = 6) -> None:
    """companyfacts에서 온 자사주매입 주식수 중 이상한 값을 10-K XBRL 원문 값으로 바꾼다.

    이상한 값: 음수, 금액이 있는데 0주, 평균 매입가가 $0.10~$10,000 밖, 같은 회사 ±2년 중앙값과 3배 넘게 차이.
    XBRL 값은 평균 매입가가 범위 안이고, 비교 대상이 있으면 ±2년 중앙값의 1/3~3배 안이면서 원래 값보다 가까울 때만 쓴다.
    못 바꾸면: 음수는 부호를 바꾼 값이 맞으면 그 값, 아니면 음수·0주·범위 밖은 빈칸('제외:' 출처), 인접 연도와만 다른 값은 그대로.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    num, (lo, hi) = analysis._num, BUYBACK_PRICE_RANGE
    stats = {}
    for path in _letter_paths("연간"):
        rows = _load_csv(path)
        price = {}  # 비교용 평균 매입가 (범위 안, 추정 아닌 값)
        for r in rows:
            a, s = num(r["자사주매입"]), num(r["자사주매입주식수"])
            if a and a > 0 and s and s > 0 and lo <= a / s <= hi and not r["자사주매입주식수_출처"].startswith("추정"):
                price[(r["티커"], int(r["연도"]))] = a / s

        def near_median(r):
            near = [price[(r["티커"], int(r["연도"]) + k)] for k in (-2, -1, 1, 2) if (r["티커"], int(r["연도"]) + k) in price]
            return statistics.median(near) if near else None

        suspects = {}  # 행 번호 → 이유
        for i, r in enumerate(rows):
            a, s = num(r["자사주매입"]), num(r["자사주매입주식수"])
            if r["자사주매입주식수_출처"] != "companyfacts" or s is None:
                continue
            if not a or a <= 0:
                if s < 0:  # 금액이 없어 맞는 값을 확인할 수 없는 음수
                    r["자사주매입주식수"], r["자사주매입주식수_출처"] = "", f"제외: companyfacts {s:g} (음수, 금액 없음)"
                    stats[("음수(금액 없음)", "비움")] = stats.get(("음수(금액 없음)", "비움"), 0) + 1
                continue
            med = near_median(r)
            if s < 0:
                suspects[i] = "음수"
            elif s == 0:
                suspects[i] = "0주"
            elif not lo <= a / s <= hi:
                suspects[i] = "범위밖"
            elif med and not 1 / 3 <= a / s / med <= 3:
                suspects[i] = "인접불일치"

        years = {}
        for i in suspects:
            years.setdefault(rows[i]["티커"], set()).add(int(rows[i]["연도"]))
        jobs = [(t, ciks[t], y) for t, y in years.items() if t in ciks]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(lambda j: _search_10k_buyback(edgar, j[1], j[2], estimate=False)[0], jobs)
            found = dict(zip((t for t, _, _ in jobs), results))

        for i, reason in suspects.items():
            r = rows[i]
            a, s, med = num(r["자사주매입"]), num(r["자사주매입주식수"]), near_median(r)
            original_ok = s > 0 and lo <= a / s <= hi

            def fits(shares, must_beat):
                if not shares or shares <= 0 or not lo <= a / shares <= hi:
                    return False
                if med is None:
                    return True
                gap = abs(math.log(a / shares / med))
                return gap <= math.log(3) and not (must_beat and original_ok and gap >= abs(math.log(a / s / med)))

            xbrl = found.get(r["티커"], {}).get(int(r["연도"]))
            if xbrl and xbrl[0] != s and fits(xbrl[0], must_beat=True):
                r["자사주매입주식수"], r["자사주매입주식수_출처"] = xbrl[0], f"{xbrl[1]}, companyfacts {s:g} 대체"
                action = "XBRL로 교체"
            elif reason == "음수" and fits(-s, must_beat=False):
                r["자사주매입주식수"], r["자사주매입주식수_출처"] = int(-s), "companyfacts (부호 수정)"
                action = "부호 수정"
            elif reason in ("음수", "0주", "범위밖"):
                r["자사주매입주식수"], r["자사주매입주식수_출처"] = "", f"제외: companyfacts {s:g} ({reason})"
                action = "비움"
            else:
                action = "그대로"
            stats[(reason, action)] = stats.get((reason, action), 0) + 1
        _save_edgar_annual(path, rows)
        print(f"{path.stem}: 의심 {len(suspects)}칸 ({len(jobs)}개 회사)", flush=True)

    for (reason, action), n in sorted(stats.items()):
        print(f"  {reason} → {action}: {n}칸")


def merge_edgar(edgar: EdgarClient) -> None:
    """글자별 일괄 결과를 연간 1개, 분기 1개 파일로 합친다 (data/analysis/edgar/연간.csv, 분기.csv).

    batch_AA는 A와 겹치므로 제외. 같은 회사(CIK)가 다른 티커로 두 파일에 들어갔으면 SEC 티커 목록에서
    그 회사의 첫 티커(대표 티커) 쪽만 남긴다. 맨 앞에 CIK 열을 붙인다 (지금 목록에 없는 티커는 빈칸).
    """
    cik_of, primary = {}, {}
    for c in edgar.tickers():
        cik_of[c["ticker"]] = c["cik"]
        primary.setdefault(c["cik"], c["ticker"])
    for kind in ("연간", "분기"):
        by_company = {}  # CIK(없으면 티커) → {티커: 행들}
        for path in _letter_paths(kind):
            for r in _load_csv(path):
                key = cik_of.get(r["티커"], r["티커"])
                by_company.setdefault(key, {}).setdefault(r["티커"], []).append({"CIK": cik_of.get(r["티커"], ""), **r})
        merged, dropped = [], []
        for key, tickers in by_company.items():
            keep = primary.get(key) if primary.get(key) in tickers else next(iter(tickers))
            merged += tickers[keep]
            dropped += [f"{t}→{keep}" for t in tickers if t != keep]
        save_csv(f"analysis/edgar/{kind}.csv", merged, list(merged[0]) if merged else [])
        print(f"{kind}: 회사 {len(by_company)}곳, 같은 회사의 다른 티커 {len(dropped)}개 제외: {', '.join(sorted(dropped))}")


def fill_edgar_buyback(edgar: EdgarClient, letters: list[str], workers: int = 6, recheck: bool = False,
                       only: set[str] | None = None) -> None:
    """EDGAR 일괄 연간표에서 자사주매입 금액은 있는데 주식수가 빈 칸을 10-K XBRL 원문으로 채운다.

    10-K 자본변동표에는 3개년이 들어 있으므로 최신 10-K부터 거꾸로 내려가며, 빈 연도가 다 채워지거나
    더 볼 10-K가 없으면 멈춘다. 결과는 원래 파일에 덮어쓰고 '자사주매입주식수_출처' 열을 붙인다.
    회사 workers개를 동시에 처리하되, 요청 간격은 EdgarClient가 스레드 전체를 합쳐 지킨다.
    recheck면 이미 XBRL·추정으로 채운 칸도 지금 규칙으로 다시 구해 바뀐 값은 고치고, 못 구하면 비운다.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    paths = _letter_paths("연간")
    if letters:
        paths = [p for p in paths if p.stem.split("_")[1] in {l.upper() for l in letters}]

    for path in paths:
        rows = _load_csv(path)
        gaps, rechecks = {}, {}  # rechecks: (티커, 연도) → 행
        for r in rows:
            if r.get("자사주매입주식수_출처") is None:
                r["자사주매입주식수_출처"] = "companyfacts" if r["자사주매입주식수"] != "" else ""
            amount = analysis._num(r["자사주매입"])
            source = r["자사주매입주식수_출처"]
            if amount and amount > 0 and r["자사주매입주식수"] == "" and source == "":
                gaps.setdefault(r["티커"], set()).add(int(r["연도"]))
            elif recheck and (source.startswith("XBRL") or source.startswith("추정")):
                gaps.setdefault(r["티커"], set()).add(int(r["연도"]))
                rechecks[(r["티커"], int(r["연도"]))] = r

        filled = searched = rejected = estimated = changed = cleared = 0
        jobs = [(ticker, ciks[ticker], years) for ticker, years in gaps.items()
                if ticker in ciks and (only is None or ticker in only)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_search_10k_buyback, edgar, cik, years) for _, cik, years in jobs]
            for i, ((ticker, _, _), future) in enumerate(zip(jobs, futures), 1):
                found, n = future.result()
                searched += n
                for (t, year), r in rechecks.items():
                    if t != ticker:
                        continue
                    old_value, old_source = r["자사주매입주식수"], r["자사주매입주식수_출처"]
                    replaced = old_source.partition(", companyfacts ")[2]  # fix-edgar-buyback이 바꾼 칸
                    new = found.get(year)
                    if new and replaced and new[1].startswith("추정"):
                        new = None  # 교체는 실제 매입 항목으로만
                    if new and BUYBACK_PRICE_RANGE[0] <= analysis._num(r["자사주매입"]) / new[0] <= BUYBACK_PRICE_RANGE[1]:
                        source = f"{new[1]}, companyfacts {replaced}" if replaced else new[1]
                        if str(new[0]) != str(old_value) or source != old_source:
                            r["자사주매입주식수"], r["자사주매입주식수_출처"] = new[0], source
                            changed += 1
                    else:
                        r["자사주매입주식수"] = ""
                        r["자사주매입주식수_출처"] = f"제외: companyfacts {replaced} (재검토)" if replaced else ""
                        cleared += 1
                for r in rows:
                    if r["티커"] == ticker and int(r["연도"]) in found and (ticker, int(r["연도"])) not in rechecks:
                        shares, source = found[int(r["연도"])]
                        # 회사가 단위를 잘못 적은 값 거르기: 평균 매입가가 $0.10~$10,000 밖이면 채우지 않음
                        if not BUYBACK_PRICE_RANGE[0] <= analysis._num(r["자사주매입"]) / shares <= BUYBACK_PRICE_RANGE[1]:
                            rejected += 1
                            continue
                        r["자사주매입주식수"], r["자사주매입주식수_출처"] = shares, source
                        filled += 1
                        estimated += source.startswith("추정")
                if i % 50 == 0:
                    print(f"{path.stem}: {i}/{len(jobs)}개 회사, 10-K {searched}건 확인, {filled}칸 채움", flush=True)

        # 추정치 거르기: 평균 매입가가 같은 회사 ±2년(추정 아닌 값)의 중앙값과 3배 넘게 차이 나면 다시 빈칸으로
        price = {(r["티커"], int(r["연도"])): analysis._num(r["자사주매입"]) / analysis._num(r["자사주매입주식수"])
                 for r in rows if not r["자사주매입주식수_출처"].startswith("추정")
                 and (analysis._num(r["자사주매입"]) or 0) > 0 and (analysis._num(r["자사주매입주식수"]) or 0) > 0}
        dropped = 0
        for r in rows:
            if not r["자사주매입주식수_출처"].startswith("추정"):
                continue
            near = [price[(r["티커"], int(r["연도"]) + k)] for k in (-2, -1, 1, 2) if (r["티커"], int(r["연도"]) + k) in price]
            if near and not 1 / 3 <= analysis._num(r["자사주매입"]) / analysis._num(r["자사주매입주식수"]) / statistics.median(near) <= 3:
                r["자사주매입주식수"], r["자사주매입주식수_출처"] = "", ""
                dropped += 1

        _save_edgar_annual(path, rows)
        if recheck:
            print(f"{path.stem}: 재검토 {len(rechecks)}칸 중 바뀜 {changed}칸, 못 구해 비움 {cleared}칸", flush=True)
        print(f"{path.stem}: 빈칸 {sum(len(y) for y in gaps.values()) - len(rechecks)}칸 중 {filled}칸 채움(추정 {estimated}칸), "
              f"평균 매입가 이상으로 제외 {rejected}칸, 인접 연도와 안 맞는 추정치 비움 {dropped}칸 (10-K {searched}건 확인)", flush=True)


PRICE_ESTIMATE = "주가 추정"


def estimate_edgar_buyback(max_share_ratio: float = 0.5) -> None:
    """EDGAR 글자별 결과(연간·분기)에서 자사주매입 금액은 있는데 주식수가 없는 칸을 금액 ÷ 기간 평균 주가로 추정한다.

    평균 주가: Alpaca 월봉 VWAP(원래 가격)의 거래량 가중 평균. 연간은 종료일까지 12개월(8개월 이상 있어야 함),
    분기는 3개월(2개월 이상). 주가가 2016년부터라 그 전 기간은 못 채운다.
    대상: 빈칸(XBRL로도 못 채운 칸, 틀린 companyfacts 값을 뺀 '제외:' 칸 포함)과 분기의 companyfacts 0주.
    거르기: 추정 주식수가 발행주식수의 max_share_ratio배 초과, 평균 주가가 같은 회사 인접 기간(연간 ±2년, 분기 ±4분기)
    실제 평균 매입가 중앙값의 1/3~3배 밖.
    출처 열: '주가 추정: VWAP $x, n개월 / 이전: <원래 출처>'. 다시 실행하면 이전 추정을 되돌린 뒤 다시 계산한다.
    """
    num, (lo, hi) = analysis._num, BUYBACK_PRICE_RANGE
    bars = {}
    for r in _load_csv(DATA_DIR / "analysis/edgar/주가_월별.csv"):
        if r["VWAP"] and r["거래량"]:
            bars.setdefault(r["티커"], {})[r["월"]] = (float(r["VWAP"]), float(r["거래량"]))

    specs = {  # 종류: (금액 열, 주식수 열, 출처 열, 개월, 최소 개월, 인접 범위, 기간 → 순번)
        "연간": ("자사주매입", "자사주매입주식수", "자사주매입주식수_출처", 12, 8, 2, lambda r: int(r["연도"])),
        "분기": ("자사주매입(분기)", "자사주매입주식수(분기)", "자사주매입주식수(분기)_출처", 3, 2, 4,
                 lambda r: int(r["기간"][:4]) * 4 + int(r["기간"][-1]) - 1),
    }
    for kind, (amount_col, shares_col, src_col, months, min_months, near, index) in specs.items():
        totals = Counter()
        for path in _letter_paths(kind):
            rows = _load_csv(path)
            for r in rows:
                if r.get(src_col) is None:
                    r[src_col] = "companyfacts" if r[shares_col] != "" else ""
                if r[src_col].startswith(PRICE_ESTIMATE):  # 이전 실행 되돌리기
                    r[src_col] = r[src_col].partition(" / 이전: ")[2]
                    r[shares_col] = 0 if r[src_col] == "companyfacts" else ""

            price = {}  # (티커, 순번) → 실제 평균 매입가 (비교용)
            for r in rows:
                a, s = num(r[amount_col]), num(r[shares_col])
                if (a and a > 0 and s and s > 0 and lo <= a / s <= hi
                        and not r[src_col].startswith(("추정", "제외"))):
                    price[(r["티커"], index(r))] = a / s

            # 분기는 주가가 없으면(2016년 이전 등) 같은 회계연도의 연간 실제 평균 매입가로 나눈다
            annual_price = {}
            if kind == "분기":
                for ar in _load_csv(path.with_name(path.name.replace("_분기", "_연간"))):
                    a2, s2 = num(ar["자사주매입"]), num(ar["자사주매입주식수"])
                    if (a2 and a2 > 0 and s2 and s2 > 0 and lo <= a2 / s2 <= hi
                            and not (ar.get("자사주매입주식수_출처") or "").startswith(("추정", "제외", PRICE_ESTIMATE))):
                        annual_price[(ar["티커"], int(ar["연도"]))] = a2 / s2

            stats = Counter()
            for r in rows:
                a, s, source = num(r[amount_col]), num(r[shares_col]), r[src_col]
                if not (a and a > 0) or not (s is None and (source == "" or source.startswith("제외"))
                                             or s == 0 and source == "companyfacts"):
                    continue
                stats["대상"] += 1
                vwap, n = (analysis.period_vwap(bars[r["티커"]], date.fromisoformat(r["종료일"]), months, min_months)
                           if r["티커"] in bars and r["종료일"] else (None, 0))
                label = f"{PRICE_ESTIMATE}: VWAP ${vwap:.2f}, {n}개월" if vwap else ""
                if vwap is None and (r["티커"], int(r["기간"][:4]) if kind == "분기" else 0) in annual_price:
                    vwap = annual_price[(r["티커"], int(r["기간"][:4]))]
                    label = f"{PRICE_ESTIMATE}(연간 평균 매입가): ${vwap:.2f}"
                    stats["연간 평균 매입가 사용"] += 1
                if vwap is None:
                    stats["주가 없음"] += 1
                    continue
                shares = round(a / vwap)
                outstanding = num(r["발행주식수"])
                i = index(r)
                nearby = [price[(r["티커"], i + k)] for k in range(-near, near + 1) if k and (r["티커"], i + k) in price]
                if outstanding and shares > outstanding * max_share_ratio:
                    stats["발행주식수 대비 과다"] += 1
                elif nearby and not 1 / 3 <= vwap / statistics.median(nearby) <= 3:
                    stats["인접 매입가와 불일치"] += 1
                else:
                    r[shares_col] = shares
                    r[src_col] = f"{label} / 이전: {source}"
                    stats["채움"] += 1

            if kind == "연간":
                _save_edgar_annual(path, rows)
            else:
                _save_edgar_quarterly(path, rows)
            totals += stats
            print(f"{path.stem}: " + ", ".join(f"{k} {v}" for k, v in stats.items()), flush=True)
        print(f"{kind} 합계: " + ", ".join(f"{k} {v:,}" for k, v in totals.items()), flush=True)


def check_edgar_buyback_price(max_gap: float = 3) -> None:
    """EDGAR 글자별 결과(연간·분기)의 자사주매입 주식수를 그 기간 평균 주가와 비교해, 평균 매입가가 max_gap배 넘게 어긋나면 비운다.

    평균 매입가 = 매입 금액 ÷ 주식수. 평균 주가 = Alpaca 월봉 VWAP을 분할 반영(지금 기준)으로 바꾼 거래량 가중 평균
    (연간 12개월 중 8개월 이상, 분기 3개월 중 2개월 이상). 회사가 나중 공시에서 주식수를 분할 반영 값으로 고쳐 적는 경우가 있어,
    기간 시작 이후 지금까지의 분할배율(원래 ÷ 분할 반영 종가) 중 하나라도 맞으면 맞는 값으로 본다 (AAPL 2020: 기간 중 4:1 분할).
    대상: companyfacts·XBRL 원문·자기주식 잔액 추정 값 (주가 추정·제외 칸은 빼고). 주가가 없는 기간(2016년 이전 등)은 그대로.
    비운 칸 출처: '제외: <원래 출처> <주식수> (평균 매입가 $x, 평균 주가 $y)' → estimate-edgar-buyback이 다시 채울 수 있음.
    """
    num = analysis._num
    adj_bars, factor = {}, {}
    for r in _load_csv(DATA_DIR / "analysis/edgar/주가_월별.csv"):
        close, adjusted = num(r["종가"]), num(r["종가(분할반영)"])
        if not (close and adjusted and close > 0 and adjusted > 0):
            continue
        f = close / adjusted
        factor.setdefault(r["티커"], {})[r["월"]] = f
        if num(r["VWAP"]) and num(r["거래량"]):
            adj_bars.setdefault(r["티커"], {})[r["월"]] = (num(r["VWAP"]) / f, num(r["거래량"]) * f)

    specs = {  # 종류: (금액 열, 주식수 열, 출처 열, 개월, 최소 개월)
        "연간": ("자사주매입", "자사주매입주식수", "자사주매입주식수_출처", 12, 8),
        "분기": ("자사주매입(분기)", "자사주매입주식수(분기)", "자사주매입주식수(분기)_출처", 3, 2),
    }
    for kind, (amount_col, shares_col, src_col, months, min_months) in specs.items():
        totals = Counter()
        for path in _letter_paths(kind):
            rows, stats = _load_csv(path), Counter()
            for r in rows:
                a, sh = num(r[amount_col]), num(r[shares_col])
                source = r.get(src_col) or ("companyfacts" if r[shares_col] != "" else "")
                if not (a and a > 0 and sh and sh > 0) or source.startswith((PRICE_ESTIMATE, "제외")) or not r["종료일"]:
                    continue
                end = date.fromisoformat(r["종료일"])
                vwap, _ = analysis.period_vwap(adj_bars.get(r["티커"], {}), end, months, min_months)
                if vwap is None:
                    stats["주가 없음"] += 1
                    continue
                start = end - timedelta(days=months * 31)
                fs = [f for m, f in factor.get(r["티커"], {}).items() if m >= f"{start.year}-{start.month:02d}"] or [1.0]
                paid = a / sh
                best = min(fs, key=lambda f: abs(math.log(paid / (vwap * f))))
                if 1 / max_gap <= paid / (vwap * best) <= max_gap:
                    stats["맞음"] += 1
                    continue
                r[shares_col] = ""
                r[src_col] = f"제외: {source} {sh:g} (평균 매입가 ${paid:,.2f}, 평균 주가 ${vwap * best:,.2f})"
                stats["비움"] += 1
            if stats["비움"]:
                (_save_edgar_annual if kind == "연간" else _save_edgar_quarterly)(path, rows)
            totals += stats
            print(f"{path.stem}: " + ", ".join(f"{k} {v}" for k, v in stats.items()), flush=True)
        print(f"{kind} 합계: " + ", ".join(f"{k} {v:,}" for k, v in totals.items()), flush=True)


def detect_splits() -> None:
    """일괄 분석 결과(분기표)의 발행주식수 변화로 주식분할·병합 후보를 찾는다. API 호출 없음."""
    def load(path):
        with path.open(encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    out_fields = ["이전기간", "이후기간", "이전주식수", "이후주식수", "종류", "배율", "실제배율"]
    dart_path = DATA_DIR / "analysis/dart/batch_상장사_분기.csv"
    if dart_path.exists():
        found = analysis.detect_splits(load(dart_path), ("종목코드", "회사명"))
        save_csv("analysis/splits_dart.csv", found, ["종목코드", "회사명"] + out_fields)

    merged = DATA_DIR / "analysis/edgar/분기.csv"
    edgar_rows = load(merged) if merged.exists() else [r for p in _letter_paths("분기") for r in load(p)]
    if edgar_rows:
        found = analysis.detect_splits(edgar_rows, ("티커", "회사명"))
        # 주식수 출처가 바뀌는 지점(표지 ↔ 가중평균)은 주식 종류 일부만 센 값과 섞여 분할이 아닐 수 있음
        source = {(r["티커"], r["기간"]): r.get("발행주식수_출처", "").split(",")[0] for r in edgar_rows}
        for f in found:
            before, after = source.get((f["티커"], f["이전기간"])), source.get((f["티커"], f["이후기간"]))
            f["주식수출처"] = before if before == after else f"{before}→{after}"
        save_csv("analysis/splits_edgar.csv", found, ["티커", "회사명"] + out_fields + ["주식수출처"])


def _split_ratio(text: str) -> float:
    """'10:1' → 10, '1:4' → 0.25, '1.5:1' → 1.5 (분할 후 주식수 ÷ 분할 전)."""
    a, b = text.split(":")
    return float(a) / float(b)


SPLIT_RATIO_TAGS = ["StockholdersEquityNoteStockSplitConversionRatio1", "StockholdersEquityNoteStockSplitConversionRatio"]


def _split_ratio_facts(ciks: list[str], workers: int = 6) -> dict:
    """주식분할 확인 자료. 반환: {CIK: {"ratios": [(날짜, 비율)], "weighted": [(종료일, 제출일, 값)]}}
    (data/cache/edgar_split_ratios.json에 저장).
    - ratios: 회사가 XBRL(us-gaap)에 적은 분할 비율 (AAPL: 2014-06-06 7, 2020-08-28 4). 병합은 0.1·10 둘 다 있다.
    - weighted: 1년 가중평균 주식수의 공시별 값. 분할이 있으면 뒤 공시가 과거 기간 값을 분할 비율만큼 소급해 고친다."""
    path = DATA_DIR / "cache/edgar_split_ratios.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [c for c in ciks if not isinstance(cache.get(c), dict)]
    if todo:
        edgar = EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))

        def one(cik):
            try:
                facts = edgar.company_facts(cik).get("facts", {}).get("us-gaap", {})
            except requests.HTTPError:
                return {"ratios": [], "weighted": []}
            ratios = sorted({(e["end"], e["val"]) for tag in SPLIT_RATIO_TAGS
                             for es in facts.get(tag, {}).get("units", {}).values() for e in es if e.get("val")})
            weighted = sorted({(e["end"], e["filed"], e["val"])
                               for e in facts.get("WeightedAverageNumberOfSharesOutstandingBasic", {}).get("units", {})
                               .get("shares", []) if "start" in e and 350 <= analysis._days(e) <= 380 and e["val"] > 0})
            return {"ratios": ratios, "weighted": weighted}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            cache |= dict(zip(todo, pool.map(one, todo)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    return {c: cache.get(c, {"ratios": [], "weighted": []}) for c in ciks}


def _split_confirmed(facts: dict, after: str, ratio: float) -> str:
    """분할 탐지(주식수 기준일 after에 배율 ratio)가 확인되면 근거, 아니면 ''.
    1) 회사가 적은 분할 비율이 ±400일 안에 같은 값(±3%, 병합은 역수도) 2) 같은 과거 기간의 1년 가중평균 주식수를
    after 전 공시와 뒤 공시(3년 안)가 배율만큼(±3%) 다르게 적음 (분할은 소급 수정, 증자는 안 고침)."""
    d_after = date.fromisoformat(after)
    for d, r in facts["ratios"]:
        if abs((date.fromisoformat(d) - d_after).days) <= 400 and any(abs(v / ratio - 1) <= 0.03 for v in (r, 1 / r) if v > 0):
            return "XBRL 분할 비율"
    by_end = {}
    for end, filed, val in facts["weighted"]:
        by_end.setdefault(end, []).append((filed, val))
    for end, values in by_end.items():
        if end >= after:
            continue
        before = [v for f, v in values if f <= after]
        later = [v for f, v in values if after < f <= (d_after + timedelta(days=1100)).isoformat()]
        if any(abs(l / b / ratio - 1) <= 0.03 for b in before for l in later):
            return "과거 가중평균 소급 수정"
    return ""


def adjust_splits() -> None:
    """과거 발행주식수·주당순유동자산을 지금 주식 기준(분할 반영)으로 바꾼 열을 더한다. 원래 열은 그대로.

    한국: detect-splits 후보(splits_dart.csv)를 KRX 분기 말 종가·시가총액으로 검증. 분할·무상증자면 종가가 배율의 반대로 움직이고
      (종가 비 × 배율이 1/1.6~1.6) 시가총액은 거의 그대로(1/1.6~1.6). 유상증자·합병은 시가총액이 같이 늘어 빠진다.
      검증 결과: data/analysis/splits_dart_검증.csv. 배율 = 그 기간 뒤 검증된 분할 배율의 곱.
    미국: Alpaca 분할배율 f(월) = 원래 ÷ 분할 반영 종가 (그 달 이후 분할의 누적). 배율 = f(발행주식수_기준일이 든 달).
      2016년 이전은 주가가 없어 2016년 1월의 f × 그 사이 detect-splits 탐지 배율 중 XBRL로 확인된 것 (_split_confirmed)
      (`분할반영_범위` 열).
    대상: dart/batch_상장사_{분기,연간}.csv, edgar/{분기,연간}.csv. analyze-dart-batch·merge-edgar 뒤에 다시 실행해야 한다.
    """
    num = analysis._num

    def add_columns(rows, ratio_of, scope_of=None):
        for r in rows:
            k = ratio_of(r)
            shares, ncav = num(r.get("발행주식수")), num(r.get("주당순유동자산"))
            r["분할반영_배율"] = round(k, 6) if k is not None else ""
            r["발행주식수(분할반영)"] = round(shares * k) if shares is not None and k else ""
            r["주당순유동자산(분할반영)"] = round(ncav / k, 4) if ncav is not None and k else ""
            if scope_of:
                r["분할반영_범위"] = scope_of(r) if k is not None else ""
        return rows

    def save(path, rows):
        extra = ["분할반영_배율", "발행주식수(분할반영)", "주당순유동자산(분할반영)", "분할반영_범위"]
        fields = [k for k in rows[0] if k not in extra] + [k for k in extra if k in rows[0]]
        save_csv(str(path.relative_to(DATA_DIR)).replace("\\", "/"), rows, fields)

    # ---- 한국
    dart_q, dart_a = DATA_DIR / "analysis/dart/batch_상장사_분기.csv", DATA_DIR / "analysis/dart/batch_상장사_연간.csv"
    cands = _load_csv(DATA_DIR / "analysis/splits_dart.csv")
    krx = KrxClient(os.getenv("KRX_API_KEY", ""), cache_dir=DATA_DIR / "cache/krx")
    market = {}

    def at(period):
        if period not in market:
            y, q = int(period[:4]), int(period[-1])
            market[period] = krx.market_data(date(y, *QUARTER_ENDS[q]))[1]
        return market[period]

    verified, checked = {}, []
    for c in cands:
        ratio = _split_ratio(c["배율"])
        before, after = at(c["이전기간"]).get(c["종목코드"]), at(c["이후기간"]).get(c["종목코드"])
        if not before or not after or not before["close"] or not before["mktcap"]:
            verdict = "주가 없음"
        else:
            price_fit = after["close"] / before["close"] * ratio
            cap_fit = after["mktcap"] / before["mktcap"]
            ok = 1 / 1.6 <= price_fit <= 1.6 and 1 / 1.6 <= cap_fit <= 1.6
            verdict = "분할" if ok else "아님"
            c |= {"종가비×배율": round(price_fit, 3), "시가총액비": round(cap_fit, 3)}
            if ok:
                verified.setdefault(c["종목코드"], []).append((c["이후기간"], ratio))
        c["판정"] = verdict
        checked.append(c)
    save_csv("analysis/splits_dart_검증.csv", checked, list(cands[0]) + ["종가비×배율", "시가총액비", "판정"])

    def dart_ratio(period_key):
        def f(r):
            p = r[period_key] if period_key == "기간" else f"{r['연도']}Q4"
            k = 1.0
            for after, ratio in verified.get(r["종목코드"], []):
                if after > p:
                    k *= ratio
            return k
        return f

    for path, key in ((dart_q, "기간"), (dart_a, "연도")):
        if path.exists():
            save(path, add_columns(_load_csv(path), dart_ratio(key)))
    print(f"한국 분할 후보 {len(cands)}건 → 분할 {sum(len(v) for v in verified.values())}건 ({len(verified)}개 회사), "
          f"아님 {sum(c['판정'] == '아님' for c in checked)}, 주가 없음 {sum(c['판정'] == '주가 없음' for c in checked)}")

    # ---- 미국
    factor = {}
    for r in _load_csv(DATA_DIR / "analysis/edgar/주가_월별.csv"):
        close, adjusted = num(r["종가"]), num(r["종가(분할반영)"])
        if close and adjusted and close > 0 and adjusted > 0:
            factor.setdefault(r["티커"], {})[r["월"]] = close / adjusted
    first = {t: fs[min(fs)] for t, fs in factor.items()}
    eq, ea = DATA_DIR / "analysis/edgar/분기.csv", DATA_DIR / "analysis/edgar/연간.csv"
    q_rows = _load_csv(eq)
    basis = {(r["티커"], r["기간"]): r.get("발행주식수_기준일") or r["종료일"] for r in q_rows}
    # 2016년 이전(주가 없음)은 detect-splits 탐지값 중 회사가 XBRL에 적은 분할 비율로 확인된 것만 쓴다
    # (주식수가 정수배로 바뀐 지점은 증자·합병일 수도 있음. 확인: 같은 비율(±3%, 병합은 역수도)이 ±400일 안에 있음)
    cik_of = {r["티커"]: r["CIK"] for r in q_rows if r.get("CIK")}
    candidates = []
    for c in _load_csv(DATA_DIR / "analysis/splits_edgar.csv"):
        after = basis.get((c["티커"], c["이후기간"]), "")
        fs = factor.get(c["티커"])
        if after and "→" not in c.get("주식수출처", "") and (not fs or after[:7] < min(fs)):
            candidates.append((c["티커"], after, _split_ratio(c["배율"])))
    disclosed = _split_ratio_facts(sorted({cik_of[t] for t, _, _ in candidates if t in cik_of}))
    early, how = {}, Counter()
    for t, after, ratio in candidates:
        reason = _split_confirmed(disclosed.get(cik_of.get(t), {"ratios": [], "weighted": []}), after, ratio)
        how[reason or "확인 안 됨(제외)"] += 1
        if reason:
            early.setdefault(t, []).append((after, ratio))
    print(f"미국 2016년 이전 분할 탐지 {len(candidates)}건: " + ", ".join(f"{k} {v}" for k, v in how.most_common()))

    def us_date(r):
        return (basis.get((r["티커"], r["기간"])) if "기간" in r else basis.get((r["티커"], f"{r['연도']}Q4"))) or r.get("종료일") or ""

    def us_ratio(r):
        d, fs = us_date(r), factor.get(r["티커"])
        if not d or not fs:
            return None
        if d[:7] >= min(fs):
            return fs.get(d[:7])
        k = first[r["티커"]]
        for after, ratio in early.get(r["티커"], []):
            if after > d:
                k *= ratio
        return k

    def us_scope(r):
        fs = factor.get(r["티커"], {})
        if not fs or us_date(r)[:7] >= min(fs):
            return "전체"
        return "2016년 이전은 XBRL로 확인한 탐지값" if early.get(r["티커"]) else "2016년 이후 분할만"

    save(eq, add_columns(q_rows, us_ratio, us_scope))
    a_rows = _load_csv(ea)
    save(ea, add_columns(a_rows, us_ratio, us_scope))
    changed = sum(1 for r in q_rows if r["분할반영_배율"] not in ("", 1, 1.0))
    print(f"미국 분기 {len(q_rows):,}행 중 배율 1이 아닌 행 {changed:,}, 배율 없음(주가 없는 티커) "
          f"{sum(1 for r in q_rows if r['분할반영_배율'] == ''):,}")


def screen_dart(args: list[str]) -> None:
    """넷넷 스크리너. 옵션:
    [연도|분기]          기본: 연간 일괄 결과의 최신 연도. 2026Q2처럼 쓰면 분기 결과로 (순이익은 최근 4분기 합)
    --quarterly          분기 결과의 최신 분기로
    --ratio R            시가총액 ÷ (유동자산 - 총부채) 상한 (기본 0.667 = 그레이엄 2/3)
    --profit A,B         허용할 순이익 상태 (기본 흑자,흑자전환)
    --profit-years N     순이익 상태를 볼 기간 3/5/10 (기본 3)
    --no-profit          순이익 상태 조건 끄기
    --no-buyback         자사주매입 주식수 전년 대비 증가 조건 끄기
                         (분기 기준일 때는 그 분기 이전 최신 연간 자사주매입으로 판단)
    --price-date YYYYMMDD  주가 기준일 (기본: 해당 연도·분기 말일, 휴장이면 직전 거래일)
    """
    def opt(name, default):
        return args[args.index(name) + 1] if name in args else default

    def load(kind):
        path = DATA_DIR / f"analysis/dart/batch_상장사_{kind}.csv"
        if not path.exists():
            sys.exit(f"{path} 가 없습니다 → 먼저 analyze-dart-batch 를 실행하세요")
        with path.open(encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def latest(rows, key):
        """자료가 충분한(가장 많은 기간의 절반 이상) 최신 기간. 결산월이 12월이 아닌 몇십 개 회사만 있는 기간은 건너뜀."""
        counts = Counter(r[key] for r in rows if r["주당순유동자산"])
        return max(p for p, n in counts.items() if n >= max(counts.values()) / 2)

    annual = load("연간")
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    quarterly = "--quarterly" in args or (positional and "Q" in positional[0].upper())
    if quarterly:
        rows = load("분기")
        period = positional[0].upper() if positional else latest(rows, "기간")
        year, q = int(period[:4]), int(period[-1])
        month, day = QUARTER_ENDS[q]
        # 분기 자사주매입은 수집하지 않으므로 그 분기보다 앞서 끝난 최신 연간 값으로 판단
        buyback_year = max(int(r["연도"]) for r in annual if r["자사주매입주식수"] and int(r["연도"]) < year + (q == 4))
        buyback = analysis.annual_buyback_pairs(annual, buyback_year)
    else:
        rows, buyback, buyback_year = annual, None, None
        period = year = int(positional[0] if positional else latest(annual, "연도"))
        month, day = 12, 31
    max_ratio = float(opt("--ratio", 2 / 3))
    profit_status = None if "--no-profit" in args else set(opt("--profit", "흑자,흑자전환").split(","))
    profit_years = int(opt("--profit-years", 3))
    price_date = opt("--price-date", f"{year}{month:02d}{day:02d}")

    krx = KrxClient(os.getenv("KRX_API_KEY", ""), cache_dir=DATA_DIR / "cache/krx")
    bas_dd, market = krx.market_data(date(int(price_date[:4]), int(price_date[4:6]), int(price_date[6:])))
    print(f"재무 {period}{'' if quarterly else '년'} / 주가 기준일 {bas_dd}"
          + (f" / 자사주매입 {buyback_year}년 vs {buyback_year - 1}년" if buyback_year else ""))

    passed, stages = analysis.screen_net_net(rows, period, market, max_ratio, profit_years, profit_status,
                                             buyback_up="--no-buyback" not in args, buyback=buyback)
    for name, count in stages.items():
        print(f"  {name}: {count}")
    name = f"analysis/dart/screen_{period}_{bas_dd}{screen_tag(args)}.csv"
    save_csv(name, passed, list(passed[0]) if passed else ["종목코드"])
    return DATA_DIR / name


# 값 없이 쓰는 옵션. 나머지 --옵션은 다음 인자가 값
FLAG_OPTIONS = {"--no-profit", "--no-buyback", "--no-foreign", "--quarterly"}


def screen_tag(args: list[str]) -> str:
    """스크리너 결과 파일 이름에 붙일 조건 (기본 조건이면 빈 문자열). 예: --no-profit --no-buyback → '_no-buyback_no-profit'.
    조건만 바꿔 다시 실행해도 다른 조건의 결과 파일을 덮어쓰지 않게 한다. 기간·기준일을 정하는 옵션(--quarterly, --price-date)은 뺀다."""
    parts = []
    for i, a in enumerate(args):
        if not a.startswith("--") or a in ("--quarterly", "--price-date"):
            continue
        parts.append(a[2:] if a in FLAG_OPTIONS else f"{a[2:]}{args[i + 1]}")
    return "".join(f"_{p}" for p in sorted(parts))


# 보통주가 아닌 티커: 나스닥 5글자 W(워런트)·U(유닛)·R(권리), NYSE -WT·-U 등. 가격이 보통주와 달라 시가총액이 틀어진다
NON_COMMON_TICKER = re.compile(r"^[A-Z]{4}[WUR]$|-(WT|WS|U|UN|R|RT)$")
PREFERRED_TICKER = re.compile(r"-P[A-Z]?$")  # 우선주 티커 (JPM-PC 등)


def _cover_classes(edgar: EdgarClient, cik: str, end: str) -> list[dict]:
    """보고기간 종료일이 end인 10-Q·10-K 표지의 주식 종류별 발행주식수·티커·기준일 (data/cache/edgar_xbrl/cover2_<CIK>_<종료일>.json)."""
    cache = DATA_DIR / "cache/edgar_xbrl" / f"cover2_{int(cik)}_{end}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    filings = [f for f in edgar.filings(cik, ("10-Q", "10-K", "10-Q/A", "10-K/A"), include_older=True) if f["reportDate"] == end]
    filings.sort(key=lambda f: (f["form"].endswith("/A"), f["filingDate"]))  # 원본 공시 먼저
    classes = []
    for f in filings[:1]:
        try:
            classes = analysis.xbrl_cover_classes(edgar.filing_instance(cik, f["accessionNumber"]))
        except (FileNotFoundError, SyntaxError):
            pass
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(classes, ensure_ascii=False), encoding="utf-8")
    return classes


def _convertible_preferred(edgar: EdgarClient, cik: str, end: str) -> float | None:
    """보고기간 종료일이 end인 10-Q·10-K의 전환우선주 보통주 환산 주식수 (data/cache/edgar_xbrl/conv_<CIK>_<종료일>.json)."""
    cache = DATA_DIR / "cache/edgar_xbrl" / f"conv_{int(cik)}_{end}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))["shares"]
    filings = [f for f in edgar.filings(cik, ("10-Q", "10-K", "10-Q/A", "10-K/A"), include_older=True) if f["reportDate"] == end]
    filings.sort(key=lambda f: (f["form"].endswith("/A"), f["filingDate"]))
    shares = None
    for f in filings[:1]:
        try:
            shares = analysis.xbrl_convertible_preferred(edgar.filing_instance(cik, f["accessionNumber"]), end)
        except (FileNotFoundError, SyntaxError):
            pass
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"shares": shares}), encoding="utf-8")
    return shares


def _class_market_caps(edgar: EdgarClient, latest: dict, market: dict, close: dict, others: dict, month: str) -> dict:
    """그 분기 10-Q·10-K 표지 XBRL의 종류별 주식수로 시가총액을 다시 계산한다. 대상 (순유동자산이 양수인 회사만):
    - 주식 종류가 여럿인 회사(같은 CIK에 보통주 티커가 둘 이상): 종류별 주식수 × 종류별 종가.
      대표 티커 가격 × 전체 주식수는 종류끼리 가격이 다르면 틀린다 (HEI-A는 HEI보다 싸고, BRK-A는 BRK-B의 1,500배).
    - 주식수를 이전 분기에서 이어 쓴 회사: 표지를 Class A/B로 나눠 적으면 companyfacts에 안 잡혀 옛 값이 이어진다
      (CTNT 10-K 3,618만 주 → 10-Q 표지 A 296만 + B 20만 주). 표지 종류별 합 × 대표 티커 종가.
    티커가 없는 종류(비상장)는 대표 티커 가격으로 본다.
    market을 고쳐 쓰고, 고친 회사의 latest 행에 발행주식수_기준일(표지 날짜)을 넣는다. 반환: {티커: 계산 설명}
    """
    num = analysis._num
    targets = {}
    for t, r in latest.items():
        multi = len(others.get(r["CIK"], [])) >= 2
        carried = "분기 전" in (r.get("발행주식수_출처") or "")
        if t not in market or not (multi or carried):
            continue
        ca, tl = num(r["유동자산"]), num(r["총부채"])
        if ca is None or tl is None or ca - tl - (num(r.get("우선주")) or 0) - max(num(r.get("비지배지분")) or 0, 0) <= 0:
            continue
        # 표지에 우선주까지 적는 회사가 있음 (FBIO의 FBIOP) → 보통주 종류만
        classes = [c for c in _cover_classes(edgar, r["CIK"], r["종료일"]) if "Preferred" not in c["member"]
                   and not (c["symbol"] and (c["symbol"].endswith("P") and c["symbol"][:-1] == t
                                             or PREFERRED_TICKER.search(c["symbol"]) or NON_COMMON_TICKER.search(c["symbol"])))]
        if len(classes) >= (2 if not carried else 1) and all(c["shares"] > 0 for c in classes):
            targets[t] = classes

    sec_ticker = {o.replace("-", ""): o for os_ in others.values() for o in os_ if "-" in o}

    def ticker(symbol):  # 표지 티커 BRK.B, HEI A, BFB → SEC 형식 BRK-B, HEI-A, BF-B
        if not symbol:
            return None
        symbol = symbol.replace(".", "-").replace(" ", "-")
        return sec_ticker.get(symbol, symbol)

    missing = sorted({ticker(c["symbol"]) for cs in targets.values() for c in cs
                      if c["symbol"] and ticker(c["symbol"]) not in close})
    if missing:
        alpaca = AlpacaClient(os.getenv("ALPACA_KEY_ID", ""), os.getenv("ALPACA_SECRET_KEY", ""),
                              cache_dir=DATA_DIR / "cache/alpaca")
        year, mon = int(month[:4]), int(month[5:7])
        last_day = (date(year + mon // 12, mon % 12 + 1, 1) - timedelta(days=1)).isoformat()
        bars = _alpaca_bars(alpaca, [m.replace("-", ".") for m in missing], f"{month}-01", last_day, "raw", [])
        close = {**close, **{s.replace(".", "-"): b[-1]["c"] for s, b in bars.items() if b}}
    notes = {}
    for t, classes in targets.items():
        parts, mktcap = [], 0
        for c in classes:
            s = ticker(c["symbol"])
            price = close.get(s) if s else None
            if not s:  # 티커가 하나도 안 적힌 표지는 비상장인지 알 수 없음
                kind = "비상장" if any(x["symbol"] for x in classes) else "티커 미표기"
                label = f"{c['member'] or '차원 없음'}({kind}→{t} 가격)"
            elif price is None:
                label = f"{s}(가격 없음→{t} 가격)"
            else:
                label = s
            price = close[t] if price is None else price
            mktcap += c["shares"] * price
            parts.append(f"{label} {c['shares']:,.0f}주×${price:,.2f}")
        market[t] = {"close": close[t], "shares": sum(c["shares"] for c in classes), "mktcap": mktcap}
        cover_date = max(c["date"] for c in classes)
        notes[t] = f"표지 {cover_date}: " + " + ".join(parts)
        latest[t]["발행주식수_기준일"] = cover_date
    return notes


def _manual_shares(latest: dict, market: dict, close: dict, as_of: date) -> dict:
    """manual_shares.csv(공시 본문으로 확인한 발행주식수)가 자동 값보다 최신이고 기준일 이전이면 그 값을 쓴다 (market을 고쳐 씀).

    10-Q 표지 뒤에 8-K로만 알려진 대량 발행·병합(CTNT: 8월 ATM으로 병합 전 1.97억 주 발행 후 9/28 1:150 병합)은
    자동 수집으로 못 잡는다. 시가총액 = 수동 주식수 × 대표 티커 종가. 고친 회사의 latest 행에 발행주식수_기준일을 넣는다.
    반환: {티커: 설명}
    """
    path = DATA_DIR.parent / "manual_shares.csv"
    if not path.exists():
        return {}
    notes = {}
    for m in _load_csv(path):
        t, d = m["티커"], m["기준일"]
        r = latest.get(t)
        if not m.get("발행주식수"):  # 전환우선주 환산만 적은 줄 (screen-edgar 출력 때 _manual_convertible이 씀)
            continue
        if not r or t not in market or d > as_of.isoformat() or d <= (r.get("발행주식수_기준일") or ""):
            continue
        shares = float(m["발행주식수"])
        market[t] = {"close": close[t], "shares": shares, "mktcap": shares * close[t]}
        r["발행주식수_기준일"] = d
        notes[t] = f"수동 {d}: {shares:,.0f}주×${close[t]:,.2f} ({m['출처']})"
    return notes


def _manual_convertible(as_of: date) -> dict:
    """manual_shares.csv의 전환우선주 보통주 환산 주식수. 반환: {티커: (기준일, 주식수, 출처)} (기준월 말 이전 중 최신)."""
    path = DATA_DIR.parent / "manual_shares.csv"
    out = {}
    for m in (_load_csv(path) if path.exists() else []):
        if m.get("전환우선주_보통주환산") and m["기준일"] <= as_of.isoformat():
            if m["티커"] not in out or m["기준일"] > out[m["티커"]][0]:
                out[m["티커"]] = (m["기준일"], float(m["전환우선주_보통주환산"]), m["출처"])
    return out


def _ads_ratio(edgar: EdgarClient, cik: str, ticker: str, as_of: date) -> dict:
    """기준일 전에 낸 최신 20-F·40-F 표지의 ADS 비율과 종류별 발행주식수 {ratio, title, classes, accn}
    (data/cache/edgar_xbrl/ads_<접수번호>_<티커>.json). companyfacts에 표지 주식수가 없는 해외 회사가 많아서 같이 읽는다."""
    forms = ("20-F", "40-F", "20-F/A", "40-F/A")
    filings = [f for f in edgar.filings(cik, forms, include_older=True) if f["isXBRL"] and f["filingDate"] <= as_of.isoformat()]
    filings.sort(key=lambda f: (f["filingDate"], not f["form"].endswith("/A")), reverse=True)  # 최신, 같은 날이면 원본
    for f in filings[:2]:
        cache = DATA_DIR / "cache/edgar_xbrl" / f"ads_{f['accessionNumber']}_{ticker}.json"
        found = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        if "classes" not in found:
            try:
                xml = edgar.filing_instance(cik, f["accessionNumber"])
                found = {**analysis.xbrl_ads_ratio(xml, ticker), "classes": analysis.xbrl_cover_classes(xml)}
            except (FileNotFoundError, SyntaxError):
                found = {"ratio": None, "title": None, "classes": []}
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        if found["title"]:
            return {**found, "accn": f["accessionNumber"], "filed": f["filingDate"]}
    return {"ratio": None, "title": None, "classes": [], "accn": None}


def _apply_ads(edgar: EdgarClient, latest: dict, market: dict, foreign: set, as_of: date, manual_shares=(),
               factor=None) -> dict:
    """해외 기업의 발행주식수(원주)를 상장 증권(ADS) 단위로 바꾼다 (market을 고쳐 씀). 순유동자산이 양수인 회사만.

    먼저 최신 20-F·40-F 표지 XBRL의 종류별 주식수(우선주 제외 합)가 분기표 값보다 새것이거나 분기표 값이 가중평균이면 그 값을 쓴다.
    (companyfacts에 표지 주식수가 없는 회사가 많고, 가중평균을 천 주 단위로 잘못 적은 회사가 있다: EH 146,665 = 1억 4,667만 주)

    ADS 1주 = 원주 N주면 ADS 수 = 원주 ÷ N. 비율은 20-F·40-F 표지 증권 명칭에서 읽고 (analysis.ads_ratio_from_title),
    manual_shares.csv의 'ADS비율' 열이 있으면 그 값. 못 읽으면 1로 두고 '미확인'.
    반환: {티커: {"ratio": 쓴 비율, "note": 설명}}
    """
    num = analysis._num
    factor = factor or {}  # (티커, 월) → Alpaca 분할배율
    path = DATA_DIR.parent / "manual_shares.csv"
    manual = {m["티커"]: (float(m["ADS비율"]), m["출처"]) for m in (_load_csv(path) if path.exists() else [])
              if m.get("ADS비율") and m["기준일"] <= as_of.isoformat()}
    targets = []
    for t in foreign:
        r = latest.get(t)
        if t not in market or not r:
            continue
        ca, tl = num(r["유동자산"]), num(r["총부채"])
        if ca is not None and tl is not None and ca - tl - (num(r.get("우선주")) or 0) - max(num(r.get("비지배지분")) or 0, 0) > 0:
            targets.append(t)
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(zip(targets, pool.map(lambda t: _ads_ratio(edgar, latest[t]["CIK"], t, as_of), targets)))
    out = {}
    for t in targets:
        f, r = found[t], latest[t]
        classes = [c for c in f.get("classes", []) if "Preferred" not in c["member"] and c["shares"] > 0]
        # 종류별 값과 그 합계를 같이 적은 표지(LX: A주 + B주 + CommonStockMember 합계)는 합계를 뺀다
        classes = [c for c in classes if len(classes) < 3 or
                   abs(c["shares"] - sum(o["shares"] for o in classes if o is not c)) > 0.01 * c["shares"]]
        cover_date = max((c["date"] for c in classes if c["date"]), default="")
        # 같은 연간 공시라도 companyfacts에는 종류 하나만 잡히는 회사가 있어(GENI 1,000만 주 vs 표지 합 2.6억 주)
        # 표지 원문이 같은 날짜 이후면 원문 합을 쓴다
        # 가중평균은 기준일이 제출일이라 표지 날짜(기간 말)보다 늦지만, 기간 말 실제 주식수가 더 정확하다
        # (기간 중 대량 발행을 덜 반영: TC 가중평균 5.7억 vs 표지 83억 주) → 표지 날짜가 그 기간 말 이후면 표지 값
        weighted = (r.get("발행주식수_출처") or "").startswith("가중평균") and cover_date >= r["종료일"]
        if (classes and t not in manual_shares and cover_date <= as_of.isoformat()
                and (cover_date >= (r.get("발행주식수_기준일") or "") or weighted)):
            shares = sum(c["shares"] for c in classes)
            market[t] = {**market[t], "shares": shares, "mktcap": shares * market[t]["close"]}
            r["발행주식수_출처"], r["발행주식수_기준일"], r["_공시제출일"] = "연간 공시 표지", cover_date, f.get("filed")
        # 표지 날짜와 공시 제출일 사이에 병합·분할이 있으면 표지 주식수가 이미 그 뒤 기준으로 고쳐 적혀 있을 수 있다
        # (LHSW: 3/31 표지 331만 주, 6월 1:16 병합, 8/13 제출 → 331만 주는 병합 후 기준). 가중평균(소급 수정이 원칙)과
        # 1/3~3배 안이면 고쳐 적은 값으로 보고 기준일을 제출일로 → 그 사이 병합을 두 번 반영하지 않는다
        d, filed = r.get("발행주식수_기준일") or "", r.get("_공시제출일") or r.get("제출일") or ""
        f_d, f_filed = factor.get((t, d[:7])), factor.get((t, filed[:7]))
        weighted_shares = num(r.get("가중평균주식수"))
        if (d and filed > d and f_d and f_filed and abs(f_d / f_filed - 1) > 0.01 and weighted_shares
                and r.get("발행주식수_출처") in ("표지", "연간 공시 표지")
                and 1 / 3 <= market[t]["shares"] / weighted_shares <= 3):
            r["발행주식수_기준일"] = filed
            r["발행주식수_출처"] += " (제출 전 병합·분할 반영된 값)"
        parsed = analysis.ads_ratio_from_title(f["title"]) if f["title"] else None  # 캐시의 ratio 대신 지금 규칙으로
        if t in manual:
            ratio, note = manual[t][0], f"수동: {manual[t][1]}"
        elif parsed:
            ratio, note = parsed, f["title"] if analysis.DEPOSITARY.search(f["title"]) else ""
        else:
            ratio, note = 1.0, f"미확인: {f['title'] or '표지 증권 명칭 없음'}"
        out[t] = {"ratio": ratio, "note": note}
        if ratio != 1:
            market[t] = {**market[t], "shares": market[t]["shares"] / ratio, "mktcap": market[t]["mktcap"] / ratio}
    return out


def _split_adjust(latest: dict, market: dict, prices: list[dict], month: str) -> dict:
    """주식수 기준일 이후 ~ 기준월 말 사이의 주식분할·병합을 주식수에 반영한다 (market을 고쳐 씀).

    주식수는 분기 표지(기준일 = 표지 날짜, 보통 제출일 직전) 또는 가중평균(제출 전 분할까지 소급 반영 → 기준일 = 제출일) 값이고,
    주가는 기준월 말 원래 종가라서 그 사이 병합이 있으면 시가총액이 병합 배율만큼 틀린다 (CREG 1:10 병합 → 10배).
    Alpaca 분할배율 f = 원래 종가 ÷ 분할 반영 종가 (그 시점 이후 분할의 누적 배율). 보정 배율 = f(기준일) ÷ f(기준월 말).
    기준일이 든 달 안에 분할이 있으면(전월과 f가 다름) 일봉으로 그날의 f를 구한다.
    반환: {티커: 보정 설명}
    """
    factor = {(r["티커"], r["월"]): float(r["종가"]) / float(r["종가(분할반영)"])
              for r in prices if r["종가"] and r["종가(분할반영)"] and float(r["종가(분할반영)"]) > 0}
    alpaca = None
    notes = {}
    for t, r in latest.items():
        d = r.get("발행주식수_기준일")
        if t not in market or not d or (t, month) not in factor or (t, d[:7]) not in factor:
            continue
        f_day = factor[(t, d[:7])]
        y, m = int(d[:4]), int(d[5:7])
        prev = f"{y - (m == 1)}-{12 if m == 1 else m - 1:02d}"
        if (t, prev) in factor and abs(factor[(t, prev)] / f_day - 1) > 0.01:
            # 기준일이 든 달에 분할이 있음 → 일봉에서 기준일(휴장이면 직전 거래일)의 배율
            alpaca = alpaca or AlpacaClient(os.getenv("ALPACA_KEY_ID", ""), os.getenv("ALPACA_SECRET_KEY", ""),
                                            cache_dir=DATA_DIR / "cache/alpaca")
            symbol = t.replace("-", ".")
            last_day = (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)).isoformat()
            raw = {b["t"][:10]: b["c"] for b in alpaca.bars([symbol], f"{d[:7]}-01", last_day, "1Day", "raw").get(symbol, [])}
            split = {b["t"][:10]: b["c"] for b in alpaca.bars([symbol], f"{d[:7]}-01", last_day, "1Day", "split").get(symbol, [])}
            days = [k for k in sorted(raw) if k <= d and split.get(k)]
            f_day = raw[days[-1]] / split[days[-1]] if days else factor[(t, prev)]
        ratio = f_day / factor[(t, month)]
        if abs(ratio - 1) <= 0.01:
            continue
        market[t] = {**market[t], "shares": market[t]["shares"] * ratio, "mktcap": market[t]["mktcap"] * ratio}
        kind = "병합" if ratio < 1 else "분할"
        notes[t] = f"{d} 이후 {kind} 반영: 주식수 ×{ratio:.4g}"
    return notes


SHARE_STATEMENT = re.compile(
    r"(?:there\s+(?:were|are)\s+)?(\d{1,3}(?:,\d{3})+)\s+(?:of\s+our\s+)?(?:class\s+([ab])\s+)?(?:ordinary|common)\s+shares"
    r"(?:\s*,\s*[\w\s$.]{0,40}?,)?\s+(?:were\s+|are\s+)?(?:issued\s+and\s+)?outstanding", re.I)
AS_OF = re.compile(r"(?:[Aa]s\s+of|[Oo]n)\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})")
TWO_CLASS_STATEMENT = re.compile(  # 'X Class A ordinary shares and Y Class B ordinary shares ... outstanding'
    r"(\d{1,3}(?:,\d{3})+)\s+class\s+a\s+ordinary\s+shares[^.]{0,80}?\s+and\s+(\d{1,3}(?:,\d{3})+)\s+class\s+b\s+"
    r"ordinary\s+shares[^.]{0,80}?outstanding", re.I)
SHARE_FORMS = ("6-K", "6-K/A", "424B1", "424B3", "424B4", "424B5", "F-1", "F-1/A", "F-3", "F-3/A", "20-F", "20-F/A")


def foreign_share_check(month: str, tag: str = "", workers: int = 6) -> None:
    """screen-edgar 결과의 해외 기업 중 확인 필요 종목(표지 뒤 병합, 비율 0.01 미만, 재무 270일 초과, 가중평균 주식수)에 대해
    표지 날짜 뒤 공시(6-K·424B·F-1·F-3·20-F) 본문에서 '...ordinary shares ... outstanding' 문장과 그 근처 'as of 날짜'를 모은다.
    결과: analysis/edgar/해외_주식수_후보_<월>.csv (티커, 공시, 제출일, 기준일, 주식수, 종류, 문장). 사람이 보고 manual_shares.csv에 옮긴다.
    """
    import html as html_lib
    from datetime import datetime
    edgar = EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))
    rows = [r for r in _load_csv(DATA_DIR / f"analysis/edgar/screen_{month}{tag}.csv") if r.get("해외")]
    year, mon = int(month[:4]), int(month[5:7])
    as_of = (date(year + mon // 12, mon % 12 + 1, 1) - timedelta(days=1)).isoformat()

    def needs(r):
        merge = "×" in r["분할보정"] and float(r["분할보정"].split("×")[1]) < 0.5
        return (merge or float(r["시총÷순유동자산"]) < 0.01 or int(r["재무경과일"]) > 270
                or r["발행주식수_출처"].startswith("가중평균"))

    def one(r):
        since = r["발행주식수_기준일"] or r["종료일"]
        found = []
        filings = [f for f in edgar.filings(r["CIK"], SHARE_FORMS) if since < f["filingDate"] <= as_of]
        for f in sorted(filings, key=lambda f: f["filingDate"], reverse=True)[:12]:
            base = f"https://www.sec.gov/Archives/edgar/data/{int(r['CIK'])}/{f['accessionNumber'].replace('-', '')}"
            try:
                items = edgar._get_json(f"{base}/index.json")["directory"]["item"]
            except Exception:
                continue
            docs = [i["name"] for i in items if i["name"].lower().endswith((".htm", ".html"))
                    and not i["name"].lower().startswith("r") and int(i.get("size") or 0) < 3_000_000][:4]
            for name in docs:
                try:
                    raw = edgar._get(f"{base}/{name}", timeout=60).text
                except Exception:
                    continue
                text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", raw)))
                matches = [(m, m.group(1).replace(",", ""), (m.group(2) or "").upper()) for m in SHARE_STATEMENT.finditer(text)]
                matches += [(m, str(int(m.group(1).replace(",", "")) + int(m.group(2).replace(",", ""))), "A+B")
                            for m in TWO_CLASS_STATEMENT.finditer(text)]
                for m, shares, kind in matches:
                    window = text[max(0, m.start() - 250): m.end() + 150]
                    dates = []
                    for d in AS_OF.findall(window):
                        try:
                            dates.append(datetime.strptime(d.replace("  ", " "), "%B %d, %Y").date().isoformat())
                        except ValueError:
                            pass
                    found.append({"티커": r["티커"], "공시": f["form"], "제출일": f["filingDate"], "문서": f"{base}/{name}",
                                  "기준일": max(dates) if dates else "", "주식수": shares,
                                  "종류": kind, "문장": window.strip()[:400]})
        return found

    targets = [r for r in rows if needs(r)]
    print(f"해외 결과 {len(rows)}종목 중 확인 대상 {len(targets)}종목", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [x for xs in pool.map(one, targets) for x in xs]
    # 같은 (티커, 기준일, 주식수, 종류)는 최신 공시 하나만
    seen, out = set(), []
    for x in sorted(results, key=lambda x: (x["티커"], x["기준일"], x["제출일"]), reverse=True):
        key = (x["티커"], x["기준일"], x["주식수"], x["종류"])
        if key not in seen:
            seen.add(key)
            out.append(x)
    save_csv(f"analysis/edgar/해외_주식수_후보_{month}.csv", out, ["티커", "공시", "제출일", "기준일", "주식수", "종류", "문서", "문장"])
    print(f"주식수 문장을 찾은 종목 {len({x['티커'] for x in out})} / {len(targets)}")


# 미국 종류: SPAC / ETF / 펀드 (투자회사·신탁·리츠). SEC 업종 코드(SIC) 우선, 없으면 이름. 나머지는 일반 기업("")
US_SIC_KIND = {"6770": "SPAC", "6221": "ETF", "6722": "펀드", "6726": "펀드", "6798": "펀드"}
US_NAME_KIND = [(re.compile(r"\bAcquisition (Corp|Co|Company|Holdings)\b|\bSPAC\b", re.I), "SPAC"),
                (re.compile(r"\bETF\b|\bETN\b", re.I), "ETF"),
                (re.compile(r"\b(Fund|Trust)\b", re.I), "펀드")]


def us_kinds(rows) -> dict:
    """CIK → 종류. rows: CIK·회사명 열이 있는 행. SIC는 data/cache/edgar_sic.json에 저장해 두고 재사용."""
    cache_path = DATA_DIR / "cache/edgar_sic.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    edgar = None
    out = {}
    for r in rows:
        cik = r["CIK"]
        if cik not in cache:
            edgar = edgar or EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))
            sub = edgar.submissions(cik)
            cache[cik] = [sub.get("sic") or "", sub.get("sicDescription") or ""]
        out[cik] = US_SIC_KIND.get(cache[cik][0]) or next((k for pat, k in US_NAME_KIND if pat.search(r["회사명"])), "")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return out


FOREIGN_MAX_AGE = 550  # 해외 기업 재무 종료일이 기준일보다 이만큼(일) 넘게 오래되면 제외 (20-F 연간 + 제출기한 4개월 + 여유)


def screen_edgar(args: list[str]) -> None:
    """미국 넷넷 스크리너. EDGAR 합친 결과 + Alpaca 월말 원래 종가. 옵션:
    [YYYY-MM]            주가 기준 월 (기본: 주가 파일의 최신 월)
    --ratio, --profit, --profit-years, --no-profit, --no-buyback   screen-dart와 같음
    --lag N              기준일보다 N일 이상 앞서 끝난 분기만 사용 (기본 45, 10-Q 제출기한). 9개월보다 오래된 분기는 제외
    --annual-lag N       자사주매입 비교에 쓸 회계연도도 같은 방식 (기본 90, 10-K 제출기한)

    회사마다 회계연도가 달라서, 기준일에 이미 공시됐을 최신 분기를 회사별로 고른다.
    시가총액 = 월말 원래 종가 × 그 분기 발행주식수(표지 값, 주식 종류 합산).
    워런트·유닛·권리 티커(NON_COMMON_TICKER)는 뺀다.
    """
    def opt(name, default):
        return args[args.index(name) + 1] if name in args else default

    edgar_dir = DATA_DIR / "analysis/edgar"
    prices = _load_csv(edgar_dir / "주가_월별.csv")
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    month = positional[0] if positional else max(r["월"] for r in prices)
    year, mon = int(month[:4]), int(month[5:7])
    as_of = date(year + mon // 12, mon % 12 + 1, 1) - timedelta(days=1)  # 그 달 말일
    lag, annual_lag = int(opt("--lag", 45)), int(opt("--annual-lag", 90))
    close = {r["티커"]: float(r["종가"]) for r in prices if r["월"] == month}

    latest, start = {}, as_of - timedelta(days=lag + 270)
    for r in _load_csv(edgar_dir / "분기.csv"):
        end = r["종료일"] and date.fromisoformat(r["종료일"])
        if (end and start <= end <= as_of - timedelta(days=lag) and r["주당순유동자산"]
                and not NON_COMMON_TICKER.search(r["티커"])):
            if r["티커"] not in latest or r["종료일"] > latest[r["티커"]]["종료일"]:
                latest[r["티커"]] = r
    # 해외 기업(20-F·40-F): 연간만 내는 회사가 많아 종료일 기준 FOREIGN_MAX_AGE일까지, 기준일 전에 제출된 기간만
    foreign = set()
    foreign_path = edgar_dir / "해외_분기.csv"
    current_assets = {}  # 해외 티커 → [(종료일, 유동자산)] (단위 오류 의심 확인용)
    for r in (_load_csv(foreign_path) if foreign_path.exists() and "--no-foreign" not in args else []):
        end = date.fromisoformat(r["종료일"])
        if r["유동자산"]:
            current_assets.setdefault(r["티커"], []).append((r["종료일"], float(r["유동자산"])))
        if (as_of - timedelta(days=FOREIGN_MAX_AGE) <= end and r["제출일"] <= as_of.isoformat() and r["주당순유동자산"]
                and (r["티커"] not in latest or r["티커"] in foreign) and not NON_COMMON_TICKER.search(r["티커"])):
            if r["티커"] not in foreign or r["종료일"] > latest[r["티커"]]["종료일"]:
                latest[r["티커"]] = r
                foreign.add(r["티커"])
    # 유동자산이 이전 기간들의 중앙값보다 300배 넘게 크거나 작으면 회사가 XBRL 단위를 잘못 적었을 가능성
    # (UPC: 2024년 9월부터 1,000배. 직전 기간도 틀려 있어서 중앙값과 비교)
    unit_doubt = {}
    for t in foreign:
        cur = latest[t]["종료일"]
        prev = [v for e, v in current_assets.get(t, []) if e < cur and v > 0]
        now = next((v for e, v in current_assets.get(t, []) if e == cur), None)
        if prev and now:
            gap = now / statistics.median(prev)
            if not 1 / 300 <= gap <= 300:
                unit_doubt[t] = f"유동자산이 이전 기간 중앙값의 {gap:,.0f}배"

    annual_by = {}
    for r in _load_csv(edgar_dir / "연간.csv"):
        annual_by.setdefault(r["티커"], {})[int(r["연도"])] = r
    def buyback_shares(r):  # 매입 금액이 0으로 공시됐으면 0주 (매입 안 한 해)
        shares = analysis._num(r.get("자사주매입주식수"))
        return 0 if shares is None and analysis._num(r.get("자사주매입")) == 0 else shares

    buyback = {}
    for ticker, years in annual_by.items():
        done = [y for y, r in years.items() if r["종료일"] and date.fromisoformat(r["종료일"]) <= as_of - timedelta(days=annual_lag)]
        if done:
            y = max(done)
            buyback[ticker] = (buyback_shares(years[y]), buyback_shares(years.get(y - 1, {})))

    # screen_net_net은 종목코드·기간 열로 고르므로 회사별로 고른 분기를 같은 이름으로 맞춰 넘긴다
    rows = [{**r, "종목코드": t, "기간": "기준"} for t, r in latest.items()]
    market = {t: {"close": close[t], "shares": float(r["발행주식수"]), "mktcap": close[t] * float(r["발행주식수"])}
              for t, r in latest.items() if t in close and float(r["발행주식수"]) > 0}
    edgar = EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))
    others = {}  # CIK → 보통주 티커들 (우선주·워런트 등 제외)
    for c in edgar.tickers():
        if not PREFERRED_TICKER.search(c["ticker"]) and not NON_COMMON_TICKER.search(c["ticker"]):
            others.setdefault(c["cik"], []).append(c["ticker"])
    class_notes = _class_market_caps(edgar, latest, market, close, others, month)
    manual_notes = _manual_shares(latest, market, close, as_of)
    class_notes |= manual_notes
    split_factor = {(r["티커"], r["월"]): float(r["종가"]) / float(r["종가(분할반영)"])
                    for r in prices if r["종가"] and r["종가(분할반영)"] and float(r["종가(분할반영)"]) > 0}
    ads = _apply_ads(edgar, latest, market, foreign, as_of, manual_notes, split_factor)
    split_notes = _split_adjust(latest, market, prices, month)
    max_ratio = float(opt("--ratio", 2 / 3))
    profit_years = int(opt("--profit-years", 3))
    print(f"주가 {month} 말 / 분기 종료일 {start} ~ {as_of - timedelta(days=lag)} / "
          f"가격 있는 종목 {len(close)}, 조건에 맞는 분기가 있는 회사 {len(latest)}, "
          f"주식 종류별로 시가총액을 다시 계산한 회사 {len(class_notes) - len(manual_notes)}, 수동 주식수 {len(manual_notes)}, "
          f"분할·병합 보정 {len(split_notes)}, 해외 기업 {len(foreign)} (ADS 비율 ≠ 1: {sum(a['ratio'] != 1 for a in ads.values())}, "
          f"미확인 {sum(a['note'].startswith('미확인') for a in ads.values())})")
    passed, stages = analysis.screen_net_net(rows, "기준", market, max_ratio, profit_years,
                                             None if "--no-profit" in args else set(opt("--profit", "흑자,흑자전환").split(",")),
                                             buyback_up="--no-buyback" not in args, buyback=buyback)
    if "--no-buyback" not in args:  # ETF·펀드·신탁의 환매는 자사주매입이 아님 → 매입 증가 조건에서 뺀다
        kinds = us_kinds([{"CIK": latest[p["종목코드"]]["CIK"], "회사명": p["회사명"]} for p in passed])
        passed = [p for p in passed if kinds[latest[p["종목코드"]]["CIK"]] not in ("ETF", "펀드")]
        stages["자사주매입증가(ETF·펀드 환매 제외)"] = len(passed)
    for name, count in stages.items():
        print(f"  {name}: {count}")
    factor = {(r["티커"], r["월"]): float(r["종가"]) / float(r["종가(분할반영)"])
              for r in prices if r["종가"] and r["종가(분할반영)"] and float(r["종가(분할반영)"]) > 0}
    manual_conv = _manual_convertible(as_of)
    out = []
    for p in passed:
        r = latest[p["종목코드"]]
        t = p.pop("종목코드")
        # 전환우선주를 보통주로 바꾸면: 주식수 + 환산 주식수, 순유동자산에서 뺐던 우선주는 다시 더함 (참고용, 통과 조건은 그대로)
        conv, conv_date = _convertible_preferred(edgar, r["CIK"], r["종료일"]), r["종료일"]
        if t in manual_conv and manual_conv[t][0] >= r["종료일"]:  # 공시 본문으로 확인한 더 최신 값
            conv_date, conv = manual_conv[t][:2]
        diluted = ""
        if conv:
            f_end, f_now = factor.get((t, conv_date[:7])), factor.get((t, month))
            conv_now = conv * (f_end / f_now) if f_end and f_now else conv  # 기간 말 이후 분할·병합 반영
            shares_now = market[t]["shares"]
            diluted = round((shares_now + conv_now) * close[t] / (p["순유동자산"] + (p.get("우선주") or 0)), 3)
            conv = round(conv_now)
        out.append({"CIK": r["CIK"], "티커": t, "회사명": p.pop("회사명"),
                    "기간": r["기간"], "종료일": r["종료일"],
                    "재무경과일": (as_of - date.fromisoformat(r["종료일"])).days,  # 기준일 − 분기 종료일
                    **{k: v for k, v in p.items() if k != "기간"},
                    "발행주식수": market[t]["shares"],
                    "발행주식수_출처": ("수동" if t in manual_notes else "표지 종류별 합") if t in class_notes
                                       else r.get("발행주식수_출처", ""),
                    "발행주식수_기준일": r.get("발행주식수_기준일", ""),
                    "분할보정": split_notes.get(t, ""),
                    "시가총액_계산": class_notes.get(t, ""),
                    "전환우선주_보통주환산": conv or "", "전환후_시총÷순유동자산": diluted,
                    "같은 회사 다른 티커": " ".join(o for o in others.get(r["CIK"], []) if o != t),
                    "해외": f"{r['통화']} {r['회계기준']} 제출 {r['제출일']}" if t in foreign else "",
                    "ADS비율": ads[t]["ratio"] if t in ads else "", "ADS_문구": ads.get(t, {}).get("note", ""),
                    "단위_의심": unit_doubt.get(t, "")})
    save_csv(f"analysis/edgar/screen_{month}{screen_tag(args)}.csv", out, list(out[0]) if out else ["티커"])


QUARTER_ENDS ={1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def fill_krx_shares(results: dict, corps: dict, start: int, end: int) -> None:
    """각 분기말(휴장이면 직전 거래일) KRX 상장주식수를 DART 결과의 발행주식수로 채운다."""
    krx = KrxClient(os.getenv("KRX_API_KEY", ""), cache_dir=DATA_DIR / "cache/krx")
    today = date.today()
    for year in range(start, end + 1):
        for q, (month, day) in QUARTER_ENDS.items():
            quarter_end = date(year, month, day)
            if quarter_end >= today:
                continue
            bas_dd, shares = krx.issued_shares(quarter_end)
            for corp_code, (quarters, annual) in results.items():
                value = shares.get(corps[corp_code]["stock_code"])
                if (year, q) in quarters:
                    quarters[(year, q)]["shares"] = value
                if q == 4 and year in annual:
                    annual[year]["shares"] = value
        print(f"{year} KRX 상장주식수 완료", flush=True)


def _alpaca_bars(alpaca: AlpacaClient, symbols: list[str], start: str, end: str, adjustment: str,
                 skipped: list[str]) -> dict:
    """여러 종목 월봉. Alpaca가 모르는 종목이 섞여 요청이 거부되면 반씩 나눠 다시 받고, 끝내 안 되는 종목은 skipped에 넣는다."""
    try:
        return alpaca.bars(symbols, start, end, adjustment=adjustment)
    except RuntimeError as e:
        if len(symbols) == 1:
            skipped.append(f"{symbols[0]} ({str(e)[:80]})")
            return {}
        half = len(symbols) // 2
        return {**_alpaca_bars(alpaca, symbols[:half], start, end, adjustment, skipped),
                **_alpaca_bars(alpaca, symbols[half:], start, end, adjustment, skipped)}


def us_prices(chunk: int = 200) -> None:
    """EDGAR 합친 결과의 티커 전체에 대해 Alpaca 월봉(2016년 1월 ~ 지난달)을 받아 주가_월별.csv로 저장한다.

    종가는 분할 미반영 원래 값(공시 주식수와 곱해 시가총액 계산용)과 분할 반영 값을 같이 둔다.
    분할배율 = 원래 ÷ 분할 반영: 이후 분할이 있으면 1이 아닌 값이 되고, 분할 시점에 값이 바뀐다 (주식분할 검증용).
    """
    alpaca = AlpacaClient(os.getenv("ALPACA_KEY_ID", ""), os.getenv("ALPACA_SECRET_KEY", ""),
                          cache_dir=DATA_DIR / "cache/alpaca")
    foreign = DATA_DIR / "analysis/edgar/해외_분기.csv"
    tickers = sorted({r["티커"] for path in (DATA_DIR / "analysis/edgar/분기.csv", foreign) if path.exists()
                      for r in _load_csv(path)})
    symbol_of = {t: t.replace("-", ".") for t in tickers}  # SEC BRK-B → Alpaca BRK.B
    ticker_of = {s: t for t, s in symbol_of.items()}
    symbols = sorted(ticker_of)
    start = "2016-01-01"
    end = (date.today().replace(day=1) - timedelta(days=1)).isoformat()  # 지난달 말 (끝난 달만 캐시)

    raw, split, skipped = {}, {}, []
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        raw.update(_alpaca_bars(alpaca, part, start, end, "raw", skipped))
        split.update(_alpaca_bars(alpaca, part, start, end, "split", []))
        print(f"{min(i + chunk, len(symbols))}/{len(symbols)}개 종목, 가격 있는 종목 {len(raw)}", flush=True)
    # 여러 종목을 한 번에 받으면 Alpaca가 가끔 종목 하나를 통째로 빠뜨린다 (DOW: 단독 요청이면 있음) → 빠진 종목만 작게 다시
    for kind, got in (("raw", raw), ("split", split)):
        missing = [s for s in symbols if s not in got and (kind == "raw" or s in raw)]
        for i in range(0, len(missing), 20):
            got.update(_alpaca_bars(alpaca, missing[i:i + 20], start, end, kind, []))
        print(f"{kind}: 빠진 {len(missing)}개 다시 받음 → 가격 있는 종목 {len(got)}", flush=True)

    rows = []
    for symbol in sorted(raw):
        adjusted = {b["t"][:7]: b["c"] for b in split.get(symbol, [])}
        for b in raw[symbol]:
            month = b["t"][:7]
            rows.append({"티커": ticker_of.get(symbol, symbol), "월": month, "종가": b["c"],
                         "종가(분할반영)": adjusted.get(month),
                         "분할배율": round(b["c"] / adjusted[month], 4) if adjusted.get(month) else None,
                         "VWAP": b.get("vw"), "거래량": b.get("v")})
    save_csv("analysis/edgar/주가_월별.csv", rows, ["티커", "월", "종가", "종가(분할반영)", "분할배율", "VWAP", "거래량"])
    print(f"가격 있는 종목 {len(raw)} / {len(symbols)}, 요청 거부 {len(skipped)}개: {', '.join(skipped[:20])}")


def _listed_corps(dart: DartClient) -> list[dict]:
    """종목코드가 있는 회사 목록. 받을 때마다 data/dart/listed_corps.json에 저장해 두고,
    고유번호 API가 점검 등으로 막히면(zip 대신 오류 XML이 옴) 저장해 둔 목록을 쓴다."""
    path = DATA_DIR / "dart/listed_corps.json"
    try:
        corps = [c for c in dart.corp_codes() if c["stock_code"]]
    except zipfile.BadZipFile:
        if not path.exists():
            raise
        corps = json.loads(path.read_text(encoding="utf-8"))
        print(f"고유번호 API를 쓸 수 없어 저장해 둔 목록 사용: {path}", flush=True)
        return corps
    save("dart/listed_corps.json", corps)
    return corps


def analyze_dart_batch(dart: DartClient, start: int, end: int, buyback_years: list[int] = (),
                       quarterly_buyback_years: list[int] = ()) -> None:
    """종목코드가 있는 회사 전체의 유동자산·총부채·순이익을 통합 CSV로 저장."""
    corps = {c["corp_code"]: c for c in _listed_corps(dart)}
    print(f"대상 회사: {len(corps)}", flush=True)
    results = analysis.dart_multi_records(dart, list(corps), start, end,
                                         progress=lambda msg: print(msg, flush=True),
                                         cache_dir=DATA_DIR / "cache/dart_multi")
    if os.getenv("KRX_API_KEY"):
        fill_krx_shares(results, corps, start, end)
    else:
        print("KRX_API_KEY가 없어 발행주식수는 비워 둡니다")

    if buyback_years:
        cached = DartClient(dart.api_key, cache_dir=DATA_DIR / "cache/dart_single")
        analysis.fill_dart_annual_buyback(cached, results, buyback_years,
                                          progress=lambda msg: print(msg, flush=True))
    if quarterly_buyback_years:
        cached = DartClient(dart.api_key, cache_dir=DATA_DIR / "cache/dart_single")
        analysis.fill_dart_quarterly_buyback(cached, results, quarterly_buyback_years,
                                             progress=lambda msg: print(msg, flush=True))

    q_all, a_all = [], []
    for corp_code, (quarters, annual) in sorted(results.items(), key=lambda x: corps[x[0]]["stock_code"]):
        head = {"종목코드": corps[corp_code]["stock_code"], "회사명": corps[corp_code]["corp_name"]}
        q_all += [{**head, **row} for row in analysis.quarterly_table(quarters)]
        a_all += [{**head, **row} for row in analysis.annual_table(annual)]
    save_csv("analysis/dart/batch_상장사_분기.csv", q_all, list(q_all[0]) if q_all else [])
    save_csv("analysis/dart/batch_상장사_연간.csv", a_all, list(a_all[0]) if a_all else [])
    print(f"데이터가 있는 회사: {len(results)} / {len(corps)}")


def save_analysis(prefix: str, quarters: dict, annual: dict) -> None:
    q_rows = analysis.quarterly_table(quarters)
    a_rows = analysis.annual_table(annual)
    save_csv(f"analysis/{prefix}_분기.csv", q_rows, list(q_rows[0]) if q_rows else [])
    save_csv(f"analysis/{prefix}_연간.csv", a_rows, list(a_rows[0]) if a_rows else [])


def main(argv: list[str]) -> None:
    load_dotenv()
    if not argv:
        print(__doc__)
        return

    cmd, args = argv[0], argv[1:]

    if cmd == "analyze-dart":
        dart = DartClient(os.getenv("DART_API_KEY", ""))
        corp_code, corp_name = resolve_corp(dart, args[0])
        start, end = int(args[1]), int(args[2])
        quarters, annual = analysis.dart_records(dart, corp_code, start, end)
        save_analysis(f"dart/{corp_name}", quarters, annual)

    elif cmd == "analyze-edgar":
        edgar = EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))
        cik, ticker = resolve_ticker(edgar, " ".join(args))
        quarters, annual = analysis.edgar_records(edgar.company_facts(cik))
        save_analysis(f"edgar/{ticker}", quarters, annual)

    elif cmd.startswith("dart"):
        dart = DartClient(os.getenv("DART_API_KEY", ""))
        if cmd == "dart-corps":
            corps = dart.corp_codes()
            save_csv("dart/corp_codes.csv", corps, ["corp_code", "corp_name"])
        elif cmd == "dart-filings":
            corp_code, bgn, end = args
            save(f"dart/filings_{corp_code}_{bgn}_{end}.json", dart.filings(corp_code, bgn, end))
        elif cmd == "dart-fs":
            corp_code, year = args[:2]
            save(f"dart/fs_{corp_code}_{year}.json", dart.financial_statements(corp_code, year))
        else:
            sys.exit(f"알 수 없는 명령: {cmd}")

    elif cmd == "detect-splits":
        detect_splits()
    elif cmd == "adjust-splits":
        adjust_splits()

    elif cmd in ("fill-edgar-buyback", "fill-edgar-buyback-quarterly"):
        # --tickers-file <파일>: 공백으로 구분한 티커만 다시 찾음 (refresh-edgar로 다시 계산한 회사 등)
        only = None
        if "--tickers-file" in args:
            i = args.index("--tickers-file")
            only = set(Path(args[i + 1]).read_text(encoding="utf-8").split())
            args = args[:i] + args[i + 2:]
        letters = [a for a in args if a != "--recheck"]
        if cmd == "fill-edgar-buyback":
            fill_edgar_buyback(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), letters, recheck="--recheck" in args,
                               only=only)
        else:
            fill_edgar_buyback_quarterly(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), letters, only=only)
    elif cmd == "fix-edgar-buyback":
        fix_edgar_buyback(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))
    elif cmd == "estimate-edgar-buyback":
        estimate_edgar_buyback()
    elif cmd == "check-edgar-buyback-price":
        check_edgar_buyback_price()
    elif cmd == "refresh-edgar":
        refresh_edgar_tickers(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), args)
    elif cmd == "fill-edgar-ends":
        fill_edgar_ends(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))
    elif cmd == "fill-edgar-balance":
        fill_edgar_balance(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))
    elif cmd == "merge-edgar":
        merge_edgar(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))

    elif cmd == "us-prices":
        us_prices()

    elif cmd == "screen-edgar":
        screen_edgar(args)

    elif cmd == "screen-dart":
        screen_dart(args)

    elif cmd == "analyze-dart-batch":
        # 예: analyze-dart-batch 2015 2025 --buyback 2025 2024 --buyback-quarterly 2026 2025
        # (자사주매입은 적힌 연도 순서대로 수집. 분기는 1분기·반기·3분기 보고서, 4분기는 연간 − 3분기 누적)
        def years_after(flag):
            if flag not in args:
                return []
            rest = args[args.index(flag) + 1:]
            return [int(y) for y in rest[:next((i for i, a in enumerate(rest) if a.startswith("--")), len(rest))]]
        analyze_dart_batch(DartClient(os.getenv("DART_API_KEY", "")), int(args[0]), int(args[1]),
                           years_after("--buyback"), years_after("--buyback-quarterly"))

    elif cmd == "foreign-share-check":
        foreign_share_check(args[0], screen_tag(args[1:]))

    elif cmd == "analyze-foreign-batch":
        analyze_foreign_batch(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))

    elif cmd == "analyze-edgar-batch":
        analyze_edgar_batch(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), args[0])

    elif cmd == "edgar-tickers":
        edgar = EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))
        save_csv("edgar/tickers.csv", edgar.tickers(), ["ticker", "name", "cik"])

    elif cmd.startswith("edgar"):
        edgar = EdgarClient(os.getenv("EDGAR_USER_AGENT", ""))
        cik = edgar.cik_for_ticker(args[0])
        if cmd == "edgar-filings":
            save(f"edgar/filings_{args[0].upper()}.json", edgar.filings(cik, tuple(args[1:])))
        elif cmd == "edgar-facts":
            save(f"edgar/facts_{args[0].upper()}.json", edgar.company_facts(cik))
        else:
            sys.exit(f"알 수 없는 명령: {cmd}")

    else:
        sys.exit(f"알 수 없는 명령: {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
