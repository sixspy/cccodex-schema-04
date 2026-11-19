"""父表血缘齐套：冻结缺边快照 → 单边裁决 → 与子表升版互锁。

本项目只采用一种齐套门槛（Config.GATE_MODE == "snapshot"）：
打开子表升版卡片的那一刻按当前父表血缘冻结缺边快照，之后齐套与升版都只认这份
快照；“提交瞬间当场重扫父表缺边”那一侧不实现、不混挂。快照冻结后父表再改边，
必须显式重新冻结，旧快照作废。
"""

import json

from app.config import Config
from app.db import get_db, get_tx
from app.lineage_core import edge_digest, missing_edges, voucher_for
from app.services.errors import ServiceError


def _parent_state(conn, edges: list[dict]) -> dict[str, dict]:
    """取每条边涉及的父表当前现行契约版本与字段。"""
    parents: dict[str, dict] = {}
    for name in {e["parent_table"] for e in edges}:
        live = conn.execute(
            "SELECT id, version_no FROM schema_contracts WHERE table_name=? AND status='现行'",
            (name,),
        ).fetchone()
        if not live:
            parents[name] = {"version": None, "fields": []}
            continue
        fields = [
            r["field_name"]
            for r in conn.execute(
                "SELECT field_name FROM contract_fields WHERE contract_id=? ORDER BY field_name",
                (live["id"],),
            )
        ]
        parents[name] = {"version": live["version_no"], "fields": fields}
    return parents


def _edges(conn, child_table: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT child_field, parent_table, parent_field, note FROM lineage_edges "
            "WHERE child_table=? ORDER BY parent_table, parent_field",
            (child_table,),
        )
    ]


def _evaluate(conn, contract_id: int) -> dict:
    draft = conn.execute(
        "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
    ).fetchone()
    if not draft:
        raise ServiceError("契约不存在", 404)
    edges = _edges(conn, draft["table_name"])
    parents = _parent_state(conn, edges)
    broken = missing_edges(edges, parents)
    digest = edge_digest(draft["table_name"], parents, edges)
    return {
        "contract": draft,
        "edges": edges,
        "parents": parents,
        "missing": broken,
        "digest": digest,
        "verdict": "缺边" if broken else "齐套",
    }


def _snapshot_dict(row, conn) -> dict:
    items = []
    for e in json.loads(row["missing_edges"]):
        items.append(e)
    gate = conn.execute(
        "SELECT * FROM lineage_gates WHERE snapshot_id=?", (row["id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "contract_id": row["contract_id"],
        "child_table": row["child_table"],
        "digest": row["digest"],
        "parents": json.loads(row["parent_state"]),
        "missing_edges": items,
        "missing_count": len(items),
        "verdict": row["verdict"],
        "status": row["status"],
        "voucher": row["voucher"],
        "frozen_by": row["frozen_by"],
        "frozen_at": row["frozen_at"],
        "decided_at": row["decided_at"],
        "gate": dict(gate) if gate else None,
        "gate_mode": Config.GATE_MODE,
        "gate_note": Config.GATE_NOTE,
    }


def _watermark(conn, child_table: str, stage: str, voucher: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO lineage_watermarks(child_table, stage, voucher, detail) VALUES (?,?,?,?)",
        (child_table, stage, voucher, detail),
    )


def _latest_snapshot(conn, contract_id: int):
    """最近一张有效快照（作废的不算）。"""
    return conn.execute(
        "SELECT * FROM lineage_snapshots WHERE contract_id=? AND status!='已作废' "
        "ORDER BY id DESC LIMIT 1",
        (contract_id,),
    ).fetchone()


def _frozen_snapshot(conn, contract_id: int):
    return conn.execute(
        "SELECT * FROM lineage_snapshots WHERE contract_id=? AND status='冻结'",
        (contract_id,),
    ).fetchone()


def _freeze(conn, contract_id: int, actor: str, force: bool = False) -> dict:
    """打开升版卡片：当场按父表现行血缘冻结缺边快照。

    - 已有冻结快照：原样返回（打开卡片幂等）；
    - 只有已裁决快照：默认回看那一张（保留留证），force=True 才另拍新快照；
    - 什么快照都没有：按当前父表血缘冻结一张。
    """
    existing = _frozen_snapshot(conn, contract_id)
    if existing:
        return _snapshot_dict(existing, conn)
    if not force:
        latest = _latest_snapshot(conn, contract_id)
        if latest:
            return _snapshot_dict(latest, conn)
    ev = _evaluate(conn, contract_id)
    cur = conn.execute(
        "INSERT INTO lineage_snapshots(contract_id, child_table, digest, parent_state, "
        "missing_edges, verdict, status, voucher, frozen_by) VALUES (?,?,?,?,?,?, '冻结', ?, ?)",
        (
            contract_id,
            ev["contract"]["table_name"],
            ev["digest"],
            json.dumps(ev["parents"], ensure_ascii=False, sort_keys=True),
            json.dumps(ev["missing"], ensure_ascii=False),
            ev["verdict"],
            "",
            actor,
        ),
    )
    snapshot_id = cur.lastrowid
    voucher = voucher_for(snapshot_id, ev["digest"])
    conn.execute(
        "UPDATE lineage_snapshots SET voucher=? WHERE id=?", (voucher, snapshot_id)
    )
    _watermark(
        conn,
        ev["contract"]["table_name"],
        "冻结缺边快照",
        voucher,
        f"{ev['verdict']}·缺边{len(ev['missing'])}",
    )
    row = conn.execute(
        "SELECT * FROM lineage_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    return _snapshot_dict(row, conn)


def open_card(contract_id: int, actor: str, db_path: str | None = None) -> dict:
    """GET 子表升版卡片：打开即冻结（幂等）。仅草稿可打开。"""
    with get_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿升版卡片可冻结齐套快照", 409)
        snap = _freeze(conn, contract_id, actor)
        return _card_payload(conn, draft, snap)


def card_view(contract_id: int, db_path: str | None = None) -> dict:
    """只读回看卡片：不冻结新快照，按最近一张有效快照渲染（升版后仍可回看）。"""
    with get_db(db_path) as conn:
        contract = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not contract:
            raise ServiceError("契约不存在", 404)
        snap_row = _latest_snapshot(conn, contract_id)
        if not snap_row:
            raise ServiceError("未冻结齐套快照，先打开升版卡片", 409)
        return _card_payload(conn, contract, _snapshot_dict(snap_row, conn))


def refreeze(contract_id: int, actor: str, db_path: str | None = None) -> dict:
    """改边后显式重新冻结：旧冻结快照作废，按当前父表血缘重拍一张。"""
    with get_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可重新冻结齐套快照", 409)
        old = _latest_snapshot(conn, contract_id)
        if old:
            conn.execute(
                "UPDATE lineage_snapshots SET status='已作废' WHERE id=?", (old["id"],)
            )
            _watermark(
                conn,
                draft["table_name"],
                "快照作废（重冻结）",
                old["voucher"],
                "显式重新冻结",
            )
        snap = _freeze(conn, contract_id, actor, force=True)
        return _card_payload(conn, draft, snap)


def _card_payload(conn, draft, snap: dict) -> dict:
    """联动四处：依赖目录 / 父表详情 / 升版卡片 / 进度水线。

    只做“当前血缘 vs 快照指纹”的比对提示，不改裁决：两者不一致就把
    stale 标出来，提示显式重新冻结。
    """
    edges = _edges(conn, draft["table_name"])
    live_parents = _parent_state(conn, edges)
    live_digest = edge_digest(draft["table_name"], live_parents, edges)
    parents = snap["parents"]
    broken_keys = {(e["parent_table"], e["parent_field"]) for e in snap["missing_edges"]}
    directory = []
    for e in edges:
        directory.append(
            {
                **e,
                "parent_version": parents.get(e["parent_table"], {}).get("version"),
                "parent_live_fields": parents.get(e["parent_table"], {}).get("fields", []),
                "state": "缺边" if (e["parent_table"], e["parent_field"]) in broken_keys else "齐全",
            }
        )
    watermarks = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM lineage_watermarks WHERE child_table=? ORDER BY id",
            (draft["table_name"],),
        )
    ]
    involved = sorted(parents)
    parent_details = []
    for name in involved:
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'", (name,)
        ).fetchone()
        parent_details.append(
            {
                "table_name": name,
                "version": parents.get(name, {}).get("version"),
                "fields": parents.get(name, {}).get("fields", []),
                "note": row["note"] if row else "",
                "used_edges": [
                    {
                        "child_field": e["child_field"],
                        "parent_field": e["parent_field"],
                        "state": "缺边" if (e["parent_table"], e["parent_field"]) in broken_keys else "齐全",
                    }
                    for e in directory
                    if e["parent_table"] == name
                ],
            }
        )
    return {
        "contract": dict(draft),
        "snapshot": snap,
        "directory": directory,
        "parent_details": parent_details,
        "watermarks": watermarks,
        "gate_mode": Config.GATE_MODE,
        "gate_chip": Config.GATE_CHIP,
        "gate_note": Config.GATE_NOTE,
        # 快照冻结后血缘又变了：只提示，不自动重扫、不改裁决。
        "stale": live_digest != snap["digest"],
        "refreeze_hint": (
            "血缘边或父表现行已变动，快照不再代表当前血缘，请重新冻结后再裁决"
            if live_digest != snap["digest"]
            else ""
        ),
    }


def decide(contract_id: int, actor: str, db_path: str | None = None) -> dict:
    """点“齐套”：按冻结快照一次改写。缺边 -> 隔离（子表不得升现行）；齐套 -> 放行。"""
    with get_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        snap_row = _frozen_snapshot(conn, contract_id)
        if not snap_row:
            latest = _latest_snapshot(conn, contract_id)
            if latest and latest["status"] == "已裁决":
                # 并发场景：另一名血缘员刚裁决完，直接回放原裁决，不双写。
                return {
                    "snapshot": _snapshot_dict(latest, conn),
                    "gate": _gate_row(conn, latest["id"]),
                    "replayed": True,
                }
            raise ServiceError("未冻结齐套快照，先打开升版卡片", 409)
        snap = _snapshot_dict(snap_row, conn)
        if snap["gate"]:
            return {"snapshot": snap, "gate": snap["gate"], "replayed": True}

        missing = snap["missing_edges"]
        if missing:
            decision, detail = "隔离", f"父表缺边{len(missing)}条，子表隔离，不得升现行"
        else:
            decision, detail = "齐套放行", "父表血缘齐套，允许子表升现行"
        voucher = snap["voucher"]
        conn.execute(
            "INSERT INTO lineage_gates(snapshot_id, contract_id, child_table, decision, "
            "voucher, detail, actor) VALUES (?,?,?,?,?,?,?)",
            (snap["id"], contract_id, draft["table_name"], decision, voucher, detail, actor),
        )
        conn.execute(
            "UPDATE lineage_snapshots SET status='已裁决', decided_at=datetime('now') WHERE id=?",
            (snap["id"],),
        )
        _watermark(
            conn,
            draft["table_name"],
            "齐套隔离" if decision == "隔离" else "齐套放行",
            voucher,
            detail,
        )
        fresh = conn.execute(
            "SELECT * FROM lineage_snapshots WHERE id=?", (snap["id"],)
        ).fetchone()
        return {
            "snapshot": _snapshot_dict(fresh, conn),
            "gate": _gate_row(conn, snap["id"]),
            "replayed": False,
        }


def _gate_row(conn, snapshot_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM lineage_gates WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    return dict(row) if row else None


def gate_state(conn, contract_id: int) -> dict | None:
    """子表升版前的闸门校验。

    只有挂了父表血缘边的子表才受齐套闸门约束；无血缘边的表（如纯父表）按基线放行，
    返回 None。有血缘边的子表：快照必须已冻结且已裁决，且裁决必须与快照一致。
    """
    draft = conn.execute(
        "SELECT table_name FROM schema_contracts WHERE id=?", (contract_id,)
    ).fetchone()
    if not draft:
        raise ServiceError("契约不存在", 404)
    if not _edges(conn, draft["table_name"]):
        return None
    snap_row = _frozen_snapshot(conn, contract_id)
    if snap_row:
        raise ServiceError("齐套快照尚未裁决，先点齐套", 409)
    snap_row = _latest_snapshot(conn, contract_id)
    if not snap_row:
        raise ServiceError("未冻结齐套快照，先打开升版卡片", 409)
    snap = _snapshot_dict(snap_row, conn)
    if snap["status"] != "已裁决" or not snap["gate"]:
        raise ServiceError("齐套快照未裁决，先点齐套", 409)
    if snap["verdict"] == "缺边" or snap["gate"]["decision"] == "隔离":
        raise ServiceError("父表血缘缺边，子表已隔离，禁止升现行", 409)
    # 注意：这里不重扫父表。门槛只取“打开即冻结快照”这一边，升版只认已有裁决。
    return snap


def list_directory(db_path: str | None = None) -> list[dict]:
    """依赖目录：每条血缘边只亮“快照芯片”。

    本项目齐套门槛已定为冻结快照一侧，因此这里不做在线重扫、不亮重扫芯片，
    每条边的状态一律取自该子表最近一张快照的留证；没有快照就显示未冻结。
    这样目录上的芯片与升版卡片、水线、对账回看是同一份证据。
    """
    with get_db(db_path) as conn:
        edges = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM lineage_edges ORDER BY child_table, parent_table, parent_field"
            )
        ]
        snapshots: dict[str, dict | None] = {}
        voided: dict[str, dict | None] = {}
        for name in {e["child_table"] for e in edges}:
            snap_row = _latest_snapshot_for_table(conn, name)
            snapshots[name] = _snapshot_dict(snap_row, conn) if snap_row else None
            if not snap_row:
                v = conn.execute(
                    "SELECT * FROM lineage_snapshots WHERE child_table=? AND status='已作废' "
                    "ORDER BY id DESC LIMIT 1",
                    (name,),
                ).fetchone()
                voided[name] = _snapshot_dict(v, conn) if v else None
        out = []
        for e in edges:
            snap = snapshots.get(e["child_table"])
            stale = voided.get(e["child_table"])
            snap_broken = {
                (m["parent_table"], m["parent_field"])
                for m in (snap["missing_edges"] if snap else [])
            }
            if not snap and stale:
                snap_state = "已作废"
                shown = stale
            elif not snap:
                snap_state = "未冻结"
                shown = None
            elif (e["parent_table"], e["parent_field"]) in snap_broken:
                snap_state = "缺边"
                shown = snap
            else:
                snap_state = "齐全"
                shown = snap
            parent = _parent_version(conn, e["parent_table"])
            out.append(
                {
                    **e,
                    "parent_version": parent,
                    "state": snap_state,
                    "chip": Config.GATE_CHIP,
                    "snapshot_state": snap_state,
                    "snapshot_voucher": shown["voucher"] if shown else "",
                    "snapshot_verdict": shown["verdict"] if shown else "",
                    "snapshot_status": shown["status"] if shown else "",
                }
            )
        return out


def _parent_version(conn, table_name: str) -> int | None:
    row = conn.execute(
        "SELECT version_no FROM schema_contracts WHERE table_name=? AND status='现行'",
        (table_name,),
    ).fetchone()
    return row["version_no"] if row else None


def _latest_snapshot_for_table(conn, child_table: str):
    return conn.execute(
        "SELECT * FROM lineage_snapshots WHERE child_table=? AND status!='已作废' "
        "ORDER BY id DESC LIMIT 1",
        (child_table,),
    ).fetchone()


def upsert_edge(
    child_table: str,
    child_field: str,
    parent_table: str,
    parent_field: str,
    actor: str,
    db_path: str | None = None,
) -> dict:
    """改血缘边。已冻结的快照当场作废 —— 不回头重扫父表，快照只会作废或重拍。"""
    child_table = child_table.strip()
    child_field = child_field.strip()
    parent_table = parent_table.strip()
    parent_field = parent_field.strip()
    if not all([child_table, child_field, parent_table, parent_field]):
        raise ServiceError("血缘边四端都不能为空", 400)
    with get_tx(db_path) as conn:
        contract = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='草稿'",
            (child_table,),
        ).fetchone()
        if not contract:
            raise ServiceError("子表没有草稿契约，血缘边已冻结", 409)
        cur = conn.execute(
            "INSERT INTO lineage_edges(child_table, child_field, parent_table, parent_field, note) "
            "VALUES (?,?,?,?,?) ON CONFLICT(child_table, child_field, parent_table, parent_field) "
            "DO NOTHING",
            (child_table, child_field, parent_table, parent_field, f"{actor} 改边"),
        )
        changed = cur.rowcount > 0
        stale_rows = conn.execute(
            "SELECT * FROM lineage_snapshots WHERE child_table=? AND status='冻结'",
            (child_table,),
        ).fetchall()
        # 边本来就在：没动血缘，不该顺手作废快照。
        if not changed:
            stale_rows = []
        for stale in stale_rows:
            conn.execute(
                "UPDATE lineage_snapshots SET status='已作废' WHERE id=?", (stale["id"],)
            )
            _watermark(
                conn,
                child_table,
                "快照作废（改边）",
                stale["voucher"],
                f"{child_table}.{child_field}→{parent_table}.{parent_field}",
            )
        # 只有真的动了血缘，才要求重新冻结；重复接同一条边是幂等的。
        need = bool(stale_rows) or (changed and _has_decided(conn, child_table))
        row = conn.execute(
            "SELECT * FROM lineage_edges WHERE child_table=? AND child_field=? AND parent_table=? "
            "AND parent_field=?",
            (child_table, child_field, parent_table, parent_field),
        ).fetchone()
        return {
            "edge": dict(row),
            "voided_snapshots": [dict(r) for r in stale_rows],
            # 改边后要么快照已作废、要么已有已裁决快照挡着，都得显式重新冻结。
            "need_refreeze": need,
            "changed": changed,
        }


def remove_edge(
    child_table: str,
    child_field: str,
    parent_table: str,
    parent_field: str,
    actor: str,
    db_path: str | None = None,
) -> dict:
    """撤一条血缘边（典型修复动作：父表删列后把断边改成新落点，先撤旧边）。

    与改边同一口径：冻结快照当场作废，等下一次打开卡片重新冻结。
    """
    with get_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT 1 FROM schema_contracts WHERE table_name=? AND status='草稿'",
            (child_table,),
        ).fetchone()
        if not draft:
            raise ServiceError("子表没有草稿契约，血缘边已冻结", 409)
        cur = conn.execute(
            "DELETE FROM lineage_edges WHERE child_table=? AND child_field=? AND parent_table=? "
            "AND parent_field=?",
            (child_table, child_field, parent_table, parent_field),
        )
        if cur.rowcount == 0:
            raise ServiceError("血缘边不存在", 404)
        stale_rows = conn.execute(
            "SELECT * FROM lineage_snapshots WHERE child_table=? AND status='冻结'",
            (child_table,),
        ).fetchall()
        for stale in stale_rows:
            conn.execute(
                "UPDATE lineage_snapshots SET status='已作废' WHERE id=?", (stale["id"],)
            )
            _watermark(
                conn,
                child_table,
                "快照作废（撤边）",
                stale["voucher"],
                f"{child_table}.{child_field}→{parent_table}.{parent_field}",
            )
        decided = conn.execute(
            "SELECT COUNT(*) FROM lineage_snapshots WHERE child_table=? AND status='已裁决'",
            (child_table,),
        ).fetchone()[0]
        return {
            "removed": {
                "child_table": child_table,
                "child_field": child_field,
                "parent_table": parent_table,
                "parent_field": parent_field,
            },
            "voided_snapshots": [dict(r) for r in stale_rows],
            "need_refreeze": bool(stale_rows) or bool(decided),
            "decided_snapshots": decided,
        }


def _has_decided(conn, child_table: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM lineage_snapshots WHERE child_table=? AND status='已裁决'",
        (child_table,),
    ).fetchone()[0] > 0


def list_watermarks(child_table: str | None = None, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        sql = "SELECT * FROM lineage_watermarks"
        args: tuple = ()
        if child_table:
            sql += " WHERE child_table=?"
            args = (child_table,)
        sql += (
            " ORDER BY id"
            if child_table
            else " ORDER BY id DESC LIMIT 200"
        )
        return [dict(r) for r in conn.execute(sql, args)]


def parent_detail(table_name: str, db_path: str | None = None) -> dict:
    """父表详情：现行字段 + 所有指向它的血缘边，以及引用方快照里的边状态。

    这里的边状态同样只取快照芯片（与依赖目录、升版卡片一个口径）。
    """
    with get_db(db_path) as conn:
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (table_name,),
        ).fetchone()
        fields = (
            [
                dict(r)
                for r in conn.execute(
                    "SELECT field_name, field_type FROM contract_fields WHERE contract_id=? "
                    "ORDER BY field_name",
                    (live["id"],),
                )
            ]
            if live
            else []
        )
        field_names = {f["field_name"] for f in fields}
        edges = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM lineage_edges WHERE parent_table=? ORDER BY child_table, parent_field",
                (table_name,),
            )
        ]
        children: dict[str, dict | None] = {}
        for e in edges:
            if e["child_table"] in children:
                continue
            snap = _latest_snapshot_for_table(conn, e["child_table"])
            children[e["child_table"]] = _snapshot_dict(snap, conn) if snap else None
        out_edges = []
        for e in edges:
            snap = children.get(e["child_table"])
            snap_broken = {
                (m["parent_table"], m["parent_field"])
                for m in (snap["missing_edges"] if snap else [])
            }
            if not snap:
                chip = "未冻结"
            elif (e["parent_table"], e["parent_field"]) in snap_broken:
                chip = "缺边"
            else:
                chip = "齐全"
            out_edges.append(
                {
                    **e,
                    "parent_field_exists": e["parent_field"] in field_names,
                    "chip": chip,
                    "snapshot_voucher": snap["voucher"] if snap else "",
                }
            )
        return {
            "table_name": table_name,
            "live_version": live["version_no"] if live else None,
            "note": live["note"] if live else "",
            "fields": fields,
            "edges": out_edges,
            "missing_edges": [e for e in out_edges if e["chip"] == "缺边"],
            "gate_chip": Config.GATE_CHIP,
        }


def waterline(db_path: str | None = None) -> list[dict]:
    """进度水线（总览）：按子表汇总最近一张快照的四个阶段与凭证号。"""
    with get_db(db_path) as conn:
        tables = [
            r["child_table"]
            for r in conn.execute(
                "SELECT DISTINCT child_table FROM lineage_edges ORDER BY child_table"
            )
        ]
        out = []
        for name in tables:
            snap = _latest_snapshot_for_table(conn, name)
            gate = None
            if snap:
                gate = conn.execute(
                    "SELECT * FROM lineage_gates WHERE snapshot_id=?", (snap["id"],)
                ).fetchone()
            cat = conn.execute(
                "SELECT * FROM catalog_tables WHERE table_name=?", (name,)
            ).fetchone()
            out.append(
                {
                    "child_table": name,
                    "frozen": bool(snap),
                    "voucher": snap["voucher"] if snap else "",
                    "verdict": snap["verdict"] if snap else "",
                    "snapshot_status": snap["status"] if snap else "",
                    "decision": gate["decision"] if gate else "",
                    "gate_voucher": gate["voucher"] if gate else "",
                    "live_version": cat["live_version"] if cat else None,
                    "catalog_voucher": cat["gate_voucher"] if cat else "",
                }
            )
        return out
