from app.db import get_db, get_tx
from app.services.errors import ServiceError
from app.services.lineage import gate_state


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count, "
            "(SELECT COUNT(*) FROM lineage_edges e WHERE e.child_table=c.table_name) AS lineage_edge_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        return [dict(r) for r in rows]


def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        if not live:
            raise ServiceError("无现行契约可对照", 409)
        old_f = _fields(conn, live["id"])
        new_f = _fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def apply_contract(contract_id: int, db_path: str | None = None) -> dict:
    """换版应用：先过齐套闸门，再在写锁内改现行。

    齐套闸门只认冻结快照的裁决（与 lineage.decide 同一口径），快照未冻结、
    未裁决、判为隔离，或与当前契约对不上，一律拒绝，不做“提交瞬间重扫”。
    """
    with get_tx(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可换版应用", 409)
        snap = gate_state(conn, contract_id)
        gate_voucher = snap["voucher"] if snap else ""
        gate_decision = snap["gate"]["decision"] if snap else "基线放行"
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        from_ver = live["version_no"] if live else None
        if live:
            conn.execute(
                "UPDATE schema_contracts SET status='停用' WHERE id=?",
                (live["id"],),
            )
        conn.execute(
            "UPDATE schema_contracts SET status='现行' WHERE id=?",
            (contract_id,),
        )
        conn.execute(
            "INSERT INTO catalog_tables(table_name, live_version, gate_voucher, gate_decision) "
            "VALUES (?,?,?,?) ON CONFLICT(table_name) DO UPDATE SET "
            "live_version=excluded.live_version, gate_voucher=excluded.gate_voucher, "
            "gate_decision=excluded.gate_decision, registered_at=datetime('now')",
            (draft["table_name"], draft["version_no"], gate_voucher, gate_decision),
        )
        conn.execute(
            "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, result, gate_voucher) "
            "VALUES (?,?,?,?,?,?)",
            (contract_id, draft["table_name"], from_ver, draft["version_no"], "已应用", gate_voucher),
        )
        conn.execute(
            "INSERT INTO lineage_watermarks(child_table, stage, voucher, detail) VALUES (?,?,?,?)",
            (
                draft["table_name"],
                "子表升现行",
                gate_voucher,
                f"v{from_ver}→v{draft['version_no']}（齐套凭证 {gate_voucher}）",
            ),
        )
        row = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        out = dict(row)
        out["gate_voucher"] = gate_voucher
        out["gate_decision"] = gate_decision
        return out
