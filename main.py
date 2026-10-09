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
    python main.py refresh-edgar PRTH WTBA       # 일부 회사만 companyfacts로 다시 계산해 글자별 파일에 반영
    python main.py fill-edgar-ends               # 글자별 결과에 보고기간 종료일 열 추가 (값은 그대로)
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


def _letter_paths(kind: str) -> list[Path]:
    """글자별 일괄 결과 파일. batch_AA는 A 파일과 겹치므로 제외."""
    return [p for p in sorted((DATA_DIR / "analysis/edgar").glob(f"batch_*_{kind}.csv"))
            if len(p.stem.split("_")[1]) == 1]


def refresh_edgar_tickers(edgar: EdgarClient, tickers: list[str], workers: int = 6) -> None:
    """일부 회사만 companyfacts를 다시 받아 글자별 일괄 결과(연간·분기)의 해당 행을 바꾼다.

    분석 코드를 고친 뒤 일부 회사만 다시 계산할 때 사용. 연간표의 XBRL 보충값은 지워지므로 fill-edgar-buyback을 다시 실행.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    by_letter = {}
    for ticker in (t.upper() for t in tickers):
        by_letter.setdefault(ticker[0], []).append(ticker)

    def tables(ticker):
        quarters, annual = analysis.edgar_records(edgar.company_facts(ciks[ticker]))
        return {"분기": analysis.quarterly_table(quarters), "연간": analysis.annual_table(annual)}

    for letter, group in sorted(by_letter.items()):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            computed = dict(zip(group, pool.map(tables, group)))
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


def fill_edgar_buyback_quarterly(edgar: EdgarClient, letters: list[str], workers: int = 6) -> None:
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
        jobs = [(t, ciks[t], g) for t, g in gaps.items() if t in ciks]
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

        # 연초누적·전년 동기 대비 다시 계산
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


def fill_edgar_buyback(edgar: EdgarClient, letters: list[str], workers: int = 6, recheck: bool = False) -> None:
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
        jobs = [(ticker, ciks[ticker], years) for ticker, years in gaps.items() if ticker in ciks]
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
        save_csv("analysis/splits_edgar.csv", found, ["티커", "회사명"] + out_fields)


def screen_dart(args: list[str]) -> None:
    """넷넷 스크리너. 옵션:
    [연도|분기]          기본: 연간 일괄 결과의 최신 연도. 2026Q2처럼 쓰면 분기 결과로 (순이익은 최근 4분기 합)
    --quarterly          분기 결과의 최신 분기로
    --ratio R            시가총액 ÷ (유동자산 - 총부채) 상한 (기본 0.667 = 그레이엄 2/3)
    --profit A,B         허용할 순이익 상태 (기본 흑자,흑자전환)
    --profit-years N     순이익 상태를 볼 기간 3/5/10 (기본 3)
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
    profit_status = set(opt("--profit", "흑자,흑자전환").split(","))
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
    save_csv(f"analysis/dart/screen_{period}_{bas_dd}.csv", passed, list(passed[0]) if passed else ["종목코드"])


# 보통주가 아닌 티커: 나스닥 5글자 W(워런트)·U(유닛)·R(권리), NYSE -WT·-U 등. 가격이 보통주와 달라 시가총액이 틀어진다
NON_COMMON_TICKER = re.compile(r"^[A-Z]{4}[WUR]$|-(WT|WS|U|UN|R|RT)$")


def screen_edgar(args: list[str]) -> None:
    """미국 넷넷 스크리너. EDGAR 합친 결과 + Alpaca 월말 원래 종가. 옵션:
    [YYYY-MM]            주가 기준 월 (기본: 주가 파일의 최신 월)
    --ratio, --profit, --profit-years, --no-buyback   screen-dart와 같음
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

    annual_by = {}
    for r in _load_csv(edgar_dir / "연간.csv"):
        annual_by.setdefault(r["티커"], {})[int(r["연도"])] = r
    buyback = {}
    for ticker, years in annual_by.items():
        done = [y for y, r in years.items() if r["종료일"] and date.fromisoformat(r["종료일"]) <= as_of - timedelta(days=annual_lag)]
        if done:
            y = max(done)
            buyback[ticker] = (analysis._num(years[y]["자사주매입주식수"]),
                               analysis._num(years.get(y - 1, {}).get("자사주매입주식수")))

    # screen_net_net은 종목코드·기간 열로 고르므로 회사별로 고른 분기를 같은 이름으로 맞춰 넘긴다
    rows = [{**r, "종목코드": t, "기간": "기준"} for t, r in latest.items()]
    market = {t: {"close": close[t], "shares": float(r["발행주식수"]), "mktcap": close[t] * float(r["발행주식수"])}
              for t, r in latest.items() if t in close and float(r["발행주식수"]) > 0}
    max_ratio = float(opt("--ratio", 2 / 3))
    profit_years = int(opt("--profit-years", 3))
    print(f"주가 {month} 말 / 분기 종료일 {start} ~ {as_of - timedelta(days=lag)} / "
          f"가격 있는 종목 {len(close)}, 조건에 맞는 분기가 있는 회사 {len(latest)}")
    passed, stages = analysis.screen_net_net(rows, "기준", market, max_ratio, profit_years,
                                             set(opt("--profit", "흑자,흑자전환").split(",")),
                                             buyback_up="--no-buyback" not in args, buyback=buyback)
    for name, count in stages.items():
        print(f"  {name}: {count}")
    out = []
    for p in passed:
        r = latest[p["종목코드"]]
        out.append({"CIK": r["CIK"], "티커": p.pop("종목코드"), "회사명": p.pop("회사명"),
                    "기간": r["기간"], "종료일": r["종료일"], **{k: v for k, v in p.items() if k != "기간"}})
    save_csv(f"analysis/edgar/screen_{month}.csv", out, list(out[0]) if out else ["티커"])


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
    tickers = sorted({r["티커"] for r in _load_csv(DATA_DIR / "analysis/edgar/분기.csv")})
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


def analyze_dart_batch(dart: DartClient, start: int, end: int, buyback_years: list[int] = ()) -> None:
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

    elif cmd == "fill-edgar-buyback":
        fill_edgar_buyback(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), [a for a in args if a != "--recheck"],
                           recheck="--recheck" in args)
    elif cmd == "fill-edgar-buyback-quarterly":
        fill_edgar_buyback_quarterly(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), args)
    elif cmd == "fix-edgar-buyback":
        fix_edgar_buyback(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))
    elif cmd == "refresh-edgar":
        refresh_edgar_tickers(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), args)
    elif cmd == "fill-edgar-ends":
        fill_edgar_ends(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))
    elif cmd == "merge-edgar":
        merge_edgar(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))

    elif cmd == "us-prices":
        us_prices()

    elif cmd == "screen-edgar":
        screen_edgar(args)

    elif cmd == "screen-dart":
        screen_dart(args)

    elif cmd == "analyze-dart-batch":
        # 예: analyze-dart-batch 2015 2025 --buyback 2025 2024  (자사주매입은 적힌 연도 순서대로 수집)
        buyback_years = [int(y) for y in args[args.index("--buyback") + 1:]] if "--buyback" in args else []
        analyze_dart_batch(DartClient(os.getenv("DART_API_KEY", "")), int(args[0]), int(args[1]), buyback_years)

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
