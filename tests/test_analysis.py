"""실제 API 없이 요약 로직만 검증한다. 데이터는 실제 응답 구조를 본뜬 가짜 값."""

from fconline_mcp import analysis
from fconline_mcp.api import Lookup

LK = Lookup(
    spid=[{"id": 101000001, "name": "가상 공격수"}, {"id": 101000002, "name": "가상 수비수"}],
    klass=[{"class": 101, "className": "TEST (Test Season)"}],
    pos=[{"spposition": 25, "desc": "ST"}, {"spposition": 5, "desc": "CB"}, {"spposition": 28, "desc": "SUB"}],
    mtype=[{"matchtype": 50, "desc": "공식경기"}],
    div=[{"divisionId": 800, "divisionName": "슈퍼 챔피언스"}],
)


def status(**kw):
    base = {f: 0 for f in analysis.PLAYER_FIELDS}
    base.update(spRating=6.0)
    base.update(kw)
    return base


def side(ouid, result, goals, shots, players, shoot_detail=()):
    return {
        "ouid": ouid,
        "nickname": ouid.upper(),
        "matchDetail": {"matchResult": result, "matchEndType": 0, "possession": 50, "averageRating": 6.0},
        "shoot": {"shootTotal": shots, "effectiveShootTotal": goals, "goalTotal": goals,
                  "goalTotalDisplay": goals, "shootOutScore": 0},
        "shootDetail": list(shoot_detail),
        "pass": {"passTry": 100, "passSuccess": 80, "throughPassTry": 10, "throughPassSuccess": 5},
        "defence": {"tackleTry": 10, "tackleSuccess": 7},
        "player": players,
    }


def match(mid, my_result, my_goals, opp_goals):
    me_players = [
        {"spId": 101000001, "spPosition": 25, "status": status(goal=my_goals, shoot=3, effectiveShoot=2)},
        {"spId": 101000002, "spPosition": 5, "status": status(tackleTry=4, tackle=3)},
        {"spId": 101000002, "spPosition": 28, "status": status()},  # 교체 명단은 집계 제외
    ]
    goals = [{"result": 3, "goalTime": (1 << 24) + 600, "spId": 101000001, "assist": False, "assistSpId": -1}] * my_goals
    opp_result = {"승": "패", "패": "승", "무": "무"}[my_result]
    return {
        "matchId": mid,
        "matchDate": "2026-11-16T00:00:00",
        "matchInfo": [
            side("me", my_result, my_goals, 5, me_players, goals),
            side("opp", opp_result, opp_goals, 4, []),
        ],
    }


def test_goal_minute_second_half():
    assert analysis.goal_minute(16777883) == 45 + 667 // 60  # 실제 데이터 값: 후반 11분대


def test_brief_and_summary():
    m = match("m1", "승", 2, 1)
    brief = analysis.match_brief(m, "me")
    assert brief["스코어"] == "2:1" and brief["상대"] == "OPP"
    summary = analysis.match_summary(m, "me", LK)
    assert len(summary["내 선발 라인업"]) == 2  # SUB 제외
    assert summary["득점 기록(분은 근사값)"][0]["분"] == 55
    assert summary["내 선발 라인업"][0]["선수"] == "가상 공격수 (TEST)"


def test_team_rating_uses_starters_not_nexon_average():
    # 실제 경기(0:5)에서 넥슨 averageRating=3.29는 선발 11명 평점 합 59.3을 18로 나눈 값이었다.
    m = match("m1", "승", 1, 0)
    me = m["matchInfo"][0]
    me["player"][0]["status"]["spRating"] = 7.0
    me["player"][1]["status"]["spRating"] = 5.0
    me["player"][2]["status"]["spRating"] = 0  # 교체 명단
    me["matchDetail"]["averageRating"] = 12.0 / 18
    summary = analysis.match_summary(m, "me", LK)
    assert summary["내 팀"]["선발 평균평점"] == 6.0


def test_form_summary():
    ms = [match("a", "승", 2, 0), match("b", "무", 1, 1), match("c", "패", 0, 3)]
    f = analysis.form_summary(ms, "me", LK)
    assert f["전적"] == "1승 1무 1패"
    assert f["경기당 득점"] == 1.0 and f["경기당 실점"] == 1.33
    assert f["슈팅 대비 득점률"] == 20.0  # 3골 / 15슈팅


def test_player_stats_only_selected_player():
    ms = [match("a", "승", 2, 0), match("b", "패", 0, 1)]
    rows = analysis.player_stats(ms, "me", {101000002}, LK)
    assert len(rows) == 1 and rows[0]["포지션"] == "CB" and rows[0]["출전"] == 2
    assert rows[0]["비율(%)"]["태클성공률"] == 75.0


def test_compare_small_sample_warning():
    mine = analysis.player_stats([match("a", "승", 1, 0)], "me", {101000001}, LK)[0]
    row = {"spid": 101000001, "spPosition": 25, "status": {"matchCount": 3, "goal": 1.0}}
    out = analysis.compare_to_ranker(mine, row, LK)
    assert "주의" in out and out["비교(내 기록 / 랭커 평균)"]["goal"] == {"나": 1.0, "랭커": 1.0}


def test_position_trends_counts_opponents_only():
    ms = []
    for i, rating in enumerate([8.0, 6.0, 7.0]):
        m = match(f"t{i}", "승", 1, 0)
        opp_spid = 101000001 if i < 2 else 101000002
        m["matchInfo"][1]["player"] = [
            {"spId": opp_spid, "spPosition": 25, "status": status(spRating=rating, goal=1)},
            {"spId": 101000002, "spPosition": 5, "status": status()},  # 다른 포지션은 제외
            {"spId": 101000001, "spPosition": 28, "status": status()},  # 교체 명단 제외
        ]
        ms.append(m)
    out = analysis.position_trends(ms, "me", {25}, LK)
    assert out["표본(상대 선발 수)"] == 3
    top = out["선수"][0]
    assert top["선수"] == "가상 공격수 (TEST)" and top["선수명"] == "가상 공격수"
    assert top["사용"] == 2 and top["평균평점"] == 7.0 and top["경기당 골"] == 1.0
    # 내 팀 ST(가상 공격수)는 세지 않는다: 총합이 상대 3명분이어야 함
    assert sum(r["사용"] for r in out["선수"]) == 3
