from __future__ import annotations

import json
import re
from datetime import datetime

from app.db import get_db
from app.services import audit as audit_svc
from app.services import idem_key as idem_key_svc
from app.services import protocols as protocols_svc
from app.services.errors import ServiceError

BATCH_STATUS = ("采集中", "已收齐", "已关闭")
ROW_QUALITY = ("合格", "缺字段", "编码空", "重复键")
ROW_STATUS = ("待检", "已入库", "待补采", "已关闭")


def _refresh_batch_counts(conn, batch_id: int) -> None:
    item_count = conn.execute(
        "SELECT COUNT(*) AS c FROM visit_rows WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    gap_count = conn.execute(
        """
        SELECT COUNT(*) AS c FROM visit_rows
        WHERE batch_id=? AND quality!='合格'
        """,
        (batch_id,),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE visit_batches SET item_count=?, gap_count=? WHERE id=?",
        (item_count, gap_count, batch_id),
    )


def list_batches(
    db_path: str | None = None,
    site_id: int | None = None,
    status: str | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if site_id:
        clauses.append("b.site_id=?")
        params.append(int(site_id))
    if status:
        if status not in BATCH_STATUS:
            raise ServiceError("批次状态无效")
        clauses.append("b.status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT b.*, s.name AS site_name, s.site_code,
                   p.protocol_code, p.version AS protocol_version,
                   (SELECT COUNT(*) FROM visit_rows r WHERE r.batch_id=b.id) AS row_count,
                   (SELECT COUNT(*) FROM idem_append_keys k
                    WHERE k.batch_id=b.id) AS append_count
            FROM visit_batches b
            JOIN trial_sites s ON s.id=b.site_id
            LEFT JOIN protocols p ON p.id=b.protocol_id
            {where}
            ORDER BY b.id DESC
            """,
            params,
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["item_count"] = d["row_count"]
            aligned = d["item_count"] == d["append_count"]
            d["idem_aligned"] = aligned
            d["idem_chip"] = idem_key_svc.OK if aligned else idem_key_svc.BAD
            d["idem_blocked"] = not aligned
            items.append(d)
        return items


def get_batch(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT b.*, s.name AS site_name, s.site_code, s.status AS site_status,
                   p.protocol_code, p.version AS protocol_version, p.title AS protocol_title
            FROM visit_batches b
            JOIN trial_sites s ON s.id=b.site_id
            LEFT JOIN protocols p ON p.id=b.protocol_id
            WHERE b.id=?
            """,
            (batch_id,),
        ).fetchone()
        if not row:
            raise ServiceError("访视批次不存在", 404)
        items = conn.execute(
            """
            SELECT * FROM visit_rows
            WHERE batch_id=?
            ORDER BY id
            """,
            (batch_id,),
        ).fetchall()
        quality_stats = conn.execute(
            """
            SELECT quality, COUNT(*) AS c
            FROM visit_rows WHERE batch_id=?
            GROUP BY quality
            """,
            (batch_id,),
        ).fetchall()
        data = dict(row)
        data["items"] = [dict(i) for i in items]
        data["quality_stats"] = {r["quality"]: r["c"] for r in quality_stats}
        status = idem_key_svc.batch_status(batch_id, db_path)
        data["append_count"] = status["append_count"]
        # 详情与徽标同源：都取当刻访视行实计数，不用批次表里可能过期的缓存列。
        data["item_count"] = status["item_count"]
        data["gap_count"] = sum(
            1 for i in data["items"] if i.get("quality") != "合格"
        )
        data["idem_aligned"] = status["aligned"]
        data["idem_chip"] = status["chip"]
        data["idem_blocked"] = status["blocked"]
        return data


def create_batch(
    site_id: int,
    protocol_id: int | None = None,
    note: str = "",
    db_path: str | None = None,
) -> dict:
    site_id = int(site_id or 0)
    if site_id <= 0:
        raise ServiceError("必须指定试验中心")
    with get_db(db_path, immediate=True) as conn:
        site = conn.execute(
            "SELECT * FROM trial_sites WHERE id=?", (site_id,)
        ).fetchone()
        if not site:
            raise ServiceError("试验中心不存在", 404)
        if site["status"] == "停用":
            raise ServiceError("停用中心不能开新批次")
        active = conn.execute(
            """
            SELECT COUNT(*) AS c FROM visit_batches
            WHERE site_id=? AND status='采集中'
            """,
            (site_id,),
        ).fetchone()["c"]
        if active >= 3:
            raise ServiceError("同一中心采集中批次不能超过 3 个")
        pid = protocol_id
        if pid:
            proto = conn.execute(
                "SELECT * FROM protocols WHERE id=?", (pid,)
            ).fetchone()
            if not proto:
                raise ServiceError("方案版本不存在", 404)
            if proto["status"] != "生效":
                raise ServiceError("只能挂接生效方案")
        else:
            active_proto = protocols_svc.active_protocol(db_path)
            pid = active_proto["id"] if active_proto else None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        seq = conn.execute("SELECT COUNT(*) AS c FROM visit_batches").fetchone()["c"] + 1
        code = f"VB-{site['site_code']}-{stamp}-{seq:03d}"
        cur = conn.execute(
            """
            INSERT INTO visit_batches(batch_code, site_id, protocol_id, status, note)
            VALUES (?, ?, ?, '采集中', ?)
            """,
            (code, site_id, pid, (note or "").strip()[:200]),
        )
        batch_id = cur.lastrowid
    audit_svc.write_event(
        "创建访视批次", "visit_batch", batch_id, code, db_path=db_path
    )
    return get_batch(batch_id, db_path)


def _infer_quality(payload: str, subject_key: str, visit_code: str, required: list[str]) -> str:
    if not subject_key or not visit_code:
        return "编码空"
    if not (payload or "").strip():
        return "编码空"
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return "缺字段"
    if not isinstance(data, dict):
        return "缺字段"
    for field in required:
        if field in ("subject_key", "visit_code"):
            continue
        val = data.get(field)
        if val is None or str(val).strip() == "":
            return "缺字段"
    return "合格"


def add_visit_row(
    batch_id: int,
    subject_key: str,
    visit_code: str,
    payload: str = "",
    quality: str | None = None,
    db_path: str | None = None,
) -> dict:
    subject_key = (subject_key or "").strip()
    visit_code = (visit_code or "").strip().upper()
    payload = payload if payload is not None else ""
    if not subject_key:
        raise ServiceError("受试者键不能空")
    if not re.match(r"^[A-Za-z0-9_-]{2,40}$", subject_key):
        raise ServiceError("受试者键格式无效")
    if not visit_code:
        raise ServiceError("访视编码不能空")
    # 页面上幂等徽标与追加次数对不上时，直接挡住重复追加。
    idem_key_svc.require_aligned_or_raise(batch_id, db_path)
    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM visit_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        if batch["status"] == "已关闭":
            raise ServiceError("已关闭批次不能追加访视行")
        site = conn.execute(
            "SELECT * FROM trial_sites WHERE id=?", (batch["site_id"],)
        ).fetchone()
        if site and site["quota"] > 0:
            if batch["item_count"] >= site["quota"]:
                raise ServiceError("已达中心配额上限，不能再追加")
        required = ["subject_key", "visit_code", "visit_date", "investigator"]
        if batch["protocol_id"]:
            proto = conn.execute(
                "SELECT required_fields FROM protocols WHERE id=?",
                (batch["protocol_id"],),
            ).fetchone()
            if proto:
                try:
                    parsed = json.loads(proto["required_fields"] or "[]")
                    if isinstance(parsed, list) and parsed:
                        required = [str(x) for x in parsed]
                except json.JSONDecodeError:
                    pass
        if quality:
            if quality not in ROW_QUALITY:
                raise ServiceError("质量标记无效")
            q = quality
        else:
            q = _infer_quality(payload, subject_key, visit_code, required)
        # 幂等键：读轨只认旁路账册凭证；同中心开放批内重复即拒收。
        pinned = conn.execute(
            """
            SELECT k.id FROM idem_append_keys k
            JOIN visit_batches b ON b.id=k.batch_id
            WHERE k.site_id=? AND k.subject_key=? AND k.visit_code=?
              AND k.state='pinned' AND b.status!='已关闭'
            LIMIT 1
            """,
            (batch["site_id"], subject_key, visit_code),
        ).fetchone()
        if pinned:
            idem_key_svc.record_attempt_conn(
                conn,
                batch_id,
                subject_key,
                visit_code,
                idem_key_svc.REJECTED,
                "同中心开放批内幂等键已钉定",
            )
            raise ServiceError("同中心开放批内受试者访视键已钉定（重复追加）", 409)
        cur = conn.execute(
            """
            INSERT INTO visit_rows(batch_id, subject_key, visit_code, payload, quality, status)
            VALUES (?, ?, ?, ?, ?, '待检')
            """,
            (batch_id, subject_key, visit_code, payload, q),
        )
        row_id = cur.lastrowid
        # 落库即写凭证：先写行再钉账册，任何一步失败整事务回滚。
        idem_key_svc.pin_conn(
            conn, batch_id, row_id, batch["site_id"], subject_key, visit_code
        )
        _refresh_batch_counts(conn, batch_id)
        if batch["status"] == "已收齐":
            conn.execute(
                "UPDATE visit_batches SET status='采集中' WHERE id=?", (batch_id,)
            )
        row = conn.execute("SELECT * FROM visit_rows WHERE id=?", (row_id,)).fetchone()
    audit_svc.write_event(
        "追加访视行",
        "visit_row",
        row_id,
        f"{subject_key}/{visit_code}:{q}",
        db_path=db_path,
    )
    return dict(row)


def mock_crawl(
    batch_id: int,
    count: int = 5,
    mix_bad: bool = True,
    db_path: str | None = None,
) -> dict:
    count = max(1, min(30, int(count or 5)))
    idem_key_svc.require_aligned_or_raise(batch_id, db_path)
    created = []
    rejected = 0
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM visit_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        if batch["status"] == "已关闭":
            raise ServiceError("已关闭批次不能模拟抓取")
        visit_codes = ["V1", "V2", "V3", "EOT"]
        if batch["protocol_id"]:
            proto = conn.execute(
                "SELECT visit_codes FROM protocols WHERE id=?",
                (batch["protocol_id"],),
            ).fetchone()
            if proto:
                try:
                    parsed = json.loads(proto["visit_codes"] or "[]")
                    if isinstance(parsed, list) and parsed:
                        visit_codes = [str(x) for x in parsed]
                except json.JSONDecodeError:
                    pass
        existing = conn.execute(
            "SELECT COUNT(*) AS c FROM visit_rows WHERE batch_id=?", (batch_id,)
        ).fetchone()["c"]
    for i in range(count):
        idx = existing + i + 1
        subject = f"S{batch_id:02d}-{idx:04d}"
        visit = visit_codes[i % len(visit_codes)]
        if mix_bad and i % 5 == 1:
            payload = json.dumps(
                {"visit_date": "", "investigator": "李医生"}, ensure_ascii=False
            )
            quality = "缺字段"
        elif mix_bad and i % 5 == 3:
            payload = ""
            quality = "编码空"
        else:
            payload = json.dumps(
                {
                    "visit_date": f"2026-11-{(i % 28) + 1:02d}",
                    "investigator": "王医生" if i % 2 == 0 else "赵医生",
                    "weight_kg": 50 + (i % 20),
                    "note": f"模拟抓取行{idx}",
                },
                ensure_ascii=False,
            )
            quality = "合格"
        try:
            created.append(
                add_visit_row(
                    batch_id,
                    subject,
                    visit,
                    payload=payload,
                    quality=quality,
                    db_path=db_path,
                )
            )
        except ServiceError:
            # 受幂等键或中心配额所阻即停，不把虚追加次数灌进列表。
            rejected += 1
            break
    audit_svc.write_event(
        "模拟抓取访视",
        "visit_batch",
        batch_id,
        f"新增{len(created)}行",
        db_path=db_path,
    )
    return {
        "created": len(created),
        "items": created,
        "rejected": rejected,
        "batch": get_batch(batch_id, db_path),
    }


def close_batch(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path, immediate=True) as conn:
        batch = conn.execute(
            "SELECT * FROM visit_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        if batch["status"] == "已关闭":
            return get_batch(batch_id, db_path)
        pending = conn.execute(
            """
            SELECT COUNT(*) AS c FROM visit_rows
            WHERE batch_id=? AND status='待补采'
            """,
            (batch_id,),
        ).fetchone()["c"]
        if pending:
            raise ServiceError("仍有待补采行，不能关闭批次")
        conn.execute(
            "UPDATE visit_batches SET status='已关闭' WHERE id=?", (batch_id,)
        )
        conn.execute(
            """
            UPDATE visit_rows SET status='已关闭'
            WHERE batch_id=? AND status='待检'
            """,
            (batch_id,),
        )
        # 收批即释放幂等键，后续批次可自觉承接同一受试者访视键。
        idem_key_svc.release_for_batch(conn, batch_id)
    audit_svc.write_event("关闭访视批次", "visit_batch", batch_id, "", db_path=db_path)
    return get_batch(batch_id, db_path)


def mark_batch_complete(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM visit_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        if batch["status"] == "已关闭":
            raise ServiceError("已关闭批次不能标为已收齐")
        if batch["item_count"] <= 0:
            raise ServiceError("空批次不能标为已收齐")
        conn.execute(
            "UPDATE visit_batches SET status='已收齐' WHERE id=?", (batch_id,)
        )
    audit_svc.write_event("批次已收齐", "visit_batch", batch_id, "", db_path=db_path)
    return get_batch(batch_id, db_path)


def list_rows(
    db_path: str | None = None,
    batch_id: int | None = None,
    quality: str | None = None,
    status: str | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if batch_id:
        clauses.append("r.batch_id=?")
        params.append(int(batch_id))
    if quality:
        if quality not in ROW_QUALITY:
            raise ServiceError("质量标记无效")
        clauses.append("r.quality=?")
        params.append(quality)
    if status:
        if status not in ROW_STATUS:
            raise ServiceError("访视行状态无效")
        clauses.append("r.status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT r.*, b.batch_code, b.site_id, s.name AS site_name
            FROM visit_rows r
            JOIN visit_batches b ON b.id=r.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            {where}
            ORDER BY r.id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def set_row_status(row_id: int, status: str, db_path: str | None = None) -> dict:
    if status not in ROW_STATUS:
        raise ServiceError("访视行状态无效")
    with get_db(db_path) as conn:
        row = conn.execute("SELECT * FROM visit_rows WHERE id=?", (row_id,)).fetchone()
        if not row:
            raise ServiceError("访视行不存在", 404)
        batch = conn.execute(
            "SELECT status FROM visit_batches WHERE id=?", (row["batch_id"],)
        ).fetchone()
        if batch and batch["status"] == "已关闭" and status != "已关闭":
            raise ServiceError("已关闭批次内行不能改状态")
        conn.execute(
            "UPDATE visit_rows SET status=? WHERE id=?", (status, row_id)
        )
        updated = conn.execute(
            "SELECT * FROM visit_rows WHERE id=?", (row_id,)
        ).fetchone()
    audit_svc.write_event(
        "改访视行状态", "visit_row", row_id, status, db_path=db_path
    )
    return dict(updated)


def ingest_ok_rows(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM visit_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        if batch["status"] == "已关闭":
            raise ServiceError("已关闭批次不能入库")
        cur = conn.execute(
            """
            UPDATE visit_rows
            SET status='已入库'
            WHERE batch_id=? AND quality='合格' AND status='待检'
            """,
            (batch_id,),
        )
        n = cur.rowcount
    audit_svc.write_event(
        "合格行入库", "visit_batch", batch_id, f"入库{n}行", db_path=db_path
    )
    return {"ingested": n, "batch": get_batch(batch_id, db_path)}
