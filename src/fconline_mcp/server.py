"""FC 온라인 전적 분석 MCP 서버.

Claude 같은 AI 어시스턴트가 도구 설명(docstring)을 읽고 어떤 도구를 부를지 스스로 고른다.
그래서 docstring은 '무엇을 돌려주는지'와 '언제 이 도구를 쓰고 언제 다른 도구를 쓰는지'를 적는다.
"""

from __future__ import annotations

import difflib
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import analysis
from .api import Lookup, NexonApiError, NexonClient

MAX_MATCHES = 30
DATA_NOTE = "넥슨 Open API 데이터는 매시 정각에 2시간 전까지의 경기가 반영됩니다."

mcp = MCPServer(
    name="fconline-mcp",
    instructions=(
        "FC 온라인 유저의 경기 기록을 넥슨 Open API로 조회한다. "
        "모든 수치는 실제 경기 데이터에서 나온 것만 사용하고, 선수 능력치(속력·슛 등)는 이 API에 없으므로 추측하지 않는다. "
        "선수를 추천할 때는 후보를 직접 지어내지 말고 get_position_trends로 실제로 쓰이는 선수를 모은 뒤, "
        "get_ranker_stats로 랭커 기록을 확인해 근거와 함께 말한다."
    ),
)

_client: NexonClient | None = None


def client() -> NexonClient:
    global _client
    if _client is None:
        _client = NexonClient()
    return _client


async def _lk() -> Lookup:
    return await client().lookup()


def _clamp(n: int) -> int:
    return max(1, min(int(n), MAX_MATCHES))


def _resolve_player(lk: Lookup, name: str) -> list[int]:
    ids = lk.ids_by_name.get(name.strip())
    if ids:
        return ids
    close = difflib.get_close_matches(name.strip(), list(lk.ids_by_name), n=5, cutoff=0.5)
    hint = f" 비슷한 이름: {', '.join(close)}" if close else ""
    raise NexonApiError(f"'{name}'라는 선수가 넥슨 선수 목록에 없습니다.{hint}")


async def _recent_details(nickname: str, match_type: str | int, n: int) -> tuple[str, int, list[dict]]:
    lk = await _lk()
    mt = lk.resolve_matchtype(match_type)
    ouid = await client().ouid(nickname)
    ids = await client().match_ids(ouid, mt, _clamp(n))
    return ouid, mt, await client().match_details(ids)


def _err(e: Exception) -> dict[str, Any]:
    return {"오류": str(e)}


@mcp.tool()
async def find_user(nickname: str) -> dict[str, Any]:
    """FC 온라인 닉네임으로 유저 기본 정보(레벨)와 매치 종류별 역대 최고 등급을 조회한다.
    다른 도구는 닉네임을 직접 받으므로, 유저가 존재하는지나 등급을 물을 때만 쓴다."""
    try:
        lk = await _lk()
        ouid = await client().ouid(nickname)
        basic = await client().user_basic(ouid)
        best = await client().max_division(ouid)
        return {
            "닉네임": basic["nickname"],
            "레벨": basic["level"],
            "역대 최고 등급": [
                {
                    "매치 종류": lk.matchtype_name.get(b["matchType"], b["matchType"]),
                    "등급": lk.division_name.get(b["division"], b["division"]),
                    "달성일": b["achievementDate"][:10],
                }
                for b in best
            ],
        }
    except (NexonApiError, KeyError) as e:
        return _err(e)


@mcp.tool()
async def get_recent_matches(nickname: str, match_type: str = "공식경기", limit: int = 10) -> dict[str, Any]:
    """최근 경기 목록을 한 줄씩(날짜, 승/무/패, 스코어, 상대, 점유율) 돌려준다. 최대 30경기.
    match_type 예: 공식경기, 감독모드, 클래식 1on1, 리그 친선.
    특정 경기를 골라 자세히 볼 때 match_id를 얻는 용도로 쓴다.
    승률·평균 득실점 같은 '요즘 폼'을 물으면 이 도구 대신 summarize_form을 쓴다."""
    try:
        ouid, mt, details = await _recent_details(nickname, match_type, limit)
        return {
            "매치 종류": (await _lk()).matchtype_name.get(mt, mt),
            "경기": [analysis.match_brief(d, ouid) for d in details],
            "참고": DATA_NOTE,
        }
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


@mcp.tool()
async def get_match_detail(nickname: str, match_id: str) -> dict[str, Any]:
    """경기 하나의 요약: 양 팀 점유율·슈팅·유효슈팅·패스/태클 성공률, 득점 기록(시간·득점자·도움),
    내 선발 라인업(선수·포지션·평점·골·도움). 선수별 세부 기록(드리블·공중볼 등)은 포함하지 않으며,
    필요하면 get_player_stats를 쓴다. match_id는 get_recent_matches에서 얻는다."""
    try:
        lk = await _lk()
        ouid = await client().ouid(nickname)
        detail = await client().match_detail(match_id)
        return analysis.match_summary(detail, ouid, lk)
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


@mcp.tool()
async def summarize_form(nickname: str, match_type: str = "공식경기", n: int = 10) -> dict[str, Any]:
    """최근 n경기(최대 30)를 집계한다: 전적, 승률, 경기당 득점·실점, 평균 점유율, 슈팅·유효슈팅,
    슈팅 대비 득점률, 패스성공률, 상대 슈팅 수, 자주 쓴 선수와 평균 평점.
    '요즘 폼 어때', '왜 자꾸 져', '실점이 많아?'처럼 여러 경기를 묶어 보는 질문에는
    get_match_detail을 여러 번 부르지 말고 이 도구를 먼저 쓴다."""
    try:
        ouid, mt, details = await _recent_details(nickname, match_type, n)
        lk = await _lk()
        return {
            "매치 종류": lk.matchtype_name.get(mt, mt),
            **analysis.form_summary(details, ouid, lk),
            "참고": DATA_NOTE,
        }
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


@mcp.tool()
async def get_player_stats(nickname: str, player_name: str, match_type: str = "공식경기", n: int = 10) -> dict[str, Any]:
    """유저가 최근 n경기(최대 30)에서 쓴 특정 선수의 경기당 평균 세부 기록을 카드·포지션별로 돌려준다.
    항목: 골, 도움, 슈팅, 유효슈팅, 패스·드리블·공중볼·볼경합·태클·블록 시도와 성공, 가로채기, 평균 평점,
    그리고 유효슈팅률·결정력·성공률 같은 비율. 선수의 강점과 약점을 판단할 때 쓴다.
    player_name은 한글 선수 이름(예: 손흥민). 같은 선수의 여러 시즌 카드가 있으면 모두 포함한다."""
    try:
        lk = await _lk()
        spids = set(_resolve_player(lk, player_name))
        ouid, mt, details = await _recent_details(nickname, match_type, n)
        rows = analysis.player_stats(details, ouid, spids, lk)
        if not rows:
            return {"결과": f"최근 {len(details)}경기({lk.matchtype_name.get(mt, mt)})에 {player_name} 선발 출전 기록이 없습니다."}
        return {"매치 종류": lk.matchtype_name.get(mt, mt), "조회 경기수": len(details), "선수 기록": rows}
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


@mcp.tool()
async def compare_with_rankers(nickname: str, player_name: str, match_type: str = "공식경기", n: int = 10) -> dict[str, Any]:
    """유저가 쓴 선수의 경기당 기록을, 같은 카드·같은 포지션을 쓴 랭커들의 평균 기록과 나란히 비교한다.
    비교 항목: 골, 도움, 슈팅, 유효슈팅, 패스 시도/성공, 드리블 시도/성공, 태클, 블록.
    '내 손흥민 잘 쓰고 있는 거야?', '랭커랑 뭐가 달라?' 같은 질문에 쓴다."""
    try:
        lk = await _lk()
        spids = set(_resolve_player(lk, player_name))
        ouid, mt, details = await _recent_details(nickname, match_type, n)
        mine = analysis.player_stats(details, ouid, spids, lk)
        if not mine:
            return {"결과": f"최근 {len(details)}경기에 {player_name} 선발 출전 기록이 없어 비교할 수 없습니다."}
        ranker = await client().ranker_stats(mt, [(m["spid"], m["position_code"]) for m in mine])
        by_key = {(r["spid"], r["spPosition"]): r for r in ranker}
        return {
            "매치 종류": lk.matchtype_name.get(mt, mt),
            "비교": [analysis.compare_to_ranker(m, by_key.get((m["spid"], m["position_code"])), lk) for m in mine],
            "참고": "랭커 기록은 넥슨이 집계한 상위 랭커들의 해당 카드·포지션 경기당 평균입니다.",
        }
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


@mcp.tool()
async def get_position_trends(
    nickname: str, position: str, match_type: str = "공식경기", n: int = 30, include_similar: bool = True
) -> dict[str, Any]:
    """유저의 최근 n경기(최대 30) 상대들이 특정 포지션(예: LW, ST, CDM)에 선발로 쓴 선수를
    사용 횟수·실제 포지션·평균 평점·경기당 골/도움과 함께 돌려준다. 비슷한 등급 유저들이 실제로 쓰는 선수 목록이다.
    'LW에 쓸 만한 선수 추천해줘'처럼 후보가 정해지지 않은 추천 질문에는 이 도구로 후보를 모은 뒤,
    상위 후보의 '선수명'을 get_ranker_stats에 넣어 랭커 기록으로 검증한다.
    include_similar=True(기본)면 비슷한 역할 포지션도 함께 센다(예: LW → LW·LF·LM·LAM).
    포메이션에 따라 LW 자리가 아예 없는 경우가 많기 때문이다. 표본이 적으면 결과에 주의가 붙는다."""
    try:
        lk = await _lk()
        pos = lk.resolve_position(position)
        name = lk.position_name.get(pos, str(pos))
        names = analysis.SIMILAR_POSITIONS.get(name, (name,)) if include_similar else (name,)
        codes = {lk.position_code[x] for x in names if x in lk.position_code} | {pos}
        ouid, mt, details = await _recent_details(nickname, match_type, n)
        out = analysis.position_trends(details, ouid, codes, lk)
        result = {
            "매치 종류": lk.matchtype_name.get(mt, mt),
            "포지션": "·".join(names),
            "조회 경기수": len(details),
            **out,
        }
        top_use = max((r["사용"] for r in out["선수"]), default=0)
        if top_use <= 2:
            result["주의"] = (
                f"가장 많이 쓰인 선수도 {top_use}회뿐이라 순위에는 의미가 없습니다. "
                "'실제로 쓰이는 후보 목록'으로만 보고, get_ranker_stats로 검증하세요."
            )
        return result
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


@mcp.tool()
async def get_ranker_stats(player_name: str, position: str, match_type: str = "공식경기", top: int = 5) -> dict[str, Any]:
    """특정 선수를 특정 포지션(예: ST, LW, CAM, CDM, CB, GK)에서 쓴 랭커들의 경기당 평균 기록을
    카드(시즌)별로 돌려준다. 랭커들이 실제로 쓴 카드만 나오며, 표본 경기수가 많은 순으로 top개.
    넥슨은 카드당 표본을 최대 20경기로 자르므로, 20경기는 '많이 쓰인다'는 뜻이지 순위 근거는 아니다.
    이 도구는 후보 이름을 받아 검증만 한다. 후보 목록이 없으면 먼저 get_position_trends를 쓴다.
    다른 선수를 추천하거나 후보끼리 비교할 때, 추천 전에 반드시 이 도구로 실제 기록을 확인한다.
    선수 이름이 넥슨 선수 목록에 없으면 오류와 비슷한 이름을 돌려준다(없는 선수를 추천하지 않기 위함)."""
    try:
        lk = await _lk()
        spids = _resolve_player(lk, player_name)
        pos = lk.resolve_position(position)
        mt = lk.resolve_matchtype(match_type)
        rows = await client().ranker_stats(mt, [(s, pos) for s in spids])
        used = [r for r in rows if r["status"].get("matchCount")]
        used.sort(key=lambda r: (-r["status"]["matchCount"], -r["status"].get("goal", 0)))
        if not used:
            return {"결과": f"랭커들이 {player_name}을(를) {lk.position_name.get(pos, pos)}에서 쓴 기록이 없습니다."}
        return {
            "매치 종류": lk.matchtype_name.get(mt, mt),
            "카드별 랭커 기록": [analysis.ranker_view(r, lk) for r in used[: max(1, min(top, 10))]],
        }
    except (NexonApiError, KeyError, ValueError) as e:
        return _err(e)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
