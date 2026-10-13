"""넷넷 스크리너 결과 CSV → 한 페이지 HTML (data/analysis/netnet_candidates.html).

    python report/build_page.py

한국: 최신 분기·최신 연도 재무에 최근 거래일(어제 이전) 주가로 screen-dart를 비율 조건만으로 돌린다 (DART 호출 없음, KRX 캐시).
미국: 주가 파일의 최신 월 screen-edgar 결과(--no-profit --no-buyback 파일)를 읽는다. 없으면 먼저 그 명령을 실행하라고 알린다.
결과를 report/template.html에 넣는다.
거래대금: 주가 기준일까지 최근 3개월 하루 평균 (한국 KRX 일별 거래대금, 미국 Alpaca 월봉 VWAP × 거래량 ÷ 거래일).
"""
import csv
import json
import os
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

from collectors import KrxClient  # noqa: E402
from main import kr_minority, screen_dart, screen_tag, us_kinds  # noqa: E402

load_dotenv(ROOT / ".env")
A = ROOT / "data/analysis"
MONTHS = 3  # 거래대금 평균 기간
NOTES = {r["티커"]: r["메모"] for r in csv.DictReader(open(ROOT / "manual_notes.csv", encoding="utf-8-sig"))} \
    if (ROOT / "manual_notes.csv").exists() else {}  # 공시 본문으로 확인한 종목 메모 (확인 필요 칸에 표시)


def load(p):
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 종류: SPAC / ETF / 펀드 (투자회사·신탁·리츠·인프라·선박투자). 나머지는 일반 기업(""). 미국은 main.us_kinds (스크리너와 같이 씀)
KR_NAME_KIND = [(re.compile(r"스팩|기업인수목적"), "SPAC"), (re.compile(r"ETF|ETN"), "ETF"),
                (re.compile(r"리츠|인프라|선박투자|펀드|투자회사"), "펀드")]


def kr_kind(name: str) -> str:
    return next((k for pat, k in KR_NAME_KIND if pat.search(name)), "")


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


NCAV_JUMP = 2  # 순유동자산이 직전 기간 중앙값의 이 배수 넘게(또는 1/배수 미만) 바뀌면 표시


def kr_history(kind: str) -> dict:
    """종목코드 → {기간: 순유동자산}. kind: 분기(기간 '2026Q2') / 연간(연도)."""
    out = {}
    for r in load(A / f"dart/batch_상장사_{kind}.csv"):
        ca, tl = num(r["유동자산"]), num(r["총부채"])
        if ca is not None and tl is not None:
            out.setdefault(r["종목코드"], {})[r["기간" if kind == "분기" else "연도"]] = ca - tl
    return out


def kr_flags(r, history: dict, period: str, back: list[str]) -> list[str]:
    flags = []  # 비지배지분 표시는 페이지에서 '비지배지분 빼기' 상태에 따라 붙임 (template.html)
    prev = [v for p, v in history.get(r["종목코드"], {}).items() if p in back]
    cur = num(r["순유동자산"])
    if len(prev) >= 2 and cur:
        med = statistics.median(prev)
        if med <= 0 or not 1 / NCAV_JUMP <= cur / med <= NCAV_JUMP:
            flags.append(f"순유동자산 급변: 직전 {len(back)}{'분기' if 'Q' in period else '년'} 중앙값 "
                         f"{med / 1e8:,.0f}억 → {cur / 1e8:,.0f}억 원")
    if r["종목코드"] in NOTES:  # manual_notes.csv (한국은 종목코드)
        flags.append(NOTES[r["종목코드"]])
    return flags


def kr(rows, volume, history, period):
    """비지배지분(nci)과 뺀 값(ncavAdj·ratioAdj·ncavpsAdj)을 같이 넣는다. 페이지의 '비지배지분 빼기'로 둘 중 하나를 보여 줌.
    nci가 None이면 DART에서 못 받은 것 (한도 초과 등)."""
    minority = kr_minority([r["종목코드"] for r in rows], period)
    out = []
    for r, row in zip(rows, _kr_rows(rows, volume, history, period)):
        n = minority[r["종목코드"]]
        adj = row["ncav"] - (n or 0)
        shares = row["mcap"] / row["price"]
        out.append({**row, "nci": n, "ncavAdj": adj,
                    "ratioAdj": round(row["mcap"] / adj, 3) if adj > 0 else None,
                    "ncavpsAdj": round(adj / shares, 2)})
    return out


def _kr_rows(rows, volume, history, period):
    if "Q" in period:  # 직전 4분기
        y, q = int(period[:4]), int(period[-1])
        back = [f"{(y * 4 + q - 1 - k) // 4}Q{(y * 4 + q - 1 - k) % 4 + 1}" for k in range(1, 5)]
    else:  # 직전 3년
        back = [str(int(period) - k) for k in range(1, 4)]
    return [{
        "code": r["종목코드"], "name": r["회사명"],
        "ratio": num(r["시총÷순유동자산"]), "price": num(r["종가"]),
        "ncavps": num(r["주당순유동자산"]), "mcap": num(r["시가총액"]), "ncav": num(r["순유동자산"]),
        "ni": num(r["순이익"]), "status": r.get("순이익상태_3년", ""), "flags": kr_flags(r, history, period, back),
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
        foreign = ""
        if r.get("해외"):  # 20-F·40-F 회사: '통화 회계기준 제출 날짜' → 이름 옆 표시 (확인 필요 아님)
            currency, standard = r["해외"].split()[:2]
            foreign = f"해외 {currency}{' IFRS' if standard == 'ifrs-full' else ''}"
            if num(r.get("ADS비율")) not in (None, 1):
                foreign += f" · ADS 1주=원주 {num(r['ADS비율']):g}주"
            if (r.get("ADS_문구") or "").startswith("미확인") and "epositar" in r["ADS_문구"]:
                flags.append("ADS 비율 미확인")
            if (r.get("발행주식수_출처") or "").startswith("가중평균"):
                flags.append("주식수 가중평균 (단위 확인)")
        if r["티커"] in NOTES:  # manual_notes.csv: 공시 본문으로 확인한 내용
            flags.append(NOTES[r["티커"]])
        if r.get("단위_의심"):
            flags.append(f"단위 오류 의심 ({r['단위_의심']})")
        ratio = num(r["시총÷순유동자산"])
        if ratio is not None and ratio < 0.01:
            flags.append("비율 0.01 미만: 주식수·단위 확인")
        out.append({
            "code": r["티커"], "name": r["회사명"],
            "ratio": num(r["시총÷순유동자산"]), "price": num(r["종가"]),
            "ncavps": num(r["주당순유동자산"]), "mcap": mcap, "ncav": num(r["순유동자산"]),
            "ni": num(r["순이익"]), "status": r.get("순이익상태_3년", ""), "flags": flags,
            "vol": round(volume.get(r["티커"], 0)), "kind": kinds.get(r["CIK"], ""), "foreign": foreign,
        })
    return out


def fmt_day(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


RATIO_ONLY = ["--no-profit", "--no-buyback"]  # 목록 페이지는 비율 조건만
krx = KrxClient(os.getenv("KRX_API_KEY", ""), cache_dir=ROOT / "data/cache/krx")
price_day, _ = krx.last_trading_day(date.today() - timedelta(days=1))  # 오늘 자료는 장 마감 뒤에야 나옴
kr_q_path = screen_dart(["--quarterly", *RATIO_ONLY, "--price-date", price_day])
kr_y_path = screen_dart([*RATIO_ONLY, "--price-date", price_day])
kr_q, kr_y = kr_q_path.stem.split("_")[1], kr_y_path.stem.split("_")[1]  # screen_<기간>_<기준일>_<조건>
us_month = max(r["월"] for r in load(A / "edgar/주가_월별.csv"))
us_path = A / f"edgar/screen_{us_month}{screen_tag(RATIO_ONLY)}.csv"
if not us_path.exists():
    sys.exit(f"{us_path} 가 없습니다 → 먼저 python main.py screen-edgar {us_month} {' '.join(RATIO_ONLY)}")
kr_volume_now = kr_volume(price_day)
data = {
    "krq": {"label": f"한국 {kr_q[:4]}년 {kr_q[-1]}분기", "cur": "KRW", "asof": f"재무 {kr_q} · 주가 {fmt_day(price_day)}",
            "niLabel": "순이익(최근 4분기)", "rows": kr(load(kr_q_path), kr_volume_now, kr_history("분기"), kr_q)},
    "kry": {"label": f"한국 {kr_y}년 연간", "cur": "KRW", "asof": f"재무 {kr_y}년 · 주가 {fmt_day(price_day)}",
            "niLabel": f"순이익({kr_y})", "rows": kr(load(kr_y_path), kr_volume_now, kr_history("연간"), kr_y)},
    "us": {"label": f"미국 {us_month[:4]}년 {int(us_month[5:])}월", "cur": "USD",
           "asof": f"주가 {us_month} 말 · 회사별 최신 분기 재무 (해외 기업은 최신 20-F·40-F·6-K)",
           "niLabel": "순이익(최근 4분기)", "rows": us(load(us_path), us_volume(us_month))},
}
tpl = (ROOT / "report/template.html").read_text(encoding="utf-8")
out = A / "netnet_candidates.html"
out.write_text(tpl.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
print(out, {k: len(v["rows"]) for k, v in data.items()})
