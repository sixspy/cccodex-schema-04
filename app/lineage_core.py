"""血缘齐套的纯规则函数（不依赖数据库，便于库内种子与在线裁决共用同一口径）。"""

import hashlib
import json


def missing_edges(edges: list[dict], parents: dict[str, dict]) -> list[dict]:
    """返回父表侧断开的血缘边。

    “缺边”只按父表现行契约判定：边引用的父表列不在父表现行字段里即断；
    父表没有现行契约时，挂在该父表上的整组边都算断。子表侧字段是否存在
    不影响判定（子表草稿本来就要新增列），但会在结果里带上子表列名。
    """
    broken = []
    for e in edges:
        parent = parents.get(e["parent_table"])
        if parent is None or parent.get("version") is None:
            broken.append(dict(e, reason=f"父表 {e['parent_table']} 无现行契约"))
            continue
        if e["parent_field"] not in parent["fields"]:
            broken.append(
                dict(e, reason=f"父表 {e['parent_table']} 现行 v{parent['version']} 缺列 {e['parent_field']}")
            )
    return broken


def edge_digest(child_table: str, parents: dict[str, dict], edges: list[dict]) -> str:
    """把“各父表现行版本与字段 + 依赖边”压成指纹，用于判断冻结快照之后是否漂移。"""
    payload = {
        "child": child_table,
        "parents": {
            name: {
                "version": p.get("version"),
                "fields": sorted(p.get("fields", [])),
            }
            for name, p in sorted(parents.items())
        },
        "edges": sorted(
            [
                {
                    "parent_table": e["parent_table"],
                    "parent_field": e["parent_field"],
                    "child_field": e["child_field"],
                }
                for e in edges
            ],
            key=lambda e: (e["parent_table"], e["parent_field"], e["child_field"]),
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def voucher_for(snapshot_id: int, digest: str) -> str:
    """凭证号：快照序号 + 边指纹前六位，齐套与隔离共用同一编号体系。"""
    return f"SNAP-{snapshot_id:04d}-{digest[:6].upper()}"
