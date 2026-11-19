import threading

import pytest

from app.db import connect


def _draft(items, table):
    return next(c for c in items if c["table_name"] == table and c["status"] == "草稿")


async def _contracts(authed):
    return (await (await authed.get("/api/contracts")).json())["items"]


async def test_card_freezes_snapshot_and_isolates_on_missing_edge(authed):
    draft = _draft(await _contracts(authed), "dw_payments_d")
    r = await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")
    assert r.status == 200
    card = (await r.json())["card"]
    snap = card["snapshot"]
    # 种子：pay_channel 花在 dim_customer_d 已删的 channel_code 上
    assert snap["verdict"] == "缺边"
    assert snap["voucher"].startswith("SNAP-")
    assert any(m["parent_field"] == "channel_code" for m in snap["missing_edges"])
    assert card["gate_mode"] == "snapshot"

    # 打开幂等：再打开不新冻一张
    again = (await (await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")).json())["card"]
    assert again["snapshot"]["id"] == snap["id"]

    decided = (await (await authed.post(
        f"/api/contracts/{draft['id']}/lineage/decide", json={}
    )).json())
    assert decided["gate"]["decision"] == "隔离"
    assert decided["gate"]["voucher"] == snap["voucher"]

    blocked = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert blocked.status == 409
    assert "隔离" in (await blocked.json())["error"]


async def test_snapshot_frozen_not_rescanned_at_decide(authed):
    """ALTER 父表字段后，快照未重冻 –– 裁决仍按旧快照，不因当场重扫而改结论。"""
    draft = _draft(await _contracts(authed), "dw_payments_d")
    card = (await (await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")).json())["card"]
    assert card["snapshot"]["verdict"] == "缺边"

    # 直接把父表补回 channel_code：若提交时重扫，就会误判齐套
    conn = connect()
    conn.execute(
        "INSERT INTO contract_fields(contract_id, field_name, field_type) "
        "SELECT id, 'channel_code', 'STRING' FROM schema_contracts "
        "WHERE table_name='dim_customer_d' AND status='现行'"
    )
    conn.commit()
    conn.close()

    decided = (await (await authed.post(
        f"/api/contracts/{draft['id']}/lineage/decide", json={}
    )).json())
    assert decided["gate"]["decision"] == "隔离"
    assert decided["snapshot"]["missing_count"] == 1


async def test_apply_requires_decided_gate(authed):
    draft = _draft(await _contracts(authed), "dw_payments_d")
    not_opened = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert not_opened.status == 409
    assert "快照" in (await not_opened.json())["error"]

    await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")
    frozen_only = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert frozen_only.status == 409
    assert "裁决" in (await frozen_only.json())["error"]


async def test_refreeze_after_edge_fix_allows_apply(authed):
    draft = _draft(await _contracts(authed), "dw_payments_d")
    await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")
    await authed.post(f"/api/contracts/{draft['id']}/lineage/decide", json={})

    # 撤掉断边
    rm = await authed.delete("/api/lineage/edges", json={
        "child_table": "dw_payments_d", "child_field": "pay_channel",
        "parent_table": "dim_customer_d", "parent_field": "channel_code",
    })
    assert rm.status == 200

    card = (await (await authed.post(
        f"/api/contracts/{draft['id']}/lineage/refreeze", json={}
    )).json())["card"]
    assert card["snapshot"]["verdict"] == "齐套"
    assert card["snapshot"]["status"] == "冻结"

    decided = (await (await authed.post(
        f"/api/contracts/{draft['id']}/lineage/decide", json={}
    )).json())
    assert decided["gate"]["decision"] == "齐套放行"

    applied = (await (await authed.post(
        f"/api/contracts/{draft['id']}/apply", json={}
    )).json())["contract"]
    assert applied["status"] == "现行"
    assert applied["gate_voucher"] == card["snapshot"]["voucher"]


async def test_double_decide_does_not_double_write(authed):
    """两名血缘员同点齐套：DB 层只落一条裁决，第二人回放原结果。"""
    draft = _draft(await _contracts(authed), "dw_refunds_d")
    await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")
    results = []

    def worker():
        from app.services.lineage import decide

        try:
            results.append(decide(draft["id"], "admin"))
        except Exception as exc:  # pragma: no cover
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(not isinstance(r, Exception) for r in results)
    replayed = [r["replayed"] for r in results]
    assert sorted(replayed) == [False, True]
    vouchers = {r["gate"]["voucher"] for r in results}
    assert len(vouchers) == 1

    conn = connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM lineage_gates WHERE contract_id=?", (draft["id"],)
    ).fetchone()[0]
    conn.close()
    assert n == 1


async def test_four_views_share_one_voucher(authed):
    """依赖目录 / 父表详情 / 升版卡片 / 水线的芯片与凭证号必须对齐。"""
    draft = _draft(await _contracts(authed), "dw_payments_d")
    card = (await (await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")).json())["card"]
    decision = (await (await authed.post(
        f"/api/contracts/{draft['id']}/lineage/decide", json={}
    )).json())
    voucher = decision["snapshot"]["voucher"]

    directory = (await (await authed.get("/api/lineage/edges")).json())["items"]
    dir_vouchers = {e["snapshot_voucher"] for e in directory if e["child_table"] == "dw_payments_d"}
    assert dir_vouchers == {voucher}
    assert all(e["snapshot_state"] in ("缺边", "齐全") for e in directory if e["child_table"] == "dw_payments_d")

    parent = (await (await authed.get("/api/lineage/parents/dim_customer_d")).json())["parent"]
    assert any(e["chip"] == "缺边" for e in parent["edges"])
    assert all(e["snapshot_voucher"] == voucher for e in parent["edges"])

    waterline = (await (await authed.get("/api/lineage/waterline")).json())["items"]
    row = next(w for w in waterline if w["child_table"] == "dw_payments_d")
    assert row["voucher"] == voucher
    assert row["gate_voucher"] == voucher
    assert row["decision"] == "隔离"


async def test_reconcile_lineage_view_matches_chip(authed):
    draft = _draft(await _contracts(authed), "dw_payments_d")
    await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")
    await authed.post(f"/api/contracts/{draft['id']}/lineage/decide", json={})

    rep = (await (await authed.post(
        "/api/reconcile-reports", json={"as_of": "2026-11-18"}
    )).json())["report"]
    assert rep["lineage_aligned"] == 0
    row = next(r for r in rep["lineage_rows"] if r["table_name"] == "dw_payments_d")
    assert row["snapshot_verdict"] == "缺边"
    assert row["gate_decision"] == "隔离"
    assert "对账" in rep["summary"]


async def test_clean_child_chain_can_upgrade(authed):
    draft = _draft(await _contracts(authed), "dw_refunds_d")
    card = (await (await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")).json())["card"]
    assert card["snapshot"]["verdict"] == "齐套"
    await authed.post(f"/api/contracts/{draft['id']}/lineage/decide", json={})
    applied = await authed.post(f"/api/contracts/{draft['id']}/apply", json={})
    assert applied.status == 200
    body = (await applied.json())["contract"]
    assert body["gate_voucher"] == card["snapshot"]["voucher"]


async def test_snapshot_stale_is_flagged_not_rescanned(authed):
    """祖先表补回列后，卡片只提示“需重新冻结”，不自动改快照结论。"""
    draft = _draft(await _contracts(authed), "dw_payments_d")
    card = (await (await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")).json())["card"]
    assert card["snapshot"]["verdict"] == "缺边"
    assert card["stale"] is False

    conn = connect()
    conn.execute(
        "INSERT INTO contract_fields(contract_id, field_name, field_type) "
        "SELECT id, 'channel_code', 'STRING' FROM schema_contracts "
        "WHERE table_name='dim_customer_d' AND status='现行'"
    )
    conn.commit()
    conn.close()

    same = (await (await authed.get(f"/api/contracts/{draft['id']}/card")).json())["card"]
    assert same["snapshot"]["verdict"] == "缺边"
    assert same["stale"] is True
    assert "重新冻结" in same["refreeze_hint"]


async def test_upsert_existing_edge_is_idempotent(authed):
    """重复接同一条边不该作废现有快照。"""
    draft = _draft(await _contracts(authed), "dw_payments_d")
    card = (await (await authed.get(f"/api/contracts/{draft['id']}/upgrade-card")).json())["card"]
    voucher = card["snapshot"]["voucher"]

    same = (await (await authed.post("/api/lineage/edges", json={
        "child_table": "dw_payments_d", "child_field": "cust_id",
        "parent_table": "dim_customer_d", "parent_field": "cust_id",
    })).json())["result"]
    assert same["changed"] is False
    assert same["need_refreeze"] is False
    assert same["voided_snapshots"] == []

    reread = (await (await authed.get(f"/api/contracts/{draft['id']}/card")).json())["card"]
    assert reread["snapshot"]["status"] == "冻结"
    assert reread["snapshot"]["voucher"] == voucher


async def test_child_upgrade_without_lineage_still_follows_baseline(authed):
    """无父表血缘边的草稿（如父表自身）不被齐套闸门拦住。"""
    items = await _contracts(authed)
    orders = _draft(items, "dw_orders_d")
    r = await authed.post(f"/api/contracts/{orders['id']}/apply", json={})
    assert r.status == 200
