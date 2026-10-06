"""按采样时间生效的温度上限策略端到端测试。

覆盖：策略写入与回溯重算（历史读数按生效时间重新判定）、逐读数上限解析
（生效边界、多策略分段、未覆盖时段沿用全局上限）、同策略号同内容幂等重放
（含等价时区、事后变化不影响首次回执）、同号异内容 / 生效时间重复冲突、
非法阈值与未知设备错误、读数 / 批次 / 争议接口在策略下的兼容、设备隔离、
策略查询与旧库迁移。
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app

DEVICE = "truck-001"
EMPTY_CHANGES = {"added": [], "changed": [], "invalidated": []}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TEMP_LIMIT", "8.0")
    with TestClient(app) as c:
        yield c


def post(client, event_no, ts, temp, device=DEVICE):
    return client.post(
        f"/devices/{device}/readings",
        json={"event_no": event_no, "sampled_at": ts, "temperature": temp},
    )


def put_policy(client, policy_no, temp_limit, effective_from, device=DEVICE):
    return client.post(
        f"/devices/{device}/limit-policies",
        json={
            "policy_no": policy_no,
            "temp_limit": temp_limit,
            "effective_from": effective_from,
        },
    )


# ---------------------------------------------------------------- 写入与回溯重算

def test_policy_retroactively_creates_interval_from_history(client):
    # 全局上限 8.0 下两条读数均不超限，无区间
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []

    # 策略把 08:00 起的上限降到 5.0：历史读数按回溯生效时间重新判定为超限
    r = put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    assert r.status_code == 201
    body = r.json()
    assert body["device_id"] == DEVICE
    assert body["policy_no"] == "P1"
    assert body["temp_limit"] == 5.0
    assert body["effective_from"] == "2026-10-01T08:00:00.000000Z"
    assert body["limit"] == 8.0  # 全局默认上限随回执返回
    iv = body["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] is None
    assert iv["reading_count"] == 2
    assert iv["max_temperature"] == 7.5
    assert [i["start_event_no"] for i in body["changes"]["added"]] == ["E1"]
    assert body["changes"]["invalidated"] == []
    # 重算结果对区间查询可见
    assert len(client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]) == 1


def test_policy_raise_limit_invalidates_existing_interval(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    assert len(client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]) == 1

    # 策略把上限抬到 10.0：原区间整体失效
    r = put_policy(client, "P1", 10.0, "2026-10-01T08:00:00Z")
    body = r.json()
    assert body["intervals"] == []
    assert [i["start_event_no"] for i in body["changes"]["invalidated"]] == ["E1"]
    assert body["changes"]["added"] == []


def test_policy_change_shifts_interval_start(client):
    # 全局 8.0 下 E1、E2、E3 连续超限，区间起点 E1
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    post(client, "E3", "2026-10-01T08:20:00Z", 9.2)
    # 策略仅覆盖 08:10 起并抬到 9.6：E2、E3 不再超限，E1 孤立 → 无区间
    r = put_policy(client, "P1", 9.6, "2026-10-01T08:10:00Z")
    body = r.json()
    assert body["intervals"] == []
    assert [i["start_event_no"] for i in body["changes"]["invalidated"]] == ["E1"]


# ---------------------------------------------------------------- 逐读数上限解析

def test_effective_from_boundary_is_inclusive(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)   # 策略生效前 → 全局 8.0
    post(client, "E2", "2026-10-01T08:10:00Z", 7.0)   # 恰在生效时刻 → 策略 6.0
    post(client, "E3", "2026-10-01T08:20:00Z", 7.0)   # 生效后 → 策略 6.0
    r = put_policy(client, "P1", 6.0, "2026-10-01T08:10:00Z")
    iv = r.json()["intervals"][0]
    # E1 仍不超限（7.0 < 8.0），E2、E3 超限（7.0 > 6.0），区间起点为 E2
    assert iv["start_event_no"] == "E2"
    assert iv["reading_count"] == 2


def test_uncovered_period_falls_back_to_global_limit(client):
    # 策略只覆盖 08:20 起；更早的读数继续用全局上限
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.0)
    put_policy(client, "P1", 6.0, "2026-10-01T08:20:00Z")
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []


def test_multiple_policies_segment_timeline(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.0)
    post(client, "E3", "2026-10-01T08:20:00Z", 7.0)
    post(client, "E4", "2026-10-01T08:30:00Z", 7.0)
    # 全局 8.0：E1、E2 超限，E3 闭合区间
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"][0][
        "end_event_no"
    ] == "E3"

    # P1：08:20 起上限 6.0 → E3、E4 转为超限，原区间延长为未闭合
    r = put_policy(client, "P1", 6.0, "2026-10-01T08:20:00Z")
    body = r.json()
    iv = body["intervals"][0]
    assert iv["start_event_no"] == "E1"
    assert iv["end_event_no"] is None
    assert iv["reading_count"] == 4
    assert [i["start_event_no"] for i in body["changes"]["changed"]] == ["E1"]

    # P2：08:05 起上限 9.5 → E2 不再超限（E1 孤立），E3、E4 仍超限（P1 段）
    r = put_policy(client, "P2", 9.5, "2026-10-01T08:05:00Z")
    body = r.json()
    starts = [i["start_event_no"] for i in body["intervals"]]
    assert starts == ["E3"]
    assert [i["start_event_no"] for i in body["changes"]["invalidated"]] == ["E1"]
    assert [i["start_event_no"] for i in body["changes"]["added"]] == ["E3"]


def test_new_readings_are_judged_by_policy(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    put_policy(client, "P1", 10.0, "2026-10-01T08:00:00Z")
    # 新读数 9.2 在全局上限之上、策略上限之下 → 不超限，不产生区间
    r = post(client, "E3", "2026-10-01T08:20:00Z", 9.2)
    assert r.json()["intervals"] == []
    assert r.json()["changes"] == EMPTY_CHANGES
    # 超过策略上限的连续两条 → 区间重新出现
    post(client, "E4", "2026-10-01T08:30:00Z", 10.5)
    r = post(client, "E5", "2026-10-01T08:40:00Z", 11.0)
    assert [i["start_event_no"] for i in r.json()["intervals"]] == ["E4"]


# ---------------------------------------------------------------- 幂等与冲突

def test_same_policy_same_content_replay_returns_first_receipt(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    first = put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    assert first.status_code == 201

    # 策略生效后读数继续变化（区间被 E3 闭合）
    post(client, "E3", "2026-10-01T08:20:00Z", 4.0)

    # 同号同内容重放（等价时区表示也算同内容）→ 200 且原样返回首次回执
    replay = put_policy(client, "P1", 5.0, "2026-10-01T16:00:00+08:00")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["intervals"][0]["end_event_no"] is None  # 首次时未闭合
    # 重放不产生新的写效果：当前区间仍是闭合态
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"][0][
        "end_event_no"
    ] == "E3"


def test_same_policy_no_different_content_conflicts(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    # 阈值不同 → 409
    r = put_policy(client, "P1", 6.0, "2026-10-01T08:00:00Z")
    assert r.status_code == 409
    # 生效时间不同 → 409
    r = put_policy(client, "P1", 5.0, "2026-10-01T09:00:00Z")
    assert r.status_code == 409


def test_same_effective_from_not_reusable_by_other_policy(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    # 不同策略号占用同一生效时间（含等价时区）→ 409
    r = put_policy(client, "P2", 6.0, "2026-10-01T08:00:00Z")
    assert r.status_code == 409
    assert "effective" in r.json()["detail"]
    r = put_policy(client, "P2", 6.0, "2026-10-01T16:00:00+08:00")
    assert r.status_code == 409
    # 冲突的策略号未留痕：换个生效时间可正常首次写入
    assert put_policy(client, "P2", 6.0, "2026-10-01T09:00:00Z").status_code == 201


def test_policies_are_isolated_per_device(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0, device="truck-002")
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5, device="truck-002")
    put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    # 同策略号 / 同生效时间可在另一设备复用，互不影响
    # （truck-002 自己的策略上限 10.0 不产生区间；truck-001 的 5.0 策略不外溢）
    assert put_policy(
        client, "P1", 10.0, "2026-10-01T08:00:00Z", device="truck-002"
    ).status_code == 201
    assert len(client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]) == 1
    assert client.get("/devices/truck-002/intervals").json()["intervals"] == []


# ---------------------------------------------------------------- 错误处理

def test_unknown_device_rejected(client):
    r = put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z", device="ghost-001")
    assert r.status_code == 404
    assert "ghost-001" in r.json()["detail"]
    # 未留痕：设备有读数后同策略号可首次写入
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0, device="ghost-001")
    assert put_policy(
        client, "P1", 5.0, "2026-10-01T08:00:00Z", device="ghost-001"
    ).status_code == 201


def test_invalid_temp_limit_rejected(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    # 超出物理范围
    assert put_policy(client, "P1", -300.0, "2026-10-01T08:00:00Z").status_code == 422
    assert put_policy(client, "P1", 1000.1, "2026-10-01T08:00:00Z").status_code == 422
    # 非数值
    r = client.post(
        f"/devices/{DEVICE}/limit-policies",
        json={"policy_no": "P1", "temp_limit": "cold",
              "effective_from": "2026-10-01T08:00:00Z"},
    )
    assert r.status_code == 422
    # 无穷 / NaN：标准 JSON 无法表达，直接发原始报文
    for bad in ("1e999", "NaN"):
        r = client.post(
            f"/devices/{DEVICE}/limit-policies",
            content=(
                '{"policy_no":"P1","temp_limit":' + bad + ","
                '"effective_from":"2026-10-01T08:00:00Z"}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422
    # 边界值本身合法：-273.15 与 1000 可接受
    assert put_policy(client, "P1", -273.15, "2026-10-01T08:00:00Z").status_code == 201
    assert put_policy(client, "P2", 1000.0, "2026-10-01T09:00:00Z").status_code == 201


def test_invalid_policy_no_and_effective_from_rejected(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    assert put_policy(client, "bad/no", 5.0, "2026-10-01T08:00:00Z").status_code == 422
    assert put_policy(client, "P1", 5.0, "not-a-time").status_code == 422


# ---------------------------------------------------------------- 与现有接口兼容

def test_batch_submission_respects_policies(client):
    put_policy_setup = post(client, "E0", "2026-10-01T07:00:00Z", 5.0)
    assert put_policy_setup.status_code == 200
    put_policy(client, "P1", 10.0, "2026-10-01T08:00:00Z")
    # 批内读数 9.x 在全局上限之上、策略上限之下 → 不超限
    r = client.post(
        f"/devices/{DEVICE}/reading-batches",
        json={
            "batch_no": "B1",
            "readings": [
                {"event_no": "E1", "sampled_at": "2026-10-01T08:00:00Z",
                 "temperature": 9.0},
                {"event_no": "E2", "sampled_at": "2026-10-01T08:10:00Z",
                 "temperature": 9.5},
            ],
        },
    )
    assert r.status_code == 201
    assert r.json()["intervals"] == []
    # 超过策略上限的批 → 区间出现
    r = client.post(
        f"/devices/{DEVICE}/reading-batches",
        json={
            "batch_no": "B2",
            "readings": [
                {"event_no": "E3", "sampled_at": "2026-10-01T08:20:00Z",
                 "temperature": 10.5},
                {"event_no": "E4", "sampled_at": "2026-10-01T08:30:00Z",
                 "temperature": 11.0},
            ],
        },
    )
    assert [i["start_event_no"] for i in r.json()["intervals"]] == ["E3"]


def test_dispute_workflow_respects_policies(client):
    # 策略上限 9.0：E1 等于上限不超限，E2 超限但孤立 → 无区间
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 9.5)
    put_policy(client, "P1", 9.0, "2026-10-01T08:00:00Z")
    assert client.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []

    # 争议更正 E1 → 9.5 并批准：两条均超策略上限，区间在执行结果中出现
    r = client.post(
        f"/devices/{DEVICE}/disputes",
        json={
            "dispute_no": "D1",
            "target_event_no": "E1",
            "action": "correct",
            "reason": "probe recalibrated",
            "sampled_at": "2026-10-01T08:00:00Z",
            "temperature": 9.5,
        },
    )
    assert r.status_code == 201
    r = client.post(
        f"/devices/{DEVICE}/disputes/D1/decisions",
        json={"decision": "approve"},
    )
    dispute = r.json()["dispute"]
    assert dispute["status"] == "approved"
    assert [i["start_event_no"] for i in dispute["execution"]["intervals"]] == ["E1"]
    assert [
        i["start_event_no"] for i in dispute["execution"]["changes"]["added"]
    ] == ["E1"]


def test_correction_and_revoke_recompute_with_policies(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 7.0)
    post(client, "E2", "2026-10-01T08:10:00Z", 7.5)
    put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    assert len(client.get(f"/devices/{DEVICE}/intervals").json()["intervals"]) == 1
    # 更正 E2 到策略上限之下 → 区间失效
    r = client.put(
        f"/devices/{DEVICE}/readings/E2",
        json={"sampled_at": "2026-10-01T08:10:00Z", "temperature": 4.0},
    )
    assert r.json()["intervals"] == []
    # 撤销 E1 后无新区间
    r = client.delete(f"/devices/{DEVICE}/readings/E1")
    assert r.json()["intervals"] == []


# ---------------------------------------------------------------- 查询与迁移

def test_list_policies_ordered_by_effective_from(client):
    post(client, "E1", "2026-10-01T08:00:00Z", 9.0)
    put_policy(client, "P2", 6.0, "2026-10-01T12:00:00Z")
    put_policy(client, "P1", 5.0, "2026-10-01T08:00:00Z")
    r = client.get(f"/devices/{DEVICE}/limit-policies")
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == DEVICE
    assert body["limit"] == 8.0
    assert [p["policy_no"] for p in body["policies"]] == ["P1", "P2"]
    assert body["policies"][0]["effective_from"] == "2026-10-01T08:00:00.000000Z"
    assert body["policies"][0]["temp_limit"] == 5.0
    # 无策略设备返回空列表（与区间 / 读数查询一致，不报 404）
    r = client.get("/devices/ghost-002/limit-policies")
    assert r.status_code == 200
    assert r.json()["policies"] == []


def test_legacy_db_without_policies_table_migrates(client, tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    # 手工构造旧版 schema（无 limit_policies 表、readings 无 revision 列）
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE readings ("
        "device_id TEXT NOT NULL, event_no TEXT NOT NULL, sampled_at TEXT NOT NULL, "
        "temperature REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (device_id, event_no))"
    )
    raw.execute(
        "INSERT INTO readings VALUES "
        "('truck-001', 'E1', '2026-10-01T08:00:00.000000Z', 7.0, 0, 't', 't'), "
        "('truck-001', 'E2', '2026-10-01T08:10:00.000000Z', 7.5, 0, 't', 't')"
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("DB_PATH", str(db_path))
    with TestClient(app) as c2:
        # 迁移后既有接口行为不变（全局上限下无区间）
        assert c2.get(f"/devices/{DEVICE}/intervals").json()["intervals"] == []
        # 策略接口可直接使用：回溯生效后历史读数形成区间
        r = put_policy(c2, "P1", 5.0, "2026-10-01T08:00:00Z")
        assert r.status_code == 201
        assert [i["start_event_no"] for i in r.json()["intervals"]] == ["E1"]
