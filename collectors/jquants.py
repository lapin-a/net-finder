"""J-Quants API V2 (일본거래소) 수집기.

API 키: https://jpx-jquants.com 대시보드 → 설정 → API키. Free 플랜은 12주 늦은 데이터, 분당 5건.
"""
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

BASE_URL = "https://api.jquants.com/v2"


class JQuantsClient:
    def __init__(self, api_key: str, cache_dir: Path | None = None, delay: float = 12.5):
        """delay: 요청 간격(초). Free 플랜 분당 5건 → 12.5초."""
        if not api_key:
            raise ValueError("JQUANTS_API_KEY가 설정되지 않았습니다.")
        self.delay = delay
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key})
        self._last = 0.0

    def _get(self, path: str, **params) -> dict:
        for attempt in range(5):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                resp = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code == 429 and attempt < 4:  # 분당 한도 초과 → 1분 쉼
                time.sleep(60)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"J-Quants 오류 {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise RuntimeError("J-Quants 재시도 초과")

    def _paged(self, path: str, **params) -> list[dict]:
        rows, key = [], None
        while True:
            data = self._get(path, **params, **({"pagination_key": key} if key else {}))
            rows += data.get("data", [])
            key = data.get("pagination_key")
            if not key:
                return rows

    def daily(self, day: date) -> list[dict]:
        """그날 전 종목 일봉 (Code 5자리, C 종가, Va 거래대금, MktCap 백만 엔 = 종가 × 상장주식수 등).
        휴장일이면 빈 리스트. 빈 응답은 아직 안 풀린 날일 수 있어 최근 100일 안이면 저장하지 않음."""
        cache = self.cache_dir / f"daily/{day.isoformat()}.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        rows = self._paged("equities/bars/daily", date=day.isoformat())
        if cache and (rows or date.today() - day > timedelta(days=100)):
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    def last_trading_day(self, on_or_before: date) -> tuple[date, list[dict]]:
        """해당 날짜 또는 그 이전 가장 가까운, 자료가 있는 거래일과 그날 일봉 (Free 플랜은 12주 전까지만 있음)."""
        day = on_or_before
        for _ in range(20):
            if day.weekday() < 5:
                try:
                    rows = self.daily(day)
                except RuntimeError as e:  # 플랜 범위 밖: '… covers the following dates: 2024-08-10 ~ 2026-08-10'
                    m = re.search(r"covers the following dates: \S+ ~ (\d{4}-\d\d-\d\d)", str(e))
                    if not m or date.fromisoformat(m[1]) >= day:
                        raise
                    day = date.fromisoformat(m[1])
                    continue
                if rows:
                    return day, rows
            day -= timedelta(days=1)
        raise RuntimeError(f"{on_or_before} 이전 20일 안에 자료가 있는 거래일이 없습니다")

    def master(self) -> list[dict]:
        """상장 종목 목록 (Code, CoName, S33Nm 업종, MktNm 시장 등). 캐시하지 않음."""
        return self._paged("equities/master")
