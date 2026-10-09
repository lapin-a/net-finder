"""분기/연간 지표 정리.

지표: 유동자산, 총부채, 발행주식수, 순이익(연평균성장률), 자사주매입

DART와 EDGAR 데이터를 같은 형태로 맞춘 뒤 분기표와 연간표를 만든다.
- 분기 record: {"year", "q"(1~4), "current_assets", "total_liabilities", "shares", "net_income", "buyback"}
  순이익/자사주매입은 해당 분기 3개월치 값
  "buyback_shares"(자사주매입 주식수, 분기), "buyback_shares_ytd"(연초부터 누적)
- 연간 record: 같은 키, 순이익/자사주매입은 회계연도 전체 값
"""
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

BALANCE_KEYS = ("current_assets", "total_liabilities", "shares")


# ---------------------------------------------------------------- 공통 유틸

def _num(value):
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _sub(a, b):
    return None if a is None or b is None else a - b


def _cagr(end, start, years):
    if end is None or start is None or end <= 0 or start <= 0:
        return None  # 적자 구간이 끼면 성장률 정의 불가
    return round(((end / start) ** (1 / years) - 1) * 100, 2)


def _growth_columns(end, start, years) -> dict:
    """순이익 성장 관련 열: CAGR, 흑자/적자 상태, 연평균 증감액.

    CAGR은 처음·마지막이 모두 흑자일 때만 의미가 있으므로, 상태와 증감액을 함께 둔다.
    """
    if end is None or start is None:
        status = None
    elif start > 0 and end > 0:
        status = "흑자"
    elif start <= 0 < end:
        status = "흑자전환"
    elif start > 0 >= end:
        status = "적자전환"
    else:
        status = "적자지속"
    return {
        f"순이익CAGR_{years}년(%)": _cagr(end, start, years),
        f"순이익상태_{years}년": status,
        f"순이익연평균증감_{years}년": None if status is None else round((end - start) / years),
    }


def _growth(current, previous):
    """전년 대비 증가율(%). 전년 값이 0 이하이면 정의 불가."""
    if current is None or previous is None or previous <= 0:
        return None
    return round((current / previous - 1) * 100, 2)


def _ncav_per_share(current_assets, total_liabilities, shares):
    """(유동자산 - 총부채) / 발행주식수"""
    if None in (current_assets, total_liabilities) or not shares:
        return None
    return round((current_assets - total_liabilities) / shares, 2)


def _prev_quarter(year, q, steps=1):
    index = year * 4 + (q - 1) - steps
    return index // 4, index % 4 + 1


# ---------------------------------------------------------------- DART

DART_REPORTS = [("11013", 1), ("11012", 2), ("11014", 3), ("11011", 4)]
DART_BUYBACK_IDS = {
    "ifrs-full_PurchaseOfTreasuryShares",
    "dart_AcquisitionOfTreasuryShares",
    "ifrs-full_PaymentsToAcquireOrRedeemEntitysShares",
}


def _dart_find(rows, sj_divs, account_ids, name_test=None):
    for sj in sj_divs:
        for r in rows:
            if r["sj_div"] != sj:
                continue
            # 2018년 이전 보고서는 'ifrs_' 접두어 사용 → 'ifrs-full_'로 통일
            account_id = r["account_id"].replace("ifrs_", "ifrs-full_", 1)
            if account_id in account_ids or (name_test and name_test(r["account_nm"])):
                return r
    return None


def _dart_buyback_amount(fs_rows) -> int:
    """현금흐름표의 자기주식 취득 금액 (보고서 기준 연초부터 누적, 양수).

    fs_rows는 별도재무제표(OFS)여야 한다. 연결 현금흐름표에는 종속회사의 자기주식 취득도 섞이고,
    주식수(자기주식 취득·처분 현황)는 회사 자체 기준이라 서로 맞지 않는다.
    """
    rows = [r for r in fs_rows if "종속" not in r["account_nm"] and "비지배" not in r["account_nm"]]
    bb = _dart_find(rows, ["CF"], DART_BUYBACK_IDS, lambda nm: "자기주식" in nm and "취득" in nm)
    return abs(_num(bb["thstrm_amount"]) or 0) if bb else 0


def _dart_buyback_shares(treasury_rows, buyback_amount):
    """자기주식 취득 수량 (보통주+우선주, 연초부터 누적). 직접취득 + 신탁계약에 의한 취득.

    총계에서 '기타취득'(단주, 주식매수청구권 행사 등 매입이 아닌 취득)을 뺀다.
    총계 행이 없으면 미공시 → 현금흐름표상 매입 금액이 0일 때만 0주로 판단."""
    totals = [r for r in treasury_rows if r["acqs_mth1"] == "총계"]
    if totals:
        other = [r for r in treasury_rows if r["acqs_mth1"] == "기타취득"]
        return (sum(_num(r["change_qy_acqs"]) or 0 for r in totals)
                - sum(_num(r["change_qy_acqs"]) or 0 for r in other))
    return 0 if buyback_amount == 0 else None


def dart_records(client, corp_code: str, start_year: int, end_year: int):
    quarters, annual = {}, {}

    for year in range(start_year, end_year + 1):
        ytd = {"net_income": {}, "buyback": {}, "buyback_shares": {}}
        for reprt_code, q in DART_REPORTS:
            fs_div = "CFS"
            rows = client.financial_statements(corp_code, str(year), reprt_code, "CFS")
            if not rows:  # 종속회사가 없으면 연결재무제표가 없음 → 별도재무제표
                fs_div = "OFS"
                rows = client.financial_statements(corp_code, str(year), reprt_code, "OFS")
            if not rows:
                continue

            rec = {"year": year, "q": q}
            ca = _dart_find(rows, ["BS"], {"ifrs-full_CurrentAssets"})
            tl = _dart_find(rows, ["BS"], {"ifrs-full_Liabilities"})
            rec["current_assets"] = _num(ca and ca["thstrm_amount"])
            rec["total_liabilities"] = _num(tl and tl["thstrm_amount"])

            ni = _dart_find(rows, ["IS", "CIS"], {"ifrs-full_ProfitLoss"})
            if q == 4:
                ytd["net_income"][4] = _num(ni and ni["thstrm_amount"])
                rec["net_income"] = _sub(ytd["net_income"][4], ytd["net_income"].get(3))
            else:
                rec["net_income"] = _num(ni and ni["thstrm_amount"])
                cum = _num(ni and ni.get("thstrm_add_amount"))
                if cum is None and q == 1:
                    cum = rec["net_income"]
                ytd["net_income"][q] = cum

            # 현금흐름표는 분기보고서에서도 누적값 → 직전 누적값을 빼서 분기값 계산
            # 자사주매입은 회사 자체 기준(별도재무제표)으로
            ofs_rows = rows if fs_div == "OFS" else (
                client.financial_statements(corp_code, str(year), reprt_code, "OFS") or rows)
            bb_cum = _dart_buyback_amount(ofs_rows)
            ytd["buyback"][q] = bb_cum
            rec["buyback"] = bb_cum if q == 1 else _sub(bb_cum, ytd["buyback"].get(q - 1))

            shares = [r for r in client.share_counts(corp_code, str(year), reprt_code) if r["se"] == "합계"]
            rec["shares"] = _num(shares[0]["istc_totqy"]) if shares else None

            bs_cum = _dart_buyback_shares(client.treasury_stock(corp_code, str(year), reprt_code), bb_cum)
            ytd["buyback_shares"][q] = bs_cum
            rec["buyback_shares_ytd"] = bs_cum
            rec["buyback_shares"] = bs_cum if q == 1 else _sub(bs_cum, ytd["buyback_shares"].get(q - 1))

            quarters[(year, q)] = rec
            if q == 4:
                annual[year] = {**rec, "net_income": ytd["net_income"][4], "buyback": bb_cum,
                                "buyback_shares": bs_cum}

    return quarters, annual


def _dart_report_settled(year: int, q: int, today: date | None = None) -> bool:
    """보고서 제출기한(분기·반기 45일, 사업보고서 90일)에서 30일이 지났는지. 지나기 전에는 공시가 덜 모였다고 본다."""
    deadline = {1: date(year, 5, 15), 2: date(year, 8, 14), 3: date(year, 11, 14), 4: date(year + 1, 3, 31)}[q]
    return ((today or date.today()) - deadline).days > 30


def dart_multi_records(client, corp_codes: list[str], start_year: int, end_year: int,
                       progress=print, cache_dir=None):
    """다중회사 주요계정 API로 유동자산·총부채·순이익만 수집 (100개 회사씩 한 번에).

    발행주식수·자사주매입은 이 API에 없으므로 비워 둔다.
    cache_dir를 주면 응답을 파일로 저장해 두고, 다시 실행할 때 이미 받은 것은 건너뛴다.
    반환: {corp_code: (quarters, annual)}
    """
    def fetch(chunk, year, reprt_code, q):
        if cache_dir is None:
            return client.multi_major_accounts(chunk, str(year), reprt_code)
        key = hashlib.md5(",".join(chunk).encode()).hexdigest()[:12]
        path = Path(cache_dir) / f"{year}_{reprt_code}_{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        rows = client.multi_major_accounts(chunk, str(year), reprt_code)
        if _dart_report_settled(year, q):  # 제출기한 전 보고서는 아직 덜 들어왔으므로 저장하지 않음
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    # raw[corp][(year, q)] = {"current_assets", "total_liabilities", "ni", "ni_ytd"}
    raw = {}
    chunks = [corp_codes[i:i + 100] for i in range(0, len(corp_codes), 100)]
    for year in range(start_year, end_year + 1):
        for reprt_code, q in DART_REPORTS:
            for chunk in chunks:
                by_corp = {}
                for r in fetch(chunk, year, reprt_code, q):
                    by_corp.setdefault(r["corp_code"], {}).setdefault(r["fs_div"], []).append(r)
                for corp, fs in by_corp.items():
                    fs_div = "CFS" if fs.get("CFS") else "OFS"  # 연결 우선, 없으면 별도
                    rows = fs[fs_div]

                    def pick(name_test):
                        return next((r for r in rows if name_test(r["account_nm"].replace(" ", ""))), None)

                    ca = pick(lambda n: n == "유동자산")
                    tl = pick(lambda n: n == "부채총계")
                    ni = pick(lambda n: n.startswith("당기순이익") or n.startswith("분기순이익") or n.startswith("반기순이익"))
                    ni_val = _num(ni and ni["thstrm_amount"])
                    ni_ytd = ni_val if q in (1, 4) else _num(ni and ni.get("thstrm_add_amount"))
                    raw.setdefault(corp, {})[(year, q)] = {
                        "current_assets": _num(ca and ca["thstrm_amount"]),
                        "total_liabilities": _num(tl and tl["thstrm_amount"]),
                        "ni": ni_val,
                        "ni_ytd": ni_ytd,
                        "fs_div": fs_div,
                    }
            progress(f"{year} {reprt_code} 완료 (회사 {len(raw)})")

    result = {}
    for corp, periods in raw.items():
        quarters, annual = {}, {}
        for (year, q), v in sorted(periods.items()):
            if q == 4:  # 사업보고서는 연간값 → 4분기 = 연간 - 3분기 누적
                net_income = _sub(v["ni"], periods.get((year, 3), {}).get("ni_ytd"))
            else:
                net_income = v["ni"]
            rec = {"year": year, "q": q, "current_assets": v["current_assets"],
                   "total_liabilities": v["total_liabilities"], "shares": None,
                   "net_income": net_income, "buyback": None,
                   "buyback_shares": None, "buyback_shares_ytd": None}
            quarters[(year, q)] = rec
            if q == 4:
                annual[year] = {**rec, "net_income": v["ni"], "fs_div": v["fs_div"]}
        result[corp] = (quarters, annual)
    return result


def fill_dart_annual_buyback(client, results: dict, years, progress=print) -> int:
    """사업보고서 기준 연간 자사주매입(금액·주식수)을 회사별로 받아 results에 채운다.

    회사당 연도마다 2건 호출(별도재무제표 + 자기주식 현황). years 순서대로 전체 회사를 처리한다.
    DART 일일 한도(오류 020)에 걸리면 거기서 멈추고 처리한 회사 수를 돌려준다.
    """
    done = 0
    for year in years:
        targets = [(corp, annual[year]) for corp, (_, annual) in results.items() if year in annual]
        for i, (corp, rec) in enumerate(targets, 1):
            try:
                # 자사주매입은 회사 자체 기준(별도재무제표). 별도가 없으면 연결로 대체
                fs_rows = (client.financial_statements(corp, str(year), "11011", "OFS")
                           or client.financial_statements(corp, str(year), "11011", rec["fs_div"]))
                treasury = client.treasury_stock(corp, str(year), "11011")
            except RuntimeError as e:
                if "020" in str(e):
                    progress(f"DART 일일 한도 도달: {year}년 {i - 1}/{len(targets)}에서 중단")
                    return done
                raise
            amount = _dart_buyback_amount(fs_rows) if fs_rows else None
            rec["buyback"] = amount
            rec["buyback_shares"] = _dart_buyback_shares(treasury, amount) if amount is not None else None
            # 분기표 4분기의 연초누적 = 연간값
            results[corp][0].get((year, 4), {})["buyback_shares_ytd"] = rec["buyback_shares"]
            done += 1
            if i % 200 == 0:
                progress(f"{year} 자사주매입 {i}/{len(targets)}")
        progress(f"{year} 자사주매입 완료 ({len(targets)}개 회사)")
    return done


# ---------------------------------------------------------------- EDGAR

EDGAR_TAGS = {
    "current_assets": ["AssetsCurrent"],
    "total_liabilities": ["Liabilities"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "buyback": ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"],
    "buyback_shares": ["StockRepurchasedDuringPeriodShares", "StockRepurchasedAndRetiredDuringPeriodShares",
                       "TreasuryStockSharesAcquired"],
}
EDGAR_FLOW_UNITS = {"net_income": "USD", "buyback": "USD", "buyback_shares": "shares"}
EDGAR_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}
FP_TO_Q = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}


def _edgar_entries(facts, taxonomy, tags, unit):
    """여러 항목명의 값을 모두 모은다. 회사가 연도별로 항목명을 바꾸는 경우가 많기 때문.

    같은 기간이 여러 항목명에 있으면 tags 앞쪽이 우선하도록, 우선순위가 높은 값이 뒤에 오게 정렬한다.
    """
    entries = []
    for rank, tag in enumerate(tags):
        units = facts["facts"].get(taxonomy, {}).get(tag, {}).get("units", {})
        entries += [{**e, "_rank": rank} for e in units.get(unit, []) if e.get("form") in EDGAR_FORMS]
    return sorted(entries, key=lambda e: (-e["_rank"], e["filed"]))


def _days(e):
    return (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days


def edgar_periods(facts: dict) -> dict:
    """각 공시(accn)의 보고 기간 종료일과 회계연도·분기. 반환: {accn: (종료일, 회계연도, 분기)}"""
    period_of_accn, annual_report = {}, set()
    for e in _edgar_entries(facts, "us-gaap", ["AssetsCurrent", "Assets"], "USD"):
        if e.get("fp") in FP_TO_Q and e["end"] > period_of_accn.get(e["accn"], ("",))[0]:
            period_of_accn[e["accn"]] = (e["end"], e["fy"], FP_TO_Q[e["fp"]])
            if e["form"].startswith("10-K"):
                annual_report.add(e["accn"])
    # 회사가 회계연도를 잘못 적은 공시(PRTH 43830 = 엑셀 날짜 일련번호, WTBA 2107 등)는
    # 같은 회사의 다른 공시에서 흔한 '회계연도 − 종료일 연도' 차이로 다시 계산
    offsets = [fy - int(end[:4]) for end, fy, _ in period_of_accn.values() if fy and abs(fy - int(end[:4])) <= 1]
    offset = max(set(offsets), key=offsets.count) if offsets else 0
    for accn, (end, fy, q) in period_of_accn.items():
        if not fy or abs(fy - int(end[:4])) > 1:
            period_of_accn[accn] = (end, int(end[:4]) + offset, q)
    return _fix_period_collisions(period_of_accn, annual_report)


def _collided(period_of_accn: dict) -> set:
    """종료일이 다른 공시와 같은 (회계연도, 분기)를 쓰는 공시들."""
    ends = {}
    for end, fy, q in period_of_accn.values():
        ends.setdefault((fy, q), set()).add(end)
    return {accn for accn, (_, fy, q) in period_of_accn.items() if len(ends[(fy, q)]) > 1}


def _fy_label_year(d: date) -> int:
    """회계연도 종료일이 속한 해. 1월 초에 끝나는 52·53주 회계연도는 앞 해로 본다."""
    return (d - timedelta(days=7)).year


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year + years, day=28)


def _fix_period_collisions(period_of_accn: dict, annual_report: set) -> dict:
    """같은 (회계연도, 분기)에 종료일이 다른 공시가 겹치면 겹친 공시만 종료일로 다시 계산한다.

    회사가 표지에 회계연도·분기를 잘못 적은 경우가 많다 (ACN은 2018-11-30·2019-11-30 분기를 둘 다 2019 Q1,
    AES는 세 분기를 모두 2022 Q2). 겹치지 않은 공시는 회사 표기를 그대로 둔다 (10-K 원문 연도와 맞추기 위해).
    분기: 10-K 종료일(회계연도 끝)들 사이에서 몇 번째 3개월인지. 회계연도: 가장 가까운 정상 10-K의 표기 ± 연도 차이.
    다시 계산한 값이 정상 공시와 또 겹치거나(결산월 변경, 회사의 회계연도 표기 방식 변경 등) 분기를 정할 수 없으면 원래 값을 둔다.
    """
    bad = _collided(period_of_accn)
    anchors = sorted({date.fromisoformat(period_of_accn[a][0]) for a in annual_report})
    if not bad or not anchors:
        return period_of_accn
    # 같은 종료일 10-K가 여럿이면 나중에 낸 공시(접수번호가 큰 쪽) 표기를 쓴다
    label = {date.fromisoformat(period_of_accn[a][0]): period_of_accn[a][1] for a in sorted(annual_report) if a not in bad}
    tolerance = timedelta(days=7)  # 52·53주 회계연도
    out = dict(period_of_accn)
    for accn in bad:
        end, fy, q = period_of_accn[accn]
        e = date.fromisoformat(end)
        fy_end = next((d for d in anchors if d >= e - tolerance), None)
        if fy_end is None:  # 마지막 10-K 이후 분기
            k = 1
            while _add_years(anchors[-1], k) < e - tolerance:
                k += 1
            fy_end = _add_years(anchors[-1], k)
        fy_start = max((d for d in anchors if d < fy_end - timedelta(days=300)), default=None)
        if fy_start is None or (fy_end - fy_start).days > 380:  # 10-K가 빠진 해
            fy_start = _add_years(fy_end, -1)
        new_q = 4 if abs((fy_end - e).days) <= 7 else round((e - fy_start).days / 91.3)
        if not 1 <= new_q <= 4:
            continue
        if label:
            near = min(label, key=lambda d: (abs((d - fy_end).days), -d.toordinal()))  # 같은 거리면 나중 10-K
            new_fy = label[near] + round((fy_end - near).days / 365.25)
        else:
            offsets = [period_of_accn[a][1] - _fy_label_year(date.fromisoformat(period_of_accn[a][0])) for a in annual_report]
            new_fy = _fy_label_year(fy_end) + max(sorted(set(offsets)), key=offsets.count)
        out[accn] = (end, new_fy, new_q)
    taken = {(fy, q): end for accn, (end, fy, q) in period_of_accn.items() if accn not in bad}
    for accn in bad:
        end, fy, q = out[accn]
        if taken.get((fy, q), end) != end:
            out[accn] = period_of_accn[accn]
    return out


def edgar_records(facts: dict):
    period_of_accn = edgar_periods(facts)
    periods = {end: (fy, q) for end, fy, q in period_of_accn.values()}
    ends = sorted(periods)

    # 같은 (회계연도, 분기)에 종료일이 여럿 남으면(겹침) 값과 같이 늦은 종료일이 들어간다
    quarters = {periods[end]: {"year": periods[end][0], "q": periods[end][1], "end": end} for end in ends}
    annual = {}

    def latest(entries):
        """같은 기간 값이 여러 개면 우선순위 높은 항목명 → 가장 최근 제출본 사용.
        (_edgar_entries가 이미 그 순서로 정렬해 둠: 뒤에 오는 값이 이김)"""
        out = {}
        for e in entries:
            out[(e.get("start"), e["end"])] = e["val"]
        return out

    for key in ("current_assets", "total_liabilities"):
        values = {end: v for (_, end), v in latest(_edgar_entries(facts, "us-gaap", EDGAR_TAGS[key], "USD")).items()}
        for end in ends:
            quarters[periods[end]][key] = values.get(end)

    for key, unit in EDGAR_FLOW_UNITS.items():
        values = latest(e for e in _edgar_entries(facts, "us-gaap", EDGAR_TAGS[key], unit) if "start" in e)
        by_end = {}
        for (start, end), v in values.items():
            by_end.setdefault(end, []).append((_days({"start": start, "end": end}), start, v))

        for i, end in enumerate(ends):
            rec = quarters[periods[end]]
            candidates = by_end.get(end, [])
            q_val = next((v for d, _, v in candidates if 80 <= d <= 100), None)
            if q_val is None and i > 0:
                # 누적(YTD)값만 있으면 직전 분기 누적값을 빼서 계산
                for d, start, v in sorted(candidates, reverse=True):
                    prev = values.get((start, ends[i - 1]))
                    if d > 100 and prev is not None:
                        q_val = v - prev
                        break
            if key != "net_income" and q_val is None and candidates:
                q_val = 0
            rec[key] = q_val

            if rec["q"] == 4:
                fy_val = next((v for d, _, v in candidates if 350 <= d <= 380), None)
                annual.setdefault(rec["year"], {"year": rec["year"], "q": 4})[key] = fy_val

    # 발행주식수: 표지(dei)의 값을 해당 공시의 보고 기간에 연결 (주식 종류가 여러 개면 합산)
    shares_by_accn = {}
    for e in _edgar_entries(facts, "dei", ["EntityCommonStockSharesOutstanding"], "shares"):
        if e["accn"] in period_of_accn:
            shares_by_accn.setdefault(e["accn"], {})[e["end"]] = shares_by_accn.get(e["accn"], {}).get(e["end"], 0) + e["val"]
    for accn, by_date in shares_by_accn.items():
        end = period_of_accn[accn][0]
        quarters[periods[end]]["shares"] = by_date[max(by_date)]
        quarters[periods[end]]["shares_source"] = "표지"
    # 표지 값이 없는 분기(주식 종류별로만 적어 companyfacts에 안 잡히는 회사 등)는 그 분기 3개월 가중평균 주식수.
    # 표지 값과 둘 다 있는 분기로 비교하면 75%가 ±2% 안
    average = {e["end"]: e["val"] for e in _edgar_entries(facts, "us-gaap", ["WeightedAverageNumberOfSharesOutstandingBasic"], "shares")
               if "start" in e and 80 <= _days(e) <= 100 and e["val"] > 0}
    # 회사가 단위를 잘못 적은 값(TBLA 2,781억 주 등)은 시점이 가장 가까운 표지 값과 10배 넘게 다르면 버림
    cover = sorted((end, rec["shares"]) for end in ends if (rec := quarters[periods[end]]).get("shares_source") == "표지")
    for end in ends:
        rec = quarters[periods[end]]
        if rec.get("shares") is None and end in average:
            near = min(cover, key=lambda c: abs((date.fromisoformat(c[0]) - date.fromisoformat(end)).days), default=None)
            if near is None or near[1] <= 0 or 0.1 <= average[end] / near[1] <= 10:
                rec["shares"], rec["shares_source"] = average[end], "가중평균"

    for (year, q), rec in quarters.items():
        quarter_vals = [quarters.get((year, k), {}).get("buyback_shares") for k in range(1, q + 1)]
        rec["buyback_shares_ytd"] = None if None in quarter_vals else sum(quarter_vals)

    for rec in quarters.values():
        if rec["q"] == 4 and rec["year"] in annual:
            annual[rec["year"]].update({k: rec.get(k) for k in (*BALANCE_KEYS, "end")})

    return quarters, annual


# ---------------------------------------------------------------- 주식분할 탐지

SPLIT_MIN_RATIO = 1.5   # 이보다 작은 변화는 분할로 보지 않음
SPLIT_TOLERANCE = 0.03  # 배율이 정수(또는 x.5)에서 3% 이내면 분할/병합으로 판단


def _near_split_ratio(ratio):
    """1.5, 2, 2.5, 3, 4, 5, 10, 50 처럼 분할에 쓰이는 배율에 가까우면 그 값, 아니면 None.
    3배 미만은 0.5 단위(1.5, 2.5), 3배 이상은 정수만 인정 (13.29 → 13.5 같은 오탐 방지)."""
    nearest = round(ratio * 2) / 2 if ratio < 3 else float(round(ratio))
    return nearest if nearest >= SPLIT_MIN_RATIO and abs(ratio / nearest - 1) <= SPLIT_TOLERANCE else None


def detect_splits(rows: list[dict], id_keys: tuple[str, ...], period_key: str = "기간",
                  shares_key: str = "발행주식수") -> list[dict]:
    """분기표에서 연속된 두 분기 사이 발행주식수가 정수배로 바뀐 지점을 찾는다.

    rows: 분기표 행(회사 식별 열 + 기간 + 발행주식수). 회사별로 기간 순 정렬돼 있다고 가정하지 않음.
    """
    by_company = {}
    for r in rows:
        if r.get(shares_key) not in ("", None):
            by_company.setdefault(tuple(r[k] for k in id_keys), []).append(r)

    found = []
    for company, items in by_company.items():
        items.sort(key=lambda r: r[period_key])
        for prev, cur in zip(items, items[1:]):
            before, after = float(prev[shares_key]), float(cur[shares_key])
            if before <= 0 or after <= 0:
                continue
            ratio = after / before
            if (n := _near_split_ratio(ratio)) is not None:
                kind, label = "분할", f"{n:g}:1"
            elif (n := _near_split_ratio(1 / ratio)) is not None:
                kind, label = "병합", f"1:{n:g}"
            else:
                continue
            found.append({
                **dict(zip(id_keys, company)),
                "이전기간": prev[period_key], "이후기간": cur[period_key],
                "이전주식수": int(before), "이후주식수": int(after),
                "종류": kind, "배율": label, "실제배율": round(ratio, 4),
            })
    return found


# ---------------------------------------------------------------- 기간 평균 주가 (자사주매입 주식수 추정)

def period_vwap(bars: dict, end: date, months: int, min_months: int):
    """월봉 {'YYYY-MM': (VWAP, 거래량)}에서 종료일까지 months개월의 거래량 가중 평균 주가와 쓴 달 수.

    종료일이 15일 전이면(52·53주 회계연도의 10월 초 마감 등) 그 전달을 마지막 달로 본다.
    가격이 있는 달이 min_months개 미만이면 (None, 달 수).
    """
    year, month = end.year, end.month - (end.day < 15)
    used = []
    for _ in range(months):
        if month == 0:
            year, month = year - 1, 12
        if (bar := bars.get(f"{year}-{month:02d}")) and bar[1] > 0:
            used.append(bar)
        month -= 1
    if len(used) < min_months:
        return None, len(used)
    return sum(p * v for p, v in used) / sum(v for _, v in used), len(used)


# ---------------------------------------------------------------- 분석표

CAGR_YEARS = (3, 5, 10)


SHARES_CARRY_QUARTERS = 3  # 발행주식수가 없는 분기에 직전 값을 이어 쓰는 최대 분기 수


def quarterly_table(quarters: dict) -> list[dict]:
    rows, last_shares, last_source, carried = [], None, None, 0
    for year, q in sorted(quarters):
        rec = quarters[(year, q)]
        if rec.get("shares") is not None:
            last_shares, last_source, carried = rec["shares"], rec.get("shares_source"), 0
        elif last_shares is not None:
            carried += 1
            if carried > SHARES_CARRY_QUARTERS:  # 공시가 끊긴 옛 값을 계속 쓰면 시가총액이 틀림
                last_shares = last_source = None

        def ttm(y, qq, key):
            vals = [quarters.get(_prev_quarter(y, qq, k), {}).get(key) for k in range(4)]
            return None if None in vals else sum(vals)

        ni_ttm = ttm(year, q, "net_income")
        row = {"기간": f"{year}Q{q}", **({"종료일": rec["end"]} if "end" in rec else {})}  # 종료일은 EDGAR만
        row |= {
            "유동자산": rec.get("current_assets"),
            "총부채": rec.get("total_liabilities"),
            "발행주식수": last_shares,  # 해당 분기에 공시가 없으면 직전 값 사용 (최대 SHARES_CARRY_QUARTERS분기)
            **({"발행주식수_출처": last_source and (last_source if not carried else f"{last_source}, {carried}분기 전")}
               if "end" in rec else {}),  # EDGAR만
            "주당순유동자산": _ncav_per_share(rec.get("current_assets"), rec.get("total_liabilities"), last_shares),
            "순이익(분기)": rec.get("net_income"),
            "순이익(최근4분기)": ni_ttm,
        }
        for n in CAGR_YEARS:
            row.update(_growth_columns(ni_ttm, ttm(year - n, q, "net_income"), n))
        row["자사주매입(분기)"] = rec.get("buyback")
        row["자사주매입(최근4분기)"] = ttm(year, q, "buyback")
        row["자사주매입주식수(분기)"] = rec.get("buyback_shares")
        row["자사주매입주식수(연초누적)"] = rec.get("buyback_shares_ytd")
        row["자사주매입주식수_전년동기대비(%)"] = _growth(
            rec.get("buyback_shares_ytd"), quarters.get((year - 1, q), {}).get("buyback_shares_ytd"))
        rows.append(row)
    return rows


def annual_table(annual: dict) -> list[dict]:
    rows = []
    for year in sorted(annual):
        rec = annual[year]
        row = {"연도": year, **({"종료일": rec.get("end")} if "end" in rec else {})}
        row |= {
            "유동자산": rec.get("current_assets"),
            "총부채": rec.get("total_liabilities"),
            "발행주식수": rec.get("shares"),
            "주당순유동자산": _ncav_per_share(rec.get("current_assets"), rec.get("total_liabilities"), rec.get("shares")),
            "순이익": rec.get("net_income"),
        }
        for n in CAGR_YEARS:
            row.update(_growth_columns(rec.get("net_income"), annual.get(year - n, {}).get("net_income"), n))
        row["자사주매입"] = rec.get("buyback")
        row["자사주매입주식수"] = rec.get("buyback_shares")
        row["자사주매입주식수_전년대비(%)"] = _growth(
            rec.get("buyback_shares"), annual.get(year - 1, {}).get("buyback_shares"))
        rows.append(row)
    return rows


def annual_buyback_pairs(annual_rows: list[dict], year: int) -> dict:
    """연간 결과에서 {종목코드: (year 자사주매입 주식수, 전년 주식수)}."""
    by_code = {}
    for r in annual_rows:
        by_code.setdefault(r["종목코드"], {})[int(r["연도"])] = r
    return {code: (_num(years.get(year, {}).get("자사주매입주식수")),
                   _num(years.get(year - 1, {}).get("자사주매입주식수")))
            for code, years in by_code.items()}


def screen_net_net(rows: list[dict], period, market: dict[str, dict], max_ratio: float,
                   profit_years: int, profit_status: set[str], buyback_up: bool, buyback: dict | None = None):
    """연간 또는 분기 일괄 결과에서 넷넷 종목을 거른다.

    시가총액 ÷ (유동자산 - 총부채) ≤ max_ratio, 최근 profit_years년 순이익 상태가 profit_status 중 하나,
    (buyback_up이면) 자사주매입 주식수가 전년보다 많은 종목.
    rows: 연간표(연도 열, period는 연도) 또는 분기표(기간 열, period는 '2026Q2'. 순이익은 최근 4분기 합).
    buyback: {종목코드: (올해, 전년) 자사주매입 주식수}. 없으면 연간표 rows에서 계산.
    반환: (통과 행 목록, 단계별 남은 종목 수)
    """
    quarterly = isinstance(period, str)
    if buyback is None:
        buyback = annual_buyback_pairs(rows, period)
    current = {r["종목코드"]: r for r in rows if (r["기간"] if quarterly else int(r["연도"])) == period}

    stages = {"전체": 0, "순유동자산>0·주가있음": 0, f"비율≤{max_ratio:g}": 0, "순이익조건": 0, "자사주매입증가": 0}
    passed = []
    for code, r in current.items():
        stages["전체"] += 1
        ca, tl = _num(r["유동자산"]), _num(r["총부채"])
        m = market.get(code)
        if ca is None or tl is None or ca - tl <= 0 or not m:
            continue
        stages["순유동자산>0·주가있음"] += 1
        ncav = ca - tl
        ratio = m["mktcap"] / ncav
        if ratio > max_ratio:
            continue
        stages[f"비율≤{max_ratio:g}"] += 1
        status = r[f"순이익상태_{profit_years}년"]
        if status not in profit_status:
            continue
        stages["순이익조건"] += 1
        cur, prev = buyback.get(code, (None, None))
        if buyback_up:
            if cur is None or prev is None or cur <= prev:
                continue
            stages["자사주매입증가"] += 1
        passed.append({
            "종목코드": code, "회사명": r["회사명"], ("기간" if quarterly else "연도"): period,
            "종가": m["close"], "시가총액": m["mktcap"],
            "유동자산": ca, "총부채": tl, "순유동자산": ncav,
            "주당순유동자산": round(ncav / m["shares"], 2),
            "시총÷순유동자산": round(ratio, 3),
            "순이익": _num(r["순이익(최근4분기)" if quarterly else "순이익"]),
            f"순이익상태_{profit_years}년": status,
            f"순이익CAGR_{profit_years}년(%)": _num(r[f"순이익CAGR_{profit_years}년(%)"]),
            "자사주매입주식수": cur, "자사주매입주식수(전년)": prev,
        })
    if not buyback_up:
        del stages["자사주매입증가"]
    passed.sort(key=lambda x: x["시총÷순유동자산"])
    return passed, stages


# ---------------------------------------------------------------- EDGAR XBRL 원문 (자사주매입 주식수 보충)

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
EQUITY_AXES = {"StatementEquityComponentsAxis", "StatementClassOfStockAxis"}
PROGRAM_AXIS = "ShareRepurchaseProgramAxis"
TREASURY_BALANCE_TAGS = ["TreasuryStockShares", "TreasuryStockCommonShares"]
AXIS_ALIASES = {"StockRepurchaseProgramAxis": PROGRAM_AXIS}  # 같은 뜻의 다른 축 이름
PARENT_AXES = {"LegalEntityAxis", "ConsolidatedEntitiesAxis"}  # 값이 ParentCompanyMember면 연결 기준과 주식수가 같음
# 회사 자체 항목 이름 규칙: 매입·취득 + Shares, 누적·잔여·한도·평균가·세금 원천징수 등은 제외
CUSTOM_BUYBACK = re.compile(r"(Repurchas|Buyback|BuyBack|TreasuryStock.*Acquired|SharesAcquired|SharesPurchased)")
CUSTOM_EXCLUDE = re.compile(r"(Cumulative|Remaining|Authoriz|Available|Average|Price|Value|Amount|Cost|"
                            r"Withh|Tax|Vest|Forfeit|Award|Option|Percent|Number.*Program|Since|ToDate|"
                            r"Employee|Preferred|TemporaryEquity)")


def _xbrl_name(tag):
    namespace, _, local = tag[1:].partition("}")
    return ("us-gaap" if "fasb.org/us-gaap" in namespace else "custom"), local


def xbrl_buyback_facts(xml_bytes: bytes) -> dict:
    """XBRL 인스턴스에서 자사주매입 주식수 후보를 기간별로 뽑는다.

    companyfacts API에는 차원(열 구분)이 없는 표준 항목만 있어서, 자본변동표의 '자기주식' 열처럼
    차원이 붙은 값이나 회사 자체 항목은 빠진다. 여기서는 그런 값까지 모은다.
    반환: {"v": 3, "fy": 회계연도, "period_end": 보고기간 종료일,
           "facts": [{start, end, tier, rank, tag, dims, val}], "treasury": [{end, tag, dims, val}]}
      tier 0 차원 없음 / 1 자본변동표 열(자본 구성요소·주식 종류) / 2 매입 프로그램별(+자본변동표 열)
           3 그 밖의 차원 (표준 항목만)
      rank 0~2 표준 항목(EDGAR_TAGS 순서) / 3 회사 자체 항목
      treasury: 시점별 자기주식 잔액 주식수 (잔액 증감으로 매입량을 추정할 때 사용)
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)

    contexts = {}
    for c in root.iter(f"{XBRLI}context"):
        period = c.find(f"{XBRLI}period")
        start = period.findtext(f"{XBRLI}startDate")
        end = period.findtext(f"{XBRLI}endDate") or period.findtext(f"{XBRLI}instant")
        dims = {}
        for m in c.iter(f"{XBRLDI}explicitMember"):
            axis, member = m.get("dimension").split(":")[-1], (m.text or "").strip().split(":")[-1]
            if not (axis in PARENT_AXES and member == "ParentCompanyMember"):
                dims[AXIS_ALIASES.get(axis, axis)] = member
        typed = any(True for _ in c.iter(f"{XBRLDI}typedMember"))
        contexts[c.get("id")] = (start and start.strip(), end and end.strip(), dims, typed)

    fy = period_end = None
    facts, treasury = [], []
    standard = EDGAR_TAGS["buyback_shares"]
    for el in root:
        if not isinstance(el.tag, str) or not el.tag.startswith("{"):
            continue
        source, local = _xbrl_name(el.tag)
        if local == "DocumentFiscalYearFocus":
            fy = int(el.text.strip()[:4])
        elif local == "DocumentPeriodEndDate":
            period_end = el.text.strip()[:10]
        if "shares" not in (el.get("unitRef") or "").lower() or el.text is None:
            continue
        if source == "us-gaap" and local in TREASURY_BALANCE_TAGS:
            start, end, dims, typed = contexts.get(el.get("contextRef"), (None, None, {}, True))
            if not start and end and not typed and set(dims) <= EQUITY_AXES:
                try:
                    treasury.append({"end": end, "tag": local, "dims": dims, "val": abs(float(el.text.strip()))})
                except ValueError:
                    pass
            continue
        if source == "us-gaap" and local in standard:
            rank = standard.index(local)
        elif source == "custom" and CUSTOM_BUYBACK.search(local) and not CUSTOM_EXCLUDE.search(local):
            rank = 3
        else:
            continue
        start, end, dims, typed = contexts.get(el.get("contextRef"), (None, None, {}, True))
        if not start or typed:
            continue
        if not dims:
            tier = 0
        elif set(dims) <= EQUITY_AXES:
            tier = 1
        elif PROGRAM_AXIS in dims and set(dims) <= EQUITY_AXES | {PROGRAM_AXIS} and rank < 3:
            tier = 2
        elif rank < 3:
            tier = 3
        else:
            continue
        try:
            val = abs(float(el.text.strip()))
        except ValueError:
            continue
        facts.append({"start": start, "end": end, "tier": tier, "rank": rank,
                      "tag": local, "dims": dims, "val": val})
    return {"v": 3, "fy": fy, "period_end": period_end, "facts": facts, "treasury": treasury}


def pick_buyback_shares(facts: list[dict], start: str, end: str):
    """한 기간의 자사주매입 주식수 하나를 고른다. 반환: (값, 출처 설명) 또는 (None, None)

    차원 없음 → 자본변동표 열 → 매입 프로그램 순, 같은 단계에서는 표준 항목 우선.
    자본변동표는 같은 주식수를 여러 열(보통주, 자기주식 등)에 적으므로 열끼리는 최댓값,
    주식 종류(Class A/C 등)별로 나뉘어 있으면 종류끼리 합산. 매입 프로그램별 값은 합산.
    """
    # 캐시에 저장된 후보에도 최신 제외 규칙을 적용
    period = [f for f in facts if f["start"] == start and f["end"] == end and f["tier"] < 3
              and (f["rank"] < 3 or not CUSTOM_EXCLUDE.search(f["tag"]))]
    if not period:
        return None, None
    tier, rank = min((f["tier"], f["rank"]) for f in period)
    chosen = [f for f in period if f["tier"] == tier and f["rank"] == rank]
    tag = chosen[0]["tag"]
    if tier == 0:
        value = chosen[0]["val"]
    elif tier == 1:
        by_class = {}
        for f in chosen:
            cls = f["dims"].get("StatementClassOfStockAxis")
            by_class[cls] = max(by_class.get(cls, 0), f["val"])
        value = by_class[None] if None in by_class else sum(by_class.values())
    else:
        # 자본 구성요소 열끼리는 최댓값, 프로그램(×주식 종류)끼리는 합산.
        # 프로그램별 값과 프로그램×주식종류 값이 같이 있으면 프로그램별만 (이중 집계 방지)
        by_key = {}
        for f in chosen:
            key = tuple(sorted((a, m) for a, m in f["dims"].items() if a != "StatementEquityComponentsAxis"))
            by_key[key] = max(by_key.get(key, 0), f["val"])
        fewest = min(len(k) for k in by_key)
        value = sum(v for k, v in by_key.items() if len(k) == fewest)
    if int(value) == 0:  # 매입 금액이 있는데 0주(1주 미만 포함)는 다른 항목일 가능성 → 못 찾은 것으로
        return None, None
    label = ["차원없음", "자본변동표", "매입프로그램합"][tier]
    return int(value), f"XBRL {tag} ({label})"


def annual_buyback_from_xbrl(parsed: dict, fy: int | None = None) -> dict:
    """10-K 한 건에서 회계연도별(보통 3개년) 자사주매입 주식수. 반환: {회계연도: (값, 출처)}

    1년짜리 기간(350~380일)만 쓰고, 보고기간 종료일과의 차이(년)로 회계연도를 매긴다.
    fy: 이 10-K의 회계연도 (분기표와 맞추려고 edgar_periods 값을 넘김). 없으면 원문 표지 값.
    """
    fy = fy or _doc_fy(parsed)
    if not fy or not parsed.get("period_end"):
        return {}
    doc_end = date.fromisoformat(parsed["period_end"])
    out = {}
    for start, end in {(f["start"], f["end"]) for f in parsed["facts"]}:
        if not 350 <= _days({"start": start, "end": end}) <= 380:
            continue
        years_back = _years_back(doc_end, end)
        if years_back is None:
            continue
        value, source = pick_buyback_shares(parsed["facts"], start, end)
        if value is not None:
            out[fy - years_back] = (value, source)
    return out


def _doc_fy(parsed: dict):
    """10-K의 회계연도. 회사가 잘못 적은 경우(보고기간 종료 연도와 0·−1년 차이가 아님, 14,898건 중 15건)는
    종료일 3개월 전이 속한 연도로 다시 매긴다 (1월 초에 끝나는 52·53주 회계연도도 앞 연도가 되도록)."""
    if not parsed.get("fy") or not parsed.get("period_end"):
        return None
    end = date.fromisoformat(parsed["period_end"])
    if parsed["fy"] - end.year in (0, -1):
        return parsed["fy"]
    return end.year if end.month > 3 else end.year - 1


def quarter_buyback_from_xbrl(parsed: dict, end: str, fy: int, q: int) -> dict:
    """10-Q(또는 10-K) 한 건에서 분기 자사주매입 주식수 후보. 반환: {("3m"|"ytd", 회계연도, 분기): (값, 출처)}

    end·fy·q는 companyfacts 기준 이 공시의 보고기간 종료일·회계연도·분기(10-K는 4).
    이번 기간과 1년 전 비교 기간에서 3개월 값(80~100일)과 연초부터 누적값(분기 × 약 91일)을 읽는다.
    1분기는 3개월 값이 곧 누적값이다.
    """
    doc_end = date.fromisoformat(end)
    out = {}
    for start, period_end in {(f["start"], f["end"]) for f in parsed.get("facts", [])}:
        years_back = _years_back(doc_end, period_end)
        if years_back not in (0, 1):
            continue
        days = _days({"start": start, "end": period_end})
        kinds = []
        if 80 <= days <= 100:
            kinds.append("3m")
        if abs(days - q * 91.3) <= 20 or (q == 4 and 350 <= days <= 380):
            kinds.append("ytd")
        if not kinds:
            continue
        value, source = pick_buyback_shares(parsed["facts"], start, period_end)
        if value is not None:
            for kind in kinds:
                out[(kind, fy - years_back, q)] = (value, source)
    return out


def _years_back(doc_end: date, end: str):
    """보고기간 종료일에서 몇 년 전 연말인지. 연말과 20일 넘게 어긋나면 None."""
    days = (doc_end - date.fromisoformat(end)).days
    years_back = round(days / 365.25)
    return years_back if years_back >= 0 and abs(days - years_back * 365.25) <= 20 else None


def treasury_increase_from_xbrl(parsed: dict, fy: int | None = None) -> dict:
    """10-K 한 건의 자기주식 잔액(기말 − 기초)이 늘어난 회계연도별 증가 주식수. 반환: {회계연도: (값, 출처)}

    매입량의 추정치: 그 해에 소각하거나 직원 보상으로 다시 내준 주식이 있으면 실제 매입보다 작게 나온다.
    잔액이 줄었거나 그대로면 추정하지 않는다. 같은 항목으로 기초·기말이 다 있어야 한다.
    fy: annual_buyback_from_xbrl와 같음.
    """
    fy = fy or _doc_fy(parsed)
    if not fy or not parsed.get("treasury") or not parsed.get("period_end"):
        return {}
    doc_end = date.fromisoformat(parsed["period_end"])
    for tag in TREASURY_BALANCE_TAGS:
        balances = {}  # 몇 년 전 연말 → 주식수
        by_end = {}
        for f in parsed["treasury"]:
            if f["tag"] == tag:
                by_end.setdefault(f["end"], []).append(f)
        for end, items in by_end.items():
            k = _years_back(doc_end, end)
            if k is None:
                continue
            plain = [f["val"] for f in items if not f["dims"]]
            if plain:
                balances[k] = plain[0]
                continue
            by_class = {}  # 자본변동표 열끼리는 최댓값, 주식 종류끼리는 합산 (pick_buyback_shares와 같은 규칙)
            for f in items:
                cls = f["dims"].get("StatementClassOfStockAxis")
                by_class[cls] = max(by_class.get(cls, 0), f["val"])
            balances[k] = by_class[None] if None in by_class else sum(by_class.values())
        out = {}
        for k, end_bal in balances.items():
            if k + 1 in balances and int(end_bal - balances[k + 1]) > 0:
                out[fy - k] = (int(end_bal - balances[k + 1]), f"추정: XBRL {tag} 기말-기초")
        if out:
            return out
    return {}
