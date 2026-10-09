"""Alpaca Market Data API 수집기 (미국 주가).

무료 계정(https://alpaca.markets)을 만들면 키 ID와 시크릿 키를 받을 수 있다. 2016년부터 있다.
무료 요금제는 분당 200회, 최근 15분 SIP 데이터는 안 되므로 지난달까지만 받는다.
https://docs.alpaca.markets/reference/stockbars
"""
import hashlib
import json
import time
from pathlib import Path

import requests

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"


class AlpacaClient:
    def __init__(self, key_id: str, secret_key: str, cache_dir: Path | None = None, delay: float = 0.35):
        if not key_id or not secret_key:
            raise ValueError("ALPACA_KEY_ID, ALPACA_SECRET_KEY가 설정되지 않았습니다. https://alpaca.markets 에서 발급")
        self.delay = delay  # 분당 200회 한도 안쪽
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = requests.Session()
        self.session.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key})

    def _get(self, params: dict) -> dict:
        for attempt in range(5):
            try:
                resp = self.session.get(DATA_URL, params=params, timeout=60)
                if resp.status_code == 429:  # 한도 초과 → 쉬고 재시도
                    raise requests.ConnectionError("HTTP 429")
                if resp.status_code >= 400:
                    raise RuntimeError(f"Alpaca HTTP {resp.status_code}: {resp.text[:300]}")
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                time.sleep(2 ** (attempt + 2))
        time.sleep(self.delay)
        return resp.json()

    def bars(self, symbols: list[str], start: str, end: str, timeframe: str = "1Month",
             adjustment: str = "raw", feed: str = "sip") -> dict[str, list[dict]]:
        """여러 종목의 봉. 반환: {종목: [{t, o, h, l, c, v, n, vw}, ...]}

        adjustment: raw(분할·배당 미반영 원래 가격) / split(분할 반영) 등. start·end: YYYY-MM-DD.
        종목명은 조회일 기준 티커로 맞춰 준다(asof 기본값) → 이름을 바꾼 회사(FB → META)도 예전 가격이 이어진다.
        cache_dir를 주면 응답을 저장해 두고 다시 부르지 않는다 (end가 지난 기간만 넘길 것).
        """
        params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
                  "adjustment": adjustment, "feed": feed, "limit": 10000}
        cache = None
        if self.cache_dir:
            key = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
            cache = self.cache_dir / f"{start}_{end}_{timeframe}_{adjustment}_{key[:12]}.json"
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))

        out, token = {}, None
        while True:
            data = self._get({**params, **({"page_token": token} if token else {})})
            for symbol, bars in (data.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(bars)
            token = data.get("next_page_token")
            if not token:
                break
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(out), encoding="utf-8")
        return out
