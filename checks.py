"""결과 파일 확인 (회귀 검사). 규칙을 고친 뒤 다시 계산하고 돌려서, 이미 공시 본문으로 확인한 값이 그대로인지 본다.

    python checks.py

API 호출 없이 data/analysis의 결과 파일만 읽는다. 실패가 하나라도 있으면 종료 코드 1.
- 계산식: 스크리너 결과의 순유동자산·비율을 열 값으로 다시 계산
- 확인한 종목: TODO.md에 기록한 본문 확인 결과 (CREG 수동 주식수, SMTK 전환 후 비율, LHSW 탈락 등)
- 분할·우선주·총부채 열: AAPL·NVDA·삼성전자 분할 배율, EHTH 상환우선주 등
- 목록 페이지: 탭별 종목 수가 원본 CSV와 같은지, manual_notes.csv 열 수
"""
import csv
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
A = ROOT / "data/analysis"
US_MONTH = "2026-09"  # 아래 확인한 종목 값의 기준 월
RATIO_ONLY = "_no-buyback_no-profit"
failures = []


def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else f' — {detail}'}")
    if not ok:
        failures.append(name)


def close(a, b, rel=0.005, abs_=0.0015):
    return a is not None and b is not None and abs(a - b) <= max(abs_, rel * abs(b))


def latest(pattern):
    paths = sorted(A.glob(pattern))
    return paths[-1] if paths else None


# ---------------------------------------------------------------- 계산식

def formula(path):
    bad = []
    for r in load(path):
        ca, tl, ncav = num(r["유동자산"]), num(r["총부채"]), num(r["순유동자산"])
        senior = (num(r.get("우선주")) or 0) + max(num(r.get("비지배지분")) or 0, 0)
        if not close(ca - tl - senior, ncav, rel=1e-6, abs_=1):
            bad.append(f"{r.get('티커') or r['종목코드']} 순유동자산")
        elif not close(num(r["시가총액"]) / ncav, num(r["시총÷순유동자산"])):
            bad.append(f"{r.get('티커') or r['종목코드']} 비율")
        elif num(r["시총÷순유동자산"]) > 2 / 3 + 0.001:
            bad.append(f"{r.get('티커') or r['종목코드']} 2/3 초과")
    check(f"계산식 {path.name}", not bad, ", ".join(bad[:5]))


us_path = A / f"edgar/screen_{US_MONTH}{RATIO_ONLY}.csv"
kr_paths = [p for p in (latest(f"dart/screen_20??Q?_*{RATIO_ONLY}.csv"), latest(f"dart/screen_20??_*{RATIO_ONLY}.csv")) if p]
for p in [us_path, *kr_paths]:
    if p.exists():
        formula(p)
    else:
        check(f"파일 {p.name}", False, "없음")

# ---------------------------------------------------------------- 확인한 종목 (미국 2026-09, 비율만)

if us_path.exists():
    us = {r["티커"]: r for r in load(us_path)}

    def field(t, key):
        return us.get(t, {}).get(key)

    check("CREG 수동 주식수 274.6만 주 (5/20 증자 후 1:10 병합)", close(num(field("CREG", "발행주식수")), 2_745_912.8, abs_=1))
    check("CTNT 수동 주식수 1,333,445주 (9/30 8-K)", close(num(field("CTNT", "발행주식수")), 1_333_445, abs_=1))
    check("MI 6/30 주식수 231,129주 (문장 함정 수동 수정)", close(num(field("MI", "발행주식수")), 231_129, abs_=1))
    check("SMTK 전환 후 비율 3.48 (9/18 S-1)", close(num(field("SMTK", "전환후_시총÷순유동자산")), 3.48, abs_=0.01))
    check("PPCB·OGEN·SHFS 전환 후 2/3 초과",
          all((num(field(t, "전환후_시총÷순유동자산")) or 0) > 2 / 3 for t in ("PPCB", "OGEN", "SHFS")))
    check("UPC 단위 오류 의심 표시", bool(field("UPC", "단위_의심")))
    check("TC ADS 1주 = 원주 4,800주", num(field("TC", "ADS비율")) == 4800)
    gone = [t for t in ("FTFT", "LHSW", "WCT", "EHTH", "FBIO") if t in us]
    check("탈락 확인 종목 (FTFT 위임장 주식수, LHSW·WCT 병합 중복, EHTH 상환우선주, FBIO 비지배지분)", not gone,
          f"결과에 있음: {gone}")
    check("CREG·NXTT 분할·병합 보정", all(field(t, "분할보정") for t in ("CREG", "NXTT")))

# ---------------------------------------------------------------- 분기·연간표 열

def find(path, keys, key_cols):
    want = {tuple(k): None for k in keys}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = tuple(r[c] for c in key_cols)
            if k in want:
                want[k] = r
    return want


us_q = find(A / "edgar/분기.csv", [("AAPL", "2014Q2"), ("NVDA", "2021Q1"), ("EHTH", "2026Q2"), ("FBIO", "2026Q2"),
                                    ("ACMR", "2026Q2"), ("AMZN", "2026Q2")], ("티커", "기간"))
check("AAPL 2014Q2 분할 배율 28 (7:1 × 4:1)", close(num((us_q[("AAPL", "2014Q2")] or {}).get("분할반영_배율")), 28, rel=0.01))
check("NVDA 2021Q1 분할 배율 40 (4:1 × 10:1)", close(num((us_q[("NVDA", "2021Q1")] or {}).get("분할반영_배율")), 40, rel=0.01))
ehth = us_q[("EHTH", "2026Q2")] or {}
check("EHTH 상환우선주 $4.06억 (임시자본)", close(num(ehth.get("우선주")), 406_283_000, abs_=1) and ehth.get("우선주_출처") == "임시자본")
check("FBIO 우선주 = 주당 청산가치 × 주식수", (us_q[("FBIO", "2026Q2")] or {}).get("우선주_출처") == "주당청산가치×주식수")
check("ACMR 옛 표지 값(1,418만 주) 대신 가중평균", (num((us_q[("ACMR", "2026Q2")] or {}).get("발행주식수")) or 0) > 50_000_000)
check("AMZN 총부채 = 부채와자본총계 − 자본총계", (us_q[("AMZN", "2026Q2")] or {}).get("총부채_출처") == "부채와자본총계−자본총계")

samsung = find(A / "dart/batch_상장사_연간.csv", [("005930", "2017"), ("005930", "2018")], ("종목코드", "연도"))
sys.path.insert(0, str(ROOT))
import analysis  # noqa: E402

check("비지배지분 계정 읽기 (연결 없음 0, 음수 0)",
      analysis.dart_minority_interest([]) == 0
      and analysis.dart_minority_interest([{"sj_div": "BS", "account_id": "ifrs-full_NoncontrollingInterests",
                                            "account_nm": "비지배지분", "thstrm_amount": "1,234"}]) == 1234
      and analysis.dart_minority_interest([{"sj_div": "BS", "account_id": "x", "account_nm": "비지배지분",
                                            "thstrm_amount": "-5"}]) == 0)
check("삼성전자 2017 분할 배율 50 (2018년 50:1)", num((samsung[("005930", "2017")] or {}).get("분할반영_배율")) == 50)

# 한국: 네 분기 순이익 합 = 연간 (4분기 = 연간 − 3분기 누적). 정정·재작성 때문에 어긋나는 회사는 README 기준 약 4%
annual = {(r["종목코드"], r["연도"]): num(r["순이익"]) for r in load(A / "dart/batch_상장사_연간.csv")}
quarters = {}
for r in load(A / "dart/batch_상장사_분기.csv"):
    quarters.setdefault((r["종목코드"], r["기간"][:4]), []).append(num(r["순이익(분기)"]))
pairs = [(sum(qs), annual[k]) for k, qs in quarters.items()
         if len(qs) == 4 and None not in qs and annual.get(k) and k[1] >= "2016" and k[1] <= "2025"]
off = sum(abs(s - a) > 0.01 * abs(a) for s, a in pairs) / len(pairs)
check(f"한국 네 분기 순이익 합 ≈ 연간 (1% 넘게 어긋남 {off:.1%}, 기준 10% 미만)", off < 0.10)

# ---------------------------------------------------------------- 목록 페이지

page = A / "netnet_candidates.html"
if page.exists():
    line = next(l for l in page.read_text(encoding="utf-8").splitlines() if l.startswith("const DATA = "))
    data = json.loads(line[len("const DATA = "):].rstrip(";"))
    for key, tab in data.items():
        if tab["cur"] == "KRW":  # asof: '재무 2026Q2 · 주가 2026-10-08' → screen_2026Q2_20261008_…
            m = re.match(r"재무 (\w+?)년? · 주가 (\d{4})-(\d\d)-(\d\d)", tab["asof"])
            src = A / f"dart/screen_{m[1]}_{m[2]}{m[3]}{m[4]}{RATIO_ONLY}.csv"
        else:
            src = A / f"edgar/screen_{re.search(r'\d{4}-\d\d', tab['asof'])[0]}{RATIO_ONLY}.csv"
        if tab["cur"] == "KRW":  # 비지배지분 뺀 비율 = 시가총액 ÷ (순유동자산 − 비지배지분)
            bad = [r["name"] for r in tab["rows"] if r.get("nci") is not None and r["ratioAdj"] is not None
                   and not close(r["mcap"] / (r["ncav"] - r["nci"]), r["ratioAdj"])]
            check(f"페이지 {tab['label']} 비지배지분 뺀 비율", not bad, ", ".join(bad[:5]))
        check(f"페이지 {tab['label']} 종목 수 = {src.name}", src.exists() and len(load(src)) == len(tab["rows"]),
              f"페이지 {len(tab['rows'])}, 파일 {len(load(src)) if src.exists() else '없음'}")

notes = ROOT / "manual_notes.csv"
if notes.exists():
    with open(notes, encoding="utf-8-sig") as f:
        bad = [r[0] for r in list(csv.reader(f))[1:] if len(r) != 3]
    check("manual_notes.csv 열 3개 (쉼표가 든 메모는 따옴표)", not bad, f"{bad}")

from main import screen_tag  # noqa: E402

check("screen_tag", screen_tag([]) == "" and screen_tag(["2026Q2", "--no-profit", "--no-buyback"]) == RATIO_ONLY
      and screen_tag(["--quarterly", "--price-date", "20261008", "--ratio", "1"]) == "_ratio1")

print(f"\n{'실패 ' + str(len(failures)) + '개' if failures else '모두 통과'}")
sys.exit(1 if failures else 0)
