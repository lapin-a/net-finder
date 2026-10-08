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
    python main.py fix-edgar-buyback             # companyfacts 자사주매입 주식수 중 이상한 값을 XBRL 원문 값으로 교체
    python main.py refresh-edgar PRTH WTBA       # 일부 회사만 companyfacts로 다시 계산해 글자별 파일에 반영
    python main.py merge-edgar                   # 글자별 결과를 edgar/연간.csv, edgar/분기.csv로 합침

    # 넷넷 스크리너 (analyze-dart-batch 결과 + KRX 종가·시가총액)
    python main.py screen-dart                   # 최신 연도, 기본 조건
    python main.py screen-dart 2024 --ratio 1 --profit 흑자 --profit-years 5 --no-buyback
    python main.py screen-dart 2025 --price-date 20261007   # 연말 대신 해당일(직전 거래일) 주가로 비교
"""
import csv
import json
import math
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

import analysis
from collectors import DartClient, EdgarClient, KrxClient

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
    """10-K XBRL 원문에서 뽑은 자사주매입 후보 (data/cache/edgar_xbrl에 저장해 두고 재사용)."""
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
        for year, value in analysis.annual_buyback_from_xbrl(parsed).items():
            if year in remaining:
                found[year] = value
                remaining.discard(year)
        for year, value in analysis.treasury_increase_from_xbrl(parsed).items():
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


def refresh_edgar_tickers(edgar: EdgarClient, tickers: list[str]) -> None:
    """일부 회사만 companyfacts를 다시 받아 글자별 일괄 결과(연간·분기)의 해당 행을 바꾼다.

    분석 코드를 고친 뒤 일부 회사만 다시 계산할 때 사용. 연간표의 XBRL 보충값은 지워지므로 fill-edgar-buyback을 다시 실행.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    for ticker in (t.upper() for t in tickers):
        quarters, annual = analysis.edgar_records(edgar.company_facts(ciks[ticker]))
        for kind, new in (("분기", analysis.quarterly_table(quarters)), ("연간", analysis.annual_table(annual))):
            path = DATA_DIR / f"analysis/edgar/batch_{ticker[0]}_{kind}.csv"
            rows = _load_csv(path)
            at = next((i for i, r in enumerate(rows) if r["티커"] == ticker), None)
            if at is None:
                print(f"{ticker}: {path.name}에 없음")
                continue
            head = {"티커": ticker, "회사명": rows[at]["회사명"]}
            new = [{**head, **row} for row in new]
            if kind == "연간":
                for r in new:
                    r["자사주매입주식수_출처"] = "companyfacts" if r["자사주매입주식수"] not in ("", None) else ""
            rows = rows[:at] + new + [r for r in rows[at:] if r["티커"] != ticker]
            if kind == "연간":
                _save_edgar_annual(path, rows)
            else:
                save_csv(f"analysis/edgar/{path.name}", rows, list(rows[0]))


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
            if r["자사주매입주식수_출처"] != "companyfacts" or not a or a <= 0 or s is None:
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


def fill_edgar_buyback(edgar: EdgarClient, letters: list[str], workers: int = 6) -> None:
    """EDGAR 일괄 연간표에서 자사주매입 금액은 있는데 주식수가 빈 칸을 10-K XBRL 원문으로 채운다.

    10-K 자본변동표에는 3개년이 들어 있으므로 최신 10-K부터 거꾸로 내려가며, 빈 연도가 다 채워지거나
    더 볼 10-K가 없으면 멈춘다. 결과는 원래 파일에 덮어쓰고 '자사주매입주식수_출처' 열을 붙인다.
    회사 workers개를 동시에 처리하되, 요청 간격은 EdgarClient가 스레드 전체를 합쳐 지킨다.
    """
    ciks = {c["ticker"]: c["cik"] for c in edgar.tickers()}
    paths = _letter_paths("연간")
    if letters:
        paths = [p for p in paths if p.stem.split("_")[1] in {l.upper() for l in letters}]

    for path in paths:
        rows = _load_csv(path)
        gaps = {}
        for r in rows:
            if r.get("자사주매입주식수_출처") is None:
                r["자사주매입주식수_출처"] = "companyfacts" if r["자사주매입주식수"] != "" else ""
            amount = analysis._num(r["자사주매입"])
            if amount and amount > 0 and r["자사주매입주식수"] == "" and r["자사주매입주식수_출처"] == "":
                gaps.setdefault(r["티커"], set()).add(int(r["연도"]))

        filled = searched = rejected = estimated = 0
        jobs = [(ticker, ciks[ticker], years) for ticker, years in gaps.items() if ticker in ciks]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_search_10k_buyback, edgar, cik, years) for _, cik, years in jobs]
            for i, ((ticker, _, _), future) in enumerate(zip(jobs, futures), 1):
                found, n = future.result()
                searched += n
                for r in rows:
                    if r["티커"] == ticker and int(r["연도"]) in found:
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
        print(f"{path.stem}: 빈칸 {sum(len(y) for y in gaps.values())}칸 중 {filled}칸 채움(추정 {estimated}칸), "
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
    [연도]               기본: 연간 일괄 결과의 최신 연도
    --ratio R            시가총액 ÷ (유동자산 - 총부채) 상한 (기본 0.667 = 그레이엄 2/3)
    --profit A,B         허용할 순이익 상태 (기본 흑자,흑자전환)
    --profit-years N     순이익 상태를 볼 기간 3/5/10 (기본 3)
    --no-buyback         자사주매입 주식수 전년 대비 증가 조건 끄기
    --price-date YYYYMMDD  주가 기준일 (기본: 해당 연도 12월 31일, 휴장이면 직전 거래일)
    """
    def opt(name, default):
        return args[args.index(name) + 1] if name in args else default

    path = DATA_DIR / "analysis/dart/batch_상장사_연간.csv"
    if not path.exists():
        sys.exit(f"{path} 가 없습니다 → 먼저 analyze-dart-batch 를 실행하세요")
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    year = int(positional[0]) if positional else max(int(r["연도"]) for r in rows if r["주당순유동자산"])
    max_ratio = float(opt("--ratio", 2 / 3))
    profit_status = set(opt("--profit", "흑자,흑자전환").split(","))
    profit_years = int(opt("--profit-years", 3))
    price_date = opt("--price-date", f"{year}1231")

    krx = KrxClient(os.getenv("KRX_API_KEY", ""), cache_dir=DATA_DIR / "cache/krx")
    bas_dd, market = krx.market_data(date(int(price_date[:4]), int(price_date[4:6]), int(price_date[6:])))
    print(f"재무 {year}년 / 주가 기준일 {bas_dd}")

    passed, stages = analysis.screen_net_net(rows, year, market, max_ratio, profit_years, profit_status,
                                             buyback_up="--no-buyback" not in args)
    for name, count in stages.items():
        print(f"  {name}: {count}")
    save_csv(f"analysis/dart/screen_{year}_{bas_dd}.csv", passed, list(passed[0]) if passed else ["종목코드"])


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


def analyze_dart_batch(dart: DartClient, start: int, end: int, buyback_years: list[int] = ()) -> None:
    """종목코드가 있는 회사 전체의 유동자산·총부채·순이익을 통합 CSV로 저장."""
    corps = {c["corp_code"]: c for c in dart.corp_codes() if c["stock_code"]}
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
        fill_edgar_buyback(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), args)
    elif cmd == "fix-edgar-buyback":
        fix_edgar_buyback(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))
    elif cmd == "refresh-edgar":
        refresh_edgar_tickers(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")), args)
    elif cmd == "merge-edgar":
        merge_edgar(EdgarClient(os.getenv("EDGAR_USER_AGENT", "")))

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
