"""SEC EDGAR 수집기.

API 키는 필요 없지만 User-Agent에 이름과 이메일을 반드시 넣어야 하며,
초당 10회 이하로 요청해야 합니다. https://www.sec.gov/os/accessing-edgar-data
"""
import time

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DATA_URL = "https://data.sec.gov"


class EdgarClient:
    def __init__(self, user_agent: str, delay: float = 0.15):
        if not user_agent:
            raise ValueError("EDGAR_USER_AGENT가 설정되지 않았습니다. 예: 'Your Name you@example.com'")
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

    def _get_json(self, url: str) -> dict:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        time.sleep(self.delay)
        return resp.json()

    def tickers(self) -> list[dict]:
        """전체 티커 목록 (ticker, name, cik)."""
        return [
            {"ticker": row["ticker"], "name": row["title"], "cik": str(row["cik_str"]).zfill(10)}
            for row in self._get_json(TICKERS_URL).values()
        ]

    def cik_for_ticker(self, ticker: str) -> str:
        ticker = ticker.upper()
        for row in self.tickers():
            if row["ticker"] == ticker:
                return row["cik"]
        raise KeyError(f"티커를 찾을 수 없습니다: {ticker}")

    def submissions(self, cik: str) -> dict:
        """회사 정보 및 최근 공시 목록."""
        return self._get_json(f"{DATA_URL}/submissions/CIK{cik.zfill(10)}.json")

    def filings(self, cik: str, form_types: tuple[str, ...] = ()) -> list[dict]:
        """최근 공시 목록을 행 단위 dict 리스트로 변환."""
        recent = self.submissions(cik)["filings"]["recent"]
        rows = [dict(zip(recent.keys(), values)) for values in zip(*recent.values())]
        if form_types:
            rows = [r for r in rows if r["form"] in form_types]
        return rows

    def company_facts(self, cik: str) -> dict:
        """XBRL 재무 데이터 전체 (us-gaap 등)."""
        return self._get_json(f"{DATA_URL}/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json")
