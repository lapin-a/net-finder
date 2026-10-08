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
"""
import csv
import json
import os
import sys
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


QUARTER_ENDS = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


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
