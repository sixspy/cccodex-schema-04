"""目录对账：契约版本对齐 + 齐套凭证回看。

对账只回看冻结快照与裁决留证，不重新扫描父表血缘（重扫是依赖目录上的显式动作），
因此报表里的齐套结论一定与升版卡片上的芯片一致。
"""

from app.db import get_db


def _lineage_view(conn) -> tuple[int, int, list[str], list[dict]]:
    """按子表回看：挂血缘边的子表，其齐套凭证必须与冻结快照的裁决一致。

    只看回看留证（冻结快照 + 裁决 + 目录凭证），不重扫父表；重扫是依赖目录上的
    显式动作，这样报表结论才能与升版卡片上的芯片逐条对齐。
    """
    aligned = 0
    isolated = 0
    problems: list[str] = []
    rows: list[dict] = []

    child_tables = [
        r["child_table"]
        for r in conn.execute(
            "SELECT DISTINCT child_table FROM lineage_edges ORDER BY child_table"
        )
    ]
    live_tables = {
        r["table_name"]: r
        for r in conn.execute("SELECT * FROM catalog_tables ORDER BY table_name")
    }
    for name in child_tables:
        cat = live_tables.get(name)
        snap = conn.execute(
            "SELECT * FROM lineage_snapshots WHERE child_table=? AND status!='已作废' "
            "ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if not cat and not snap:
            # 既没升现行、也没冻结过快照的草稿不在回看范围。
            continue
        gate = None
        gate = None
        if snap:
            gate = conn.execute(
                "SELECT * FROM lineage_gates WHERE snapshot_id=?", (snap["id"],)
            ).fetchone()
        cat_voucher = cat["gate_voucher"] if cat else ""
        ok = bool(
            cat
            and snap
            and gate
            and snap["verdict"] == "齐套"
            and gate["decision"] == "齐套放行"
            and cat_voucher == snap["voucher"]
        )
        if ok:
            aligned += 1
        else:
            isolated += 1
            problems.append(
                f"{name}:凭证{cat_voucher or '无'}/快照{snap['voucher'] if snap else '无'}"
            )
        rows.append(
            {
                "table_name": name,
                "live_version": cat["live_version"] if cat else None,
                "snapshot_voucher": snap["voucher"] if snap else "",
                "snapshot_verdict": snap["verdict"] if snap else "",
                "gate_decision": gate["decision"] if gate else "",
                "catalog_voucher": cat_voucher,
                "aligned": ok,
            }
        )

    # 有冻结快照但还没裁决的草稿也算“对齐待办”，让水线一处可见。
    for r in conn.execute(
        "SELECT s.* FROM lineage_snapshots s WHERE s.status='冻结' ORDER BY s.id"
    ):
        problems.append(f"{r['child_table']}:快照已冻结待裁决")
        rows.append(
            {
                "table_name": r["child_table"],
                "live_version": None,
                "snapshot_voucher": r["voucher"],
                "snapshot_verdict": r["verdict"],
                "gate_decision": "",
                "catalog_voucher": "",
                "aligned": False,
                "pending": True,
            }
        )
    return aligned, isolated, problems, rows


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        contracts = conn.execute(
            "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
        ).fetchall()
        catalog = {
            r["table_name"]: r["live_version"]
            for r in conn.execute("SELECT table_name, live_version FROM catalog_tables")
        }
        matched = 0
        drifted = 0
        details = []
        for c in contracts:
            live = catalog.get(c["table_name"])
            if live == c["version_no"]:
                matched += 1
            else:
                drifted += 1
                details.append(f"{c['table_name']}:契约v{c['version_no']}/目录v{live}")

        lin_aligned, lin_isolated, lin_problems, lin_rows = _lineage_view(conn)
        lineage_aligned = lin_isolated == 0
        lineage_summary = f"血缘齐套：齐套{lin_aligned}，隔离/待办{lin_isolated}"
        if lin_problems:
            lineage_summary += "；" + "，".join(lin_problems)

        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}"
            + (("；" + "，".join(details)) if details else "")
        )
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary, lineage_summary, lineage_aligned) "
            "VALUES (?,?,?,?,?,?)",
            (as_of, matched, drifted, summary, lineage_summary, 1 if lineage_aligned else 0),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        out = dict(row)
        out["lineage_rows"] = lin_rows
        return out


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
