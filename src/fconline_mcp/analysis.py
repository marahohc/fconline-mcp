"""매치 상세 원본(경기당 약 17KB)을 AI가 읽기 좋은 작은 요약으로 줄이는 순수 함수들.

원칙: 기본 응답에는 판단에 필요한 값만 남기고, 선수별 세부 기록은
get_player_stats처럼 따로 요청할 때만 꺼낸다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Iterable

from .api import Lookup

SUB_POSITION = 28
HALF_OFFSET_MIN = {0: 0, 1: 45, 2: 90, 3: 105, 4: 120}  # 전반/후반/연장전반/연장후반/승부차기
SMALL_SAMPLE = 10
# 비슷한 역할의 포지션 묶음. 실제 감독모드 20경기에서 상대의 LW·ST 선발이 0명이었고
# 대부분 LAM/CAM/RAM, LM/RM을 썼다. 정확히 같은 포지션만 세면 추천 후보가 비기 쉽다.
SIMILAR_POSITIONS = {
    "ST": ("ST", "LS", "RS", "CF"), "CF": ("CF", "ST", "LS", "RS"),
    "LW": ("LW", "LF", "LM", "LAM"), "RW": ("RW", "RF", "RM", "RAM"),
    "LM": ("LM", "LW", "LAM"), "RM": ("RM", "RW", "RAM"),
    "LAM": ("LAM", "LW", "LF", "LM"), "RAM": ("RAM", "RW", "RF", "RM"),
    "CAM": ("CAM", "CF"),
    "CM": ("CM", "LCM", "RCM"), "LCM": ("LCM", "CM", "RCM"), "RCM": ("RCM", "CM", "LCM"),
    "CDM": ("CDM", "LDM", "RDM"), "LDM": ("LDM", "CDM", "RDM"), "RDM": ("RDM", "CDM", "LDM"),
    "CB": ("CB", "LCB", "RCB", "SW"), "LCB": ("LCB", "CB", "RCB"), "RCB": ("RCB", "CB", "LCB"),
    "LB": ("LB", "LWB"), "LWB": ("LWB", "LB"), "RB": ("RB", "RWB"), "RWB": ("RWB", "RB"),
}
GOAL_RESULT = 3  # shootDetail.result: 3 = 골 (경기 데이터로 goalTotal과 대조해 확인)

# 경기 기록 중 선수 분석에 쓰는 항목 (경기당 평균으로 집계)
PLAYER_FIELDS = (
    "goal", "assist", "shoot", "effectiveShoot",
    "passTry", "passSuccess", "dribbleTry", "dribbleSuccess",
    "ballPossesionTry", "ballPossesionSuccess", "aerialTry", "aerialSuccess",
    "intercept", "defending", "tackleTry", "tackle", "blockTry", "block",
)
# 랭커 스탯 API가 돌려주는 항목 (비교는 이 교집합으로만 한다)
RANKER_FIELDS = (
    "goal", "assist", "shoot", "effectiveShoot",
    "passTry", "passSuccess", "dribbleTry", "dribbleSuccess", "tackle", "block",
)


def _r(x: float, nd: int = 2) -> float:
    return round(x, nd)


def _rate(success: float, total: float) -> float | None:
    return _r(success / total * 100, 1) if total else None


def split_sides(detail: dict, ouid: str) -> tuple[dict, dict | None]:
    infos = detail["matchInfo"]
    me = next((m for m in infos if m["ouid"] == ouid), None)
    if me is None:
        raise ValueError("이 경기에 해당 유저가 없습니다.")
    opp = next((m for m in infos if m["ouid"] != ouid), None)
    return me, opp


def goal_minute(goal_time: int) -> int:
    half = goal_time >> 24
    seconds = goal_time & 0xFFFFFF
    return HALF_OFFSET_MIN.get(half, 0) + seconds // 60


def _score(side: dict | None) -> int:
    return side["shoot"]["goalTotalDisplay"] if side else 0


def _starter_rating(side: dict) -> float | None:
    # 넥슨 matchDetail.averageRating은 선발 평점 합을 18명(교체 포함)으로 나눈 값이라
    # 게임 화면보다 훨씬 낮게 나온다(실제 경기 2건으로 확인). 선발만으로 다시 계산한다.
    ratings = [p["status"]["spRating"] for p in side.get("player", []) if p["spPosition"] != SUB_POSITION]
    return _r(mean(ratings)) if ratings else None


def _team_stats(side: dict) -> dict:
    s, p, d = side["shoot"], side["pass"], side["defence"]
    md = side["matchDetail"]
    return {
        "점유율": md["possession"],
        "슈팅": s["shootTotal"],
        "유효슈팅": s["effectiveShootTotal"],
        "패스성공률": _rate(p["passSuccess"], p["passTry"]),
        "스루패스성공률": _rate(p["throughPassSuccess"], p["throughPassTry"]),
        "태클성공률": _rate(d["tackleSuccess"], d["tackleTry"]),
        "선발 평균평점": _starter_rating(side),
    }


def match_brief(detail: dict, ouid: str) -> dict:
    """경기 목록용 한 줄 요약."""
    me, opp = split_sides(detail, ouid)
    brief = {
        "match_id": detail["matchId"],
        "날짜": detail["matchDate"],
        "결과": me["matchDetail"]["matchResult"],
        "스코어": f"{_score(me)}:{_score(opp)}",
        "상대": opp["nickname"] if opp else None,
        "점유율": me["matchDetail"]["possession"],
    }
    if me["shoot"].get("shootOutScore") or (opp and opp["shoot"].get("shootOutScore")):
        brief["승부차기"] = f"{me['shoot']['shootOutScore']}:{opp['shoot']['shootOutScore'] if opp else 0}"
    if me["matchDetail"]["matchEndType"] != 0:
        brief["비정상종료"] = True
    return brief


def _goals(side: dict | None, who: str, lk: Lookup) -> list[dict]:
    if not side:
        return []
    out = []
    for sh in side["shootDetail"]:
        if sh["result"] != GOAL_RESULT:
            continue
        g = {"분": goal_minute(sh["goalTime"]), "팀": who, "득점": lk.card_label(sh["spId"])}
        if sh.get("assist") and sh.get("assistSpId", -1) > 0:
            g["도움"] = lk.card_label(sh["assistSpId"])
        out.append(g)
    return out


def match_summary(detail: dict, ouid: str, lk: Lookup) -> dict:
    """경기 하나의 요약. 선수는 이름·포지션·평점·골·도움만 (세부 기록 제외)."""
    me, opp = split_sides(detail, ouid)
    lineup = [
        {
            "선수": lk.card_label(p["spId"]),
            "포지션": lk.position_name.get(p["spPosition"], p["spPosition"]),
            "평점": p["status"]["spRating"],
            "골": p["status"]["goal"],
            "도움": p["status"]["assist"],
        }
        for p in me["player"]
        if p["spPosition"] != SUB_POSITION
    ]
    goals = sorted(_goals(me, "나", lk) + _goals(opp, "상대", lk), key=lambda g: g["분"])
    return {
        **match_brief(detail, ouid),
        "내 팀": _team_stats(me),
        "상대 팀": _team_stats(opp) if opp else None,
        "득점 기록(분은 근사값)": goals,
        "내 선발 라인업": lineup,
    }


def form_summary(details: Iterable[dict], ouid: str, lk: Lookup) -> dict:
    """최근 n경기 집계: 승률, 득실점, 슈팅 효율, 자주 쓴 선수."""
    rows = []
    usage: dict[tuple[int, int], list[float]] = defaultdict(list)
    for d in details:
        me, opp = split_sides(d, ouid)
        rows.append(
            {
                "result": me["matchDetail"]["matchResult"],
                "gf": _score(me),
                "ga": _score(opp),
                "poss": me["matchDetail"]["possession"],
                "shots": me["shoot"]["shootTotal"],
                "on": me["shoot"]["effectiveShootTotal"],
                "pass_try": me["pass"]["passTry"],
                "pass_ok": me["pass"]["passSuccess"],
                "opp_shots": opp["shoot"]["shootTotal"] if opp else 0,
            }
        )
        for p in me["player"]:
            if p["spPosition"] != SUB_POSITION:
                usage[(p["spId"], p["spPosition"])].append(p["status"]["spRating"])
    if not rows:
        return {"경기수": 0}
    n = len(rows)
    wins = sum(r["result"] == "승" for r in rows)
    draws = sum(r["result"] == "무" for r in rows)
    shots = sum(r["shots"] for r in rows)
    goals = sum(r["gf"] for r in rows)
    top = sorted(usage.items(), key=lambda kv: (-len(kv[1]), -mean(kv[1])))[:11]
    return {
        "경기수": n,
        "전적": f"{wins}승 {draws}무 {n - wins - draws}패",
        "승률": _r(wins / n * 100, 1),
        "경기당 득점": _r(goals / n),
        "경기당 실점": _r(sum(r["ga"] for r in rows) / n),
        "평균 점유율": _r(mean(r["poss"] for r in rows), 1),
        "경기당 슈팅": _r(shots / n),
        "경기당 유효슈팅": _r(sum(r["on"] for r in rows) / n),
        "슈팅 대비 득점률": _rate(goals, shots),
        "패스성공률": _rate(sum(r["pass_ok"] for r in rows), sum(r["pass_try"] for r in rows)),
        "경기당 상대 슈팅": _r(sum(r["opp_shots"] for r in rows) / n),
        "자주 쓴 선수(출전수·평균평점)": [
            {
                "선수": lk.card_label(spid),
                "포지션": lk.position_name.get(pos, pos),
                "출전": len(ratings),
                "평균평점": _r(mean(ratings)),
            }
            for (spid, pos), ratings in top
        ],
    }


def player_stats(details: Iterable[dict], ouid: str, spids: set[int], lk: Lookup) -> list[dict]:
    """해당 선수(여러 카드 가능)의 경기당 평균 세부 기록. 카드·포지션별로 나눠서 집계."""
    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for d in details:
        me, _ = split_sides(d, ouid)
        for p in me["player"]:
            if p["spId"] in spids and p["spPosition"] != SUB_POSITION:
                groups[(p["spId"], p["spPosition"])].append(p["status"])
    out = []
    for (spid, pos), stats in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(stats)
        avg = {f: _r(sum(s.get(f, 0) for s in stats) / n) for f in PLAYER_FIELDS}
        out.append(
            {
                "spid": spid,
                "선수": lk.card_label(spid),
                "포지션": lk.position_name.get(pos, pos),
                "position_code": pos,
                "출전": n,
                "평균평점": _r(mean(s["spRating"] for s in stats)),
                "경기당 평균": avg,
                "비율(%)": {
                    "유효슈팅률": _rate(avg["effectiveShoot"], avg["shoot"]),
                    "결정력(골/슈팅)": _rate(avg["goal"], avg["shoot"]),
                    "패스성공률": _rate(avg["passSuccess"], avg["passTry"]),
                    "드리블성공률": _rate(avg["dribbleSuccess"], avg["dribbleTry"]),
                    "공중볼성공률": _rate(avg["aerialSuccess"], avg["aerialTry"]),
                    "태클성공률": _rate(avg["tackle"], avg["tackleTry"]),
                },
            }
        )
    return out


def position_trends(details: Iterable[dict], ouid: str, positions: set[int], lk: Lookup, top: int = 10) -> dict:
    """최근 경기 상대들이 해당 포지션(들)에 선발로 쓴 선수 집계 (비슷한 등급 유저들의 실제 선택)."""
    groups: dict[int, list[dict]] = defaultdict(list)
    used_pos: dict[int, Counter] = defaultdict(Counter)
    for d in details:
        _, opp = split_sides(d, ouid)
        for p in (opp or {}).get("player", []):
            if p["spPosition"] in positions:
                groups[p["spId"]].append(p["status"])
                used_pos[p["spId"]][lk.position_name.get(p["spPosition"], p["spPosition"])] += 1
    total = sum(len(v) for v in groups.values())
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), -mean(s["spRating"] for s in kv[1])))
    return {
        "표본(상대 선발 수)": total,
        "선수": [
            {
                "선수": lk.card_label(spid),
                "선수명": lk.player_name.get(spid, str(spid)),
                "사용": len(st),
                "실제 포지션": dict(used_pos[spid]),
                "평균평점": _r(mean(s["spRating"] for s in st)),
                "경기당 골": _r(sum(s.get("goal", 0) for s in st) / len(st)),
                "경기당 도움": _r(sum(s.get("assist", 0) for s in st) / len(st)),
            }
            for spid, st in ranked[:top]
        ],
    }


def ranker_view(row: dict, lk: Lookup) -> dict:
    st = row["status"]
    return {
        "선수": lk.card_label(row["spid"]),
        "포지션": lk.position_name.get(row["spPosition"], row["spPosition"]),
        "랭커 표본 경기수": st.get("matchCount"),
        "랭커 경기당 평균": {f: _r(st.get(f, 0)) for f in RANKER_FIELDS},
    }


def compare_to_ranker(mine: dict, ranker_row: dict | None, lk: Lookup) -> dict:
    out = {"선수": mine["선수"], "포지션": mine["포지션"], "내 출전": mine["출전"]}
    if not ranker_row or not ranker_row["status"].get("matchCount"):
        out["랭커 데이터"] = "없음 (랭커들이 이 카드·포지션을 거의 쓰지 않음)"
        out["내 경기당 평균"] = {f: mine["경기당 평균"][f] for f in RANKER_FIELDS}
        return out
    st = ranker_row["status"]
    out["랭커 표본 경기수"] = st["matchCount"]
    if st["matchCount"] < SMALL_SAMPLE:
        out["주의"] = f"랭커 표본이 {st['matchCount']}경기뿐이라 비교 신뢰도가 낮습니다."
    out["비교(내 기록 / 랭커 평균)"] = {
        f: {"나": mine["경기당 평균"][f], "랭커": _r(st.get(f, 0))} for f in RANKER_FIELDS
    }
    return out
