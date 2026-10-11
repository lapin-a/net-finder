"""넷넷 스크리너 결과 CSV → 한 페이지 HTML (data/analysis/netnet_candidates.html).

    python report/build_page.py

한국 2026Q2·2025, 미국 2026-09 결과를 읽어 report/template.html에 넣는다.
거래대금: 주가 기준일까지 최근 3개월 하루 평균 (한국 KRX 일별 거래대금, 미국 Alpaca 월봉 VWAP × 거래량 ÷ 거래일).
"""
import csv
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

from collectors import EdgarClient, KrxClient  # noqa: E402

load_dotenv(ROOT / ".env")
A = ROOT / "data/analysis"
MONTHS = 3  # 거래대금 평균 기간


def load(p):
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 종류: SPAC / ETF / 펀드 (투자회사·신탁·리츠·인프라·선박투자). 나머지는 일반 기업("")
US_SIC_KIND = {"6770": "SPAC", "6221": "ETF", "6722": "펀드", "6726": "펀드", "6798": "펀드"}
KR_NAME_KIND = [(re.compile(r"스팩|기업인수목적"), "SPAC"), (re.compile(r"ETF|ETN"), "ETF"),
                (re.compile(r"리츠|인프라|선박투자|펀드|투자회사"), "펀드")]
US_NAME_KIND = [(re.compile(r"\bAcquisition (Corp|Co|Company|Holdings)\b|\bSPAC\b", re.I), "SPAC"),
                (re.compile(r"\bETF\b|\bETN\b", re.I), "ETF"),
                (re.compile(r"\b(Fund|Trust)\b", re.I), "펀드")]


def kr_kind(name: str) -> str:
    return next((k for pat, k in KR_NAME_KIND if pat.search(name)), "")


def us_kinds(rows) -> dict:
    """CIK → 종류. SEC 업종 코드(SIC, data/cache/edgar_sic.json에 저장) 우선, 없으면 이름."""
    cache_path = ROOT / "data/cache/edgar_sic.json"
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


def kr_volume(price_date: str) -> dict:
    """종목코드 → 주가 기준일까지 최근 3개월 하루 평균 거래대금(원). 우선주 거래대금은 빼고 보통주만."""
    krx = KrxClient(os.getenv("KRX_API_KEY", ""), cache_dir=ROOT / "data/cache/krx")
    end = date(int(price_date[:4]), int(price_date[4:6]), int(price_date[6:]))
    total, days = {}, 0
    day = end
    while day > end - timedelta(days=MONTHS * 30):
        if day.weekday() < 5:
            rows = [r for m in ("KOSPI", "KOSDAQ", "KONEX") for r in krx.daily(m, day.strftime("%Y%m%d"))]
            if rows:
                days += 1
                for r in rows:
                    total[r["ISU_CD"]] = total.get(r["ISU_CD"], 0) + (num(r["ACC_TRDVAL"]) or 0)
        day -= timedelta(days=1)
    return {code: v / days for code, v in total.items()} if days else {}


def us_volume(month: str) -> dict:
    """티커 → 그 달까지 최근 3개월 하루 평균 거래대금(달러). 월봉 VWAP × 거래량의 합 ÷ (달 수 × 21거래일)."""
    y, m = int(month[:4]), int(month[5:7])
    months = [f"{y - (m - k <= 0)}-{(m - k - 1) % 12 + 1:02d}" for k in range(MONTHS)]
    total = {}
    for r in load(A / "edgar/주가_월별.csv"):
        if r["월"] in months and r["VWAP"] and r["거래량"]:
            total[r["티커"]] = total.get(r["티커"], 0) + float(r["VWAP"]) * float(r["거래량"])
    return {t: v / (MONTHS * 21) for t, v in total.items()}


def kr(rows, volume):
    return [{
        "code": r["종목코드"], "name": r["회사명"],
        "ratio": num(r["시총÷순유동자산"]), "price": num(r["종가"]),
        "ncavps": num(r["주당순유동자산"]), "mcap": num(r["시가총액"]), "ncav": num(r["순유동자산"]),
        "ni": num(r["순이익"]), "status": r.get("순이익상태_3년", ""), "flags": [],
        "vol": round(volume.get(r["종목코드"], 0)), "kind": kr_kind(r["회사명"]),
    } for r in rows]


def us(rows, volume):
    kinds = us_kinds(rows)
    out = []
    for r in rows:
        flags = []
        days = num(r.get("재무경과일"))
        if days and days > 180:
            flags.append(f"재무 {int(days)}일 전")
        if r.get("분할보정"):
            ratio = float(r["분할보정"].split("×")[1])
            kind = "병합" if ratio < 1 else "분할"
            flags.append(f"{r['분할보정'][:10]} 이후 {kind} 1:{1 / ratio:,.0f} 보정" if ratio < 1 else f"{kind} ×{ratio:g} 보정")
            if ratio < 0.01:
                flags.append("연속·대형 병합: 주식수 미확인")
        if r.get("우선주_출처") == "장부가":
            flags.append("우선주 장부가만")
        if r.get("시가총액_계산", "").startswith("수동"):
            flags.append("공시 본문으로 확인한 주식수")
        elif r.get("시가총액_계산"):
            flags.append("주식 종류별 시총" if "+" in r["시가총액_계산"] and "티커 미표기" not in r["시가총액_계산"] else "표지 원문 주식수")
        diluted = num(r.get("전환후_시총÷순유동자산"))
        if diluted is not None and diluted > 2 / 3:
            flags.append(f"전환우선주 전환 시 비율 {diluted:.2f}")
        mcap = num(r["시가총액"])
        if mcap is not None and mcap < 1e6:
            flags.append("시가총액 $1M 미만")
        if (r.get("발행주식수_출처") or "").find("분기 전") >= 0:
            flags.append("주식수 " + r["발행주식수_출처"].split(", ")[1])
        out.append({
            "code": r["티커"], "name": r["회사명"],
            "ratio": num(r["시총÷순유동자산"]), "price": num(r["종가"]),
            "ncavps": num(r["주당순유동자산"]), "mcap": mcap, "ncav": num(r["순유동자산"]),
            "ni": num(r["순이익"]), "status": r.get("순이익상태_3년", ""), "flags": flags,
            "vol": round(volume.get(r["티커"], 0)), "kind": kinds.get(r["CIK"], ""),
        })
    return out


def fmt_day(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


kr_q, kr_y = "20260630", "20251230"
us_month = "2026-09"
data = {
    "krq": {"label": "한국 2026년 2분기", "cur": "KRW", "asof": f"재무 2026Q2 · 주가 {fmt_day(kr_q)}",
            "niLabel": "순이익(최근 4분기)", "rows": kr(load(A / f"dart/screen_2026Q2_{kr_q}.csv"), kr_volume(kr_q))},
    "kry": {"label": "한국 2025년 연간", "cur": "KRW", "asof": f"재무 2025년 · 주가 {fmt_day(kr_y)}",
            "niLabel": "순이익(2025)", "rows": kr(load(A / f"dart/screen_2025_{kr_y}.csv"), kr_volume(kr_y))},
    "us": {"label": "미국 2026년 9월", "cur": "USD", "asof": f"주가 {us_month} 말 · 회사별 최신 분기 재무",
           "niLabel": "순이익(최근 4분기)", "rows": us(load(A / f"edgar/screen_{us_month}.csv"), us_volume(us_month))},
}
tpl = (ROOT / "report/template.html").read_text(encoding="utf-8")
out = A / "netnet_candidates.html"
out.write_text(tpl.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
print(out, {k: len(v["rows"]) for k, v in data.items()})
