"""旧版账本兼容：升级后老查询出现在本人历史中（业务场景覆盖）。

数据来源：``tests/fixtures/ledger.json`` 由升级前版本（git a61b8d2）的
真实服务代码写出，是一份扁平 queries、无 history/pools/adjustments 分区、
记录无 created_at 的旧账本；``legacy-expected.json`` 记录了升级前各查询的
原始加噪响应与预算账实，用于断言重放结果不变、不重复扣费。

覆盖业务：
1. 不加时间范围：老记录出现在当初提交它的用户本人历史里，且不串到别人；
2. 指定任意时间范围：无提交时间的老记录必须被排除，不塞进任何时间段；
3. 反复加载/重启：迁移补录幂等，不重复补、预算账实不变；
4. 老查询仍可原样重放：DP 载荷逐字段一致、不二次扣预算；
5. 新用户使用相同编号不受老记录影响；
6. 补录条目只携带加噪结果与抑制原因，绝不带出原始精确值。
"""
import json
import logging
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"

logger = logging.getLogger("pqe.test.legacy")


@pytest.fixture()
def legacy_settings(tmp_path: Path) -> Settings:
    """把真实旧账本与旧数据集复制进临时目录，模拟“拿原账本启动新版”。"""
    shutil.copy(FIXTURE_DIR / "ledger.json", tmp_path / "ledger.json")
    shutil.copy(FIXTURE_DIR / "dataset.json", tmp_path / "dataset.json")
    return Settings(
        data_dir=tmp_path,
        default_budget_epsilon=10.0,
        max_query_epsilon=2.0,
        min_group_size=3,
    )


@pytest.fixture()
def expected() -> dict:
    return json.loads((FIXTURE_DIR / "legacy-expected.json").read_text(encoding="utf-8"))


def _open(settings: Settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app)


def _history_ids(client: TestClient, principal: str, **params) -> list[str]:
    r = client.get(
        f"/history/{principal}", params={"viewer": principal, **params}
    )
    assert r.status_code == 200, r.text
    return [i["query_id"] for i in r.json()["items"]]


def test_legacy_records_visible_in_own_history_without_time_range(
    legacy_settings, expected
):
    """场景1：不指定时间范围时，老记录出现在提交者本人的历史里。"""
    logger.info("legacy-history | 场景1 不加时间范围：老记录按 principal 归位进历史")
    with _open(legacy_settings) as client:
        body = client.get("/history/alice", params={"viewer": "alice"}).json()
        assert body["total"] == 2
        items = {i["query_id"]: i for i in body["items"]}
        assert sorted(items) == ["legacy-count-1", "legacy-sum-1"]
        for item in items.values():
            # 老记录没有提交时间，也不被伪造一个时间
            assert item["created_at"] is None
            assert item["legacy"] is True
            assert item["status"] == "completed"
        # 条目内容就是升级前的加噪响应，载荷逐字段一致
        assert (
            items["legacy-count-1"]["results"]
            == expected["alice/legacy-count-1"]["results"]
        )
        assert (
            items["legacy-count-1"]["suppressed"]
            == expected["alice/legacy-count-1"]["suppressed"]
        )
        assert (
            items["legacy-sum-1"]["results"]
            == expected["alice/legacy-sum-1"]["results"]
        )
        # 翻页也要能看到：page_size=1 时两页各一条，total 正确
        page1 = client.get(
            "/history/alice", params={"viewer": "alice", "page": 1, "page_size": 1}
        ).json()
        page2 = client.get(
            "/history/alice", params={"viewer": "alice", "page": 2, "page_size": 1}
        ).json()
        assert page1["total"] == page2["total"] == 2
        assert len(page1["items"]) == len(page2["items"]) == 1
        assert page1["items"][0]["query_id"] != page2["items"][0]["query_id"]


def test_legacy_records_not_leaked_to_other_users(legacy_settings):
    """场景1b：老记录只落在提交者名下，不能串到别人那里。"""
    logger.info("legacy-history | 场景1b 多用户隔离：老记录不串到其他用户历史")
    with _open(legacy_settings) as client:
        # carol 只看到自己的老记录
        assert _history_ids(client, "carol") == ["legacy-carol-1"]
        # 一个全新用户看不到任何人的老记录
        assert _history_ids(client, "dave") == []
        # alice 看不到 carol 的编号，carol 也看不到 alice 的
        assert "legacy-carol-1" not in _history_ids(client, "alice")
        for qid in ("legacy-count-1", "legacy-sum-1"):
            assert qid not in _history_ids(client, "carol")
        # 越权查看仍然拒绝
        r = client.get("/history/alice", params={"viewer": "carol"})
        assert r.status_code == 403
        assert r.json()["error"] == "unauthorized"


@pytest.mark.parametrize(
    "params",
    [
        {"start": 0},                       # 只给开始时间
        {"end": 4_000_000_000},             # 只给结束时间（远期）
        {"start": 0, "end": 4_000_000_000}, # 覆盖极宽的范围
        {"start": 1_700_000_000, "end": 2_000_000_000},
    ],
)
def test_legacy_records_excluded_when_time_range_given(legacy_settings, params):
    """场景2：只要指定了时间范围，无时间的老记录必须排除。"""
    logger.info("legacy-history | 场景2 时间范围筛选 params=%s 排除老记录", params)
    with _open(legacy_settings) as client:
        assert _history_ids(client, "alice", **params) == []
        assert _history_ids(client, "carol", **params) == []
        # total 同样不计老记录
        body = client.get(
            "/history/alice", params={"viewer": "alice", **params}
        ).json()
        assert body["total"] == 0


def test_legacy_records_time_filter_alongside_new_records(legacy_settings):
    """场景2b：迁移后新提交的记录仍受时间筛选正常约束，老记录不混入时间段。"""
    logger.info("legacy-history | 场景2b 老记录与新记录混合时时间筛选行为不变")
    with _open(legacy_settings) as client:
        r = client.post(
            "/query",
            json={
                "query_id": "new-count-1",
                "principal": "alice",
                "aggregation": "count",
                "dimension": "city",
                "epsilon": 1.0,
            },
        )
        assert r.status_code == 200, r.text
        new_ts = client.get("/history/alice", params={"viewer": "alice"}).json()[
            "items"
        ][-1]["created_at"]
        # 无时间范围：老记录（排前）+ 新记录共 3 条
        ids = _history_ids(client, "alice")
        assert sorted(ids) == ["legacy-count-1", "legacy-sum-1", "new-count-1"]
        assert ids[-1] == "new-count-1"
        # 只覆盖新记录之后的范围：只见新记录
        ids = _history_ids(client, "alice", start=new_ts - 1e-6)
        assert ids == ["new-count-1"]
        # 只覆盖新记录之前的范围：谁都不见
        ids = _history_ids(client, "alice", end=new_ts - 1e-6)
        assert ids == []
        # 其它筛选（聚合/状态）对老记录仍生效
        assert _history_ids(client, "alice", aggregation="sum") == ["legacy-sum-1"]
        assert _history_ids(client, "alice", status="rejected") == []
        assert sorted(
            _history_ids(client, "alice", aggregation="count")
        ) == ["legacy-count-1", "new-count-1"]


def test_repeated_loads_backfill_once_and_keep_budget_intact(
    legacy_settings, expected, caplog
):
    """场景3：反复加载/重启不重复补录，预算与查询账实不变。"""
    logger.info("legacy-history | 场景3 反复加载：补录幂等、账实不变")
    ledger_path = legacy_settings.ledger_path

    def history_count_on_disk() -> tuple[int, int]:
        disk = json.loads(ledger_path.read_text(encoding="utf-8"))
        events = disk["history"]
        alice = sum(1 for e in events if e["principal"] == "alice")
        carol = sum(1 for e in events if e["principal"] == "carol")
        return alice, carol

    # 多次“重启”：每次都是新存储/新应用实例，加载后触发一次事务落盘
    for cycle in range(3):
        caplog.clear()
        logger.info("legacy-history | 场景3 第 %d 次重新加载旧账本", cycle + 1)
        with _open(legacy_settings) as client:
            assert client.get("/budget/alice").json()["spent_epsilon"] == 2.0
            assert client.get("/budget/carol").json()["spent_epsilon"] == 1.0
            assert client.get("/history/alice", params={"viewer": "alice"}).json()[
                "total"
            ] == 2
            assert client.get("/history/carol", params={"viewer": "carol"}).json()[
                "total"
            ] == 1
        # 仅首次加载补录 3 条；之后重启日志里不再出现补录动作
        backfill_logs = [
            r.message for r in caplog.records if "legacy_history_backfilled" in r.message
        ]
        if cycle == 0:
            assert any("count=3" in m for m in backfill_logs), backfill_logs
        else:
            assert backfill_logs == [], backfill_logs
        assert history_count_on_disk() == (2, 1)

    # 迁移不改动查询与预算分区的账实
    disk = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert sorted(disk["queries"]) == ["alice", "carol"]
    assert disk["principals"]["alice"]["spent_epsilon"] == 2.0
    assert disk["principals"]["carol"]["spent_epsilon"] == 1.0
    # 老查询记录本身保持旧样：仍无 created_at
    for qid in ("legacy-count-1", "legacy-sum-1"):
        assert disk["queries"]["alice"][qid]["created_at"] is None
    # 同一条老记录在历史中只补了一次（按 query_id 唯一）
    alice_legacy = [
        e for e in disk["history"]
        if e["principal"] == "alice" and e.get("legacy")
    ]
    assert len(alice_legacy) == 2
    assert len({e["query_id"] for e in alice_legacy}) == 2


def test_legacy_query_replays_identically_without_double_charge(
    legacy_settings, expected
):
    """场景4：老查询仍可原样重放，结果逐字段一致，不重复扣预算。"""
    logger.info("legacy-history | 场景4 老查询原样重放：结果不变、不重复扣费")
    with _open(legacy_settings) as client:
        r = client.post(
            "/query",
            json={
                "query_id": "legacy-count-1",
                "principal": "alice",
                "aggregation": "count",
                "dimension": "city",
                "epsilon": 1.0,
                "groups": ["Beijing", "Shanghai", "Shenzhen", "Nowhere"],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["replayed"] is True
        first = dict(expected["alice/legacy-count-1"])
        first.pop("replayed")
        actual = dict(body)
        actual.pop("replayed")
        # 纯增量字段之外，DP 载荷与升级前逐字段一致
        assert actual["results"] == first["results"]
        assert actual["suppressed"] == first["suppressed"]
        assert actual["epsilon_charged"] == first["epsilon_charged"]
        assert actual["remaining_budget"] == first["remaining_budget"]
        # 再重放一次依旧命中缓存
        r2 = client.post(
            "/query",
            json={
                "query_id": "legacy-count-1",
                "principal": "alice",
                "aggregation": "count",
                "dimension": "city",
                "epsilon": 1.0,
                "groups": ["Beijing", "Shanghai", "Shenzhen", "Nowhere"],
            },
        )
        assert r2.json()["replayed"] is True
        # 已耗预算仍是升级前的 2.0，不重复扣
        assert client.get("/budget/alice").json()["spent_epsilon"] == 2.0

        # 重放会按新版本行为产生 replayed 历史事件，但老记录的补录只有一条
        items = client.get("/history/alice", params={"viewer": "alice"}).json()["items"]
        by_id = {}
        for i in items:
            by_id.setdefault(i["query_id"], []).append(i)
        legacy_events = by_id["legacy-count-1"]
        # 恰好一条迁移补录（legacy+completed），其余是用户主动重放产生的
        assert [e["status"] for e in legacy_events if e["legacy"]] == ["completed"]
        assert {e["status"] for e in legacy_events if not e["legacy"]} == {"replayed"}
        # 补录条目仍是最初的加噪载荷，重放事件也不带精确值
        for e in legacy_events:
            assert e["created_at"] is None if e["legacy"] else e["created_at"] is not None
            assert e["results"] == expected["alice/legacy-count-1"]["results"]


def test_new_user_same_query_id_unaffected(legacy_settings):
    """场景5：新用户用相同编号独立执行，与老记录互不影响。"""
    logger.info("legacy-history | 场景5 新用户同名编号不受老记录影响")
    with _open(legacy_settings) as client:
        client.post("/admin/grant", json={"principal": "bob", "budget_epsilon": 5.0})
        r = client.post(
            "/query",
            json={
                "query_id": "legacy-count-1",
                "principal": "bob",
                "aggregation": "count",
                "dimension": "city",
                "epsilon": 1.0,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["replayed"] is False
        assert client.get("/budget/bob").json()["spent_epsilon"] == 1.0
        # bob 的历史只有自己的新记录，拿不到 alice 的老结果
        bob_items = _history_ids(client, "bob")
        assert bob_items == ["legacy-count-1"]
        item = client.get("/history/bob", params={"viewer": "bob"}).json()["items"][0]
        assert item["legacy"] is False
        assert item["created_at"] is not None
        # alice 的账实与老记录不受影响
        assert client.get("/budget/alice").json()["spent_epsilon"] == 2.0
        assert len(_history_ids(client, "alice")) == 2


def test_backfilled_history_carries_no_exact_values(legacy_settings):
    """场景6：补录条目只有加噪结果与抑制原因，不含任何原始精确值。"""
    logger.info("legacy-history | 场景6 隐私：补录条目不含原始精确值")
    with _open(legacy_settings) as client:
        body = client.get("/history/alice", params={"viewer": "alice"}).json()
        for item in body["items"]:
            assert "exact" not in json.dumps(item).lower()
            if item["results"]:
                for g in item["results"]:
                    assert set(g.keys()) <= {"group", "value", "noisy_count"}
            if item["suppressed"]:
                for g in item["suppressed"]:
                    # 抑制原因固定文案/阈值，不含具体行数
                    assert set(g.keys()) <= {"group", "reason", "detail"}
                    assert not re.search(r"\d+\s*rows?", g.get("detail") or "")

    # 落盘的历史分区同样不含精确值字段
    disk = json.loads(legacy_settings.ledger_path.read_text(encoding="utf-8"))
    for event in disk["history"]:
        assert set(event.keys()) <= {
            "principal", "query_id", "aggregation", "dimension", "status",
            "epsilon", "error", "created_at", "results", "suppressed", "legacy",
        }
