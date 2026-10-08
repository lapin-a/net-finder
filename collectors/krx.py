"""KRX(한국거래소) Open API 수집기.

인증키 발급: https://openapi.krx.co.kr (서비스별 이용 신청 필요)
"""
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto"
PREFERRED_NAME = re.compile(r"\d?우[A-Z]?(\(전환\))?$")
MARKETS = {"KOSPI": "stk_bydd_trd", "KOSDAQ": "ksq_bydd_trd", "KONEX": "knx_bydd_trd"}


class KrxClient:
    def __init__(self, api_key: str, cache_dir: Path | None = None, delay: float = 0.2):
        if not api_key:
            raise ValueError("KRX_API_KEY가 설정되지 않았습니다.")
        self.delay = delay
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = requests.Session()
        self.session.headers.update({"AUTH_KEY": api_key})

    def daily(self, market: str, bas_dd: str) -> list[dict]:
        """시장별 일별매매정보 (종가, 시가총액, 상장주식수 등). bas_dd: YYYYMMDD. 휴장일이면 빈 리스트."""
        cache = self.cache_dir / f"{market}_{bas_dd}.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))

        for attempt in range(5):
            try:
                resp = self.session.get(f"{BASE_URL}/{MARKETS[market]}", params={"basDd": bas_dd}, timeout=60)
                resp.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                time.sleep(2 ** (attempt + 1))
        time.sleep(self.delay)
        rows = resp.json().get("OutBlock_1", [])

        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    def last_trading_day(self, on_or_before: date) -> tuple[str, list[dict]]:
        """해당 날짜 또는 그 이전 가장 가까운 거래일과 그날의 코스피 데이터."""
        day = on_or_before
        for _ in range(15):
            bas_dd = day.strftime("%Y%m%d")
            rows = self.daily("KOSPI", bas_dd)
            if rows:
                return bas_dd, rows
            day -= timedelta(days=1)
        raise RuntimeError(f"{on_or_before} 이전 15일 안에 거래일이 없습니다")

    def market_data(self, on_or_before: date) -> tuple[str, dict[str, dict]]:
        """전 시장 종목코드 → {종가, 시가총액, 상장주식수}. 시가총액·주식수는 보통주 + 해당 우선주 합계.

        우선주는 종목코드 앞 5자리가 같고 이름이 보통주 이름으로 시작하면 보통주에 합산한다.
        (예: 005930 삼성전자 + 005935 삼성전자우). 이름이 줄여 쓰였어도 앞 5자리가 같은 보통주가
        하나뿐이고 이름이 '우', '2우B' 등으로 끝나면 합산한다. 종가는 보통주 종가.
        """
        bas_dd, kospi = self.last_trading_day(on_or_before)
        rows = kospi + self.daily("KOSDAQ", bas_dd) + self.daily("KONEX", bas_dd)

        def item(r):
            return {"close": int(r["TDD_CLSPRC"]), "mktcap": int(r["MKTCAP"]), "shares": int(r["LIST_SHRS"])}

        commons = {r["ISU_CD"]: r for r in rows if r["ISU_CD"].endswith("0")}
        data = {code: item(r) for code, r in commons.items()}
        for r in rows:
            if r["ISU_CD"] in commons:
                continue
            same_prefix = [c for c in commons.values() if c["ISU_CD"][:5] == r["ISU_CD"][:5]]
            common = next((c for c in same_prefix if r["ISU_NM"].startswith(c["ISU_NM"])), None)
            if common is None and len(same_prefix) == 1 and PREFERRED_NAME.search(r["ISU_NM"]):
                common = same_prefix[0]  # 이름이 줄여 쓰인 우선주 (예: 남선알미늄 → 남선알미우)
            if common:
                data[common["ISU_CD"]]["mktcap"] += int(r["MKTCAP"])
                data[common["ISU_CD"]]["shares"] += int(r["LIST_SHRS"])
            else:
                data[r["ISU_CD"]] = item(r)
        return bas_dd, data

    def issued_shares(self, on_or_before: date) -> tuple[str, dict[str, int]]:
        """전 시장 종목코드 → 상장주식수 (보통주 + 해당 우선주 합계)."""
        bas_dd, data = self.market_data(on_or_before)
        return bas_dd, {code: d["shares"] for code, d in data.items()}
