"""선수 이름 후보 찾기. 이름과 카드 수는 실제 넥슨 선수 목록에서 가져온 값."""

from fconline_mcp.api import Lookup


def make_lookup(cards: dict[str, int]) -> Lookup:
    spid, i = [], 100000000
    for name, n in cards.items():
        for _ in range(n):
            i += 1
            spid.append({"id": i, "name": name})
    return Lookup(spid=spid, klass=[], pos=[], mtype=[], div=[])


LK = make_lookup({
    "손흥민": 51, "송홍민": 1, "홍진민": 1, "홍성민": 1, "손정민": 1, "신형민": 6,
    "모하메드 살라": 30, "칼라": 1, "솔라": 1, "해리 케인": 40, "케이얀": 1,
})


def test_typo_prefers_jamo_similarity_then_card_count():
    # 글자 단위 difflib은 홍진민·홍성민·송홍민 다음에 손흥민을 4번째로 냈다.
    # 자모로는 손흥민·송홍민이 동점이고, 카드가 많은 손흥민이 앞에 온다.
    assert LK.similar_names("손홍민")[0] == "손흥민"


def test_single_word_matches_full_name_first():
    assert LK.similar_names("살라")[0] == "모하메드 살라"
    assert LK.similar_names("케인")[0] == "해리 케인"


def test_no_suggestion_for_unrelated():
    assert LK.similar_names("zzzz") == []
