"""넥슨 Open API(FC 온라인) 호출과 캐시.

- API 키는 환경변수 NEXON_API_KEY에서 읽는다 (.env 지원). 코드나 저장소에 넣지 않는다.
- 메타데이터는 디스크에 7일 캐시한다. 넥슨 약관상 받아 둔 데이터는 30일 안에 갱신해야 하므로
  그보다 짧게 잡았다.
- ouid는 게임 콘텐츠 변경으로 바뀔 수 있다고 문서에 적혀 있어 메모리에만 10분 캐시한다.
- 매치 상세는 끝난 경기라 바뀌지 않으므로 프로세스가 살아 있는 동안만 메모리에 둔다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청마다 찍히는 로그 끄기

BASE_URL = "https://open.api.nexon.com"
META_TTL_SECONDS = 7 * 24 * 3600
OUID_TTL_SECONDS = 10 * 60
RANKER_BATCH = 20  # 직접 확인: 20개까지는 되고 40개는 OPENAPI00004 오류
# 응답 헤더 x-ratelimit-limit: 5 (초당 5건). 동시 개수만 막으면 응답이 빨라서 초당 수십 건이 나가
# 30경기 조회에서 429가 쏟아졌다. 요청 시작 간격을 벌려 초당 4건 정도로 맞춘다.
MIN_INTERVAL_SECONDS = 0.25
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)  # 429/5xx 재시도 대기. 마지막 시도 뒤에는 기다리지 않는다.

# seasonid.json은 2026-11-19 종료 예정 공지 → class.json 사용
META_FILES = ("spid", "class", "spposition", "matchtype", "division")


class NexonApiError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class NexonClient:
    def __init__(self, api_key: str | None = None, cache_dir: Path | None = None):
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # 프로젝트 루트의 .env
        load_dotenv()  # 실행 위치의 .env (있으면)
        self.api_key = api_key or os.environ.get("NEXON_API_KEY", "")
        if not self.api_key:
            raise NexonApiError(
                "NEXON_API_KEY가 없습니다. .env 파일이나 환경변수에 넥슨 Open API 키를 넣어 주세요."
            )
        self.cache_dir = cache_dir or Path(os.environ.get("FCONLINE_MCP_CACHE", Path.home() / ".fconline-mcp"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"x-nxopen-api-key": self.api_key},
            timeout=15.0,
        )
        self._sem = asyncio.Semaphore(2)  # 개발 단계 키는 호출 제한이 빡빡해서 동시 2개로 제한 (4개에서 429 다수 확인)
        self._rate_lock = asyncio.Lock()
        self._next_slot = 0.0  # 다음 요청을 보내도 되는 시각 (time.monotonic 기준)
        self._ouid: dict[str, tuple[float, str]] = {}
        self._match: dict[str, dict] = {}
        self._meta: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- 저수준 호출 ----------
    async def _wait_for_slot(self) -> None:
        """요청 시작 간격을 MIN_INTERVAL_SECONDS 이상으로 유지한다 (초당 호출 제한 대응)."""
        async with self._rate_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + MIN_INTERVAL_SECONDS
        if wait > 0:
            await asyncio.sleep(wait)

    async def _get(self, path: str, params: dict | None = None) -> Any:
        last_err, code = "", None
        for attempt in range(len(RETRY_DELAYS) + 1):
            async with self._sem:
                await self._wait_for_slot()
                resp = await self._http.get(path, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(RETRY_DELAYS[attempt] * random.uniform(1.0, 1.5))
                continue
            try:
                err = resp.json().get("error", {})
                code = err.get("name")
                last_err = f"{err.get('name', resp.status_code)}: {err.get('message', resp.text[:200])}"
            except ValueError:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            break
        raise NexonApiError(f"넥슨 API 호출 실패 ({path}) - {last_err}", code)

    # ---------- 메타데이터 ----------
    async def meta(self, name: str) -> Any:
        if name in self._meta:
            return self._meta[name]
        path = self.cache_dir / f"{name}.json"
        if path.exists() and time.time() - path.stat().st_mtime < META_TTL_SECONDS:
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = await self._get(f"/static/fconline/meta/{name}.json")
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self._meta[name] = data
        return data

    async def lookup(self) -> "Lookup":
        spid, klass, pos, mtype, div = await asyncio.gather(*(self.meta(n) for n in META_FILES))
        return Lookup(spid, klass, pos, mtype, div)

    # ---------- 계정 / 매치 / 랭커 ----------
    async def ouid(self, nickname: str) -> str:
        hit = self._ouid.get(nickname)
        if hit and time.time() - hit[0] < OUID_TTL_SECONDS:
            return hit[1]
        try:
            data = await self._get("/fconline/v1/id", {"nickname": nickname})
        except NexonApiError as e:
            if e.code == "OPENAPI00004":
                raise NexonApiError(f"'{nickname}' 닉네임을 찾을 수 없습니다.", e.code) from e
            raise
        ouid = data["ouid"]
        self._ouid[nickname] = (time.time(), ouid)
        return ouid

    async def user_basic(self, ouid: str) -> dict:
        return await self._get("/fconline/v1/user/basic", {"ouid": ouid})

    async def max_division(self, ouid: str) -> list[dict]:
        return await self._get("/fconline/v1/user/maxdivision", {"ouid": ouid})

    async def match_ids(self, ouid: str, matchtype: int, limit: int, offset: int = 0) -> list[str]:
        return await self._get(
            "/fconline/v1/user/match",
            {"ouid": ouid, "matchtype": matchtype, "offset": offset, "limit": limit},
        )

    async def match_detail(self, match_id: str) -> dict:
        if match_id not in self._match:
            self._match[match_id] = await self._get("/fconline/v1/match-detail", {"matchid": match_id})
        return self._match[match_id]

    async def match_details(self, match_ids: list[str]) -> list[dict]:
        return list(await asyncio.gather(*(self.match_detail(m) for m in match_ids)))

    async def ranker_stats(self, matchtype: int, players: list[tuple[int, int]]) -> list[dict]:
        """players = [(spid, position), ...]. 한 번에 20개씩 나눠 호출한다."""
        out: list[dict] = []
        for i in range(0, len(players), RANKER_BATCH):
            chunk = players[i : i + RANKER_BATCH]
            try:
                out.extend(await self._ranker_call(matchtype, chunk))
            except NexonApiError as e:
                # 카드 하나만 잘못돼도(예: 랭커 집계 대상이 아닌 카드) 묶음 전체가 400이 된다.
                # 그럴 땐 하나씩 다시 불러서 잘못된 카드만 건너뛴다.
                if e.code != "OPENAPI00004":
                    raise
                for one in chunk:
                    try:
                        out.extend(await self._ranker_call(matchtype, [one]))
                    except NexonApiError as e2:
                        if e2.code != "OPENAPI00004":
                            raise
        return out

    async def _ranker_call(self, matchtype: int, chunk: list[tuple[int, int]]) -> list[dict]:
        body = json.dumps([{"id": s, "po": p} for s, p in chunk])
        return await self._get("/fconline/v1/ranker-stats", {"matchtype": matchtype, "players": body})


class Lookup:
    """메타데이터를 이름 ↔ 코드로 바꾸는 표."""

    def __init__(self, spid, klass, pos, mtype, div):
        self.player_name = {x["id"]: x["name"] for x in spid}
        self.ids_by_name: dict[str, list[int]] = {}
        for x in spid:
            self.ids_by_name.setdefault(x["name"], []).append(x["id"])
        self.class_name = {x["class"]: x["className"] for x in klass}
        self.position_name = {x["spposition"]: x["desc"] for x in pos}
        self.position_code = {v: k for k, v in self.position_name.items()}
        self.matchtype_name = {x["matchtype"]: x["desc"] for x in mtype}
        self.matchtype_code = {v: k for k, v in self.matchtype_name.items()}
        self.division_name = {x["divisionId"]: x["divisionName"] for x in div}

    def card_label(self, spid: int) -> str:
        """'손흥민 (클래스명)' 형태. 클래스는 spid 앞 3자리."""
        name = self.player_name.get(spid, f"알 수 없는 선수({spid})")
        klass = self.class_name.get(spid // 1_000_000)
        if not klass:
            return name
        return f"{name} ({klass.split(' (')[0]})"  # 'GRU (Greatest Runner-Ups)' → 'GRU' 로 줄임

    def resolve_matchtype(self, value: str | int) -> int:
        if isinstance(value, int) or str(value).isdigit():
            return int(value)
        if value in self.matchtype_code:
            return self.matchtype_code[value]
        options = ", ".join(self.matchtype_code)
        raise NexonApiError(f"매치 종류 '{value}'를 모르겠습니다. 가능한 값: {options}")

    def resolve_position(self, value: str | int) -> int:
        if isinstance(value, int) or str(value).isdigit():
            return int(value)
        key = str(value).upper()
        if key in self.position_code:
            return self.position_code[key]
        raise NexonApiError(f"포지션 '{value}'를 모르겠습니다. 예: ST, LW, CAM, CB, GK")
