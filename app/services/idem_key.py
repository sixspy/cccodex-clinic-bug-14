"""幂等键（甲路）：旁路账册凭证。

命运骨架两侧档（已备齐，择一落地）：

* 甲路 ``bypass_ledger``（**已选**）：每次被接受的可重复追加写一张旁路账册凭证，
  读轨只认这张账册；批次关闭时凭证转为 ``released`` 释放幂等键；任何"回刷"
  请求一律拒绝，账册不可被事后追补改写。
* 乙路 ``readonly_history``（未选）：把幂等键迁入只读历史后表，再改读新旗面。
  该路会让读轨与页面上追加次数短暂分叉，且新旗面仍需二次回刷，故弃用。

徽标只由账册与访视行数是否对齐推出，不再由任何"亮灯"操作直接涂抹：
``append_count``（账册凭证数）与 ``item_count``（访视行数）一致才是 ``幂等已亮``。
两者对不上即 ``重复追加``，并挡住后续追加。
"""
from __future__ import annotations

import sqlite3

from app.db import get_db
from app.services.errors import ServiceError

SIDE = "bypass_ledger"
FATE = SIDE
REJECTED_FATE = "readonly_history"

OK = "幂等已亮"
BAD = "重复追加"

ACCEPTED = "accepted"
REJECTED = "rejected"
REFRESH_REJECTED = "refresh_rejected"

_TITLE = "幂等键"
_CONFLICT = "重复键：同一中心开放批内已钉定该受试者访视键"


def _attempt(conn, batch_id: int, subject_key: str, visit_code: str, outcome: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO idem_append_attempts(batch_id, subject_key, visit_code, outcome, reason)
           VALUES (?, ?, ?, ?, ?)""",
        (int(batch_id or 0), subject_key or "", visit_code or "", outcome, reason or ""),
    )


def record_attempt(
    batch_id: int,
    subject_key: str,
    visit_code: str,
    outcome: str,
    reason: str = "",
    db_path: str | None = None,
) -> None:
    """在独立事务里落一次追加尝试；用于主事务回滚后仍留下拒收痕迹。"""
    with get_db(db_path) as conn:
        _attempt(conn, batch_id, subject_key, visit_code, outcome, reason)


def record_attempt_conn(
    conn,
    batch_id: int,
    subject_key: str,
    visit_code: str,
    outcome: str,
    reason: str = "",
) -> None:
    """在调用方已持有的连接上落一次尝试，并立刻提交，保证随后的回滚不会抹掉它。"""
    _attempt(conn, batch_id, subject_key, visit_code, outcome, reason)
    conn.commit()


def _pinned_voucher(conn, site_id: int, subject_key: str, visit_code: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM idem_append_keys
           WHERE site_id=? AND subject_key=? AND visit_code=? AND state='pinned'""",
        (int(site_id), subject_key, visit_code),
    ).fetchone()


def pin_conn(
    conn,
    batch_id: int,
    row_id: int,
    site_id: int,
    subject_key: str,
    visit_code: str,
) -> str:
    """在主事务内钉定一张凭证；重复键抛 409。"""
    if _pinned_voucher(conn, site_id, subject_key, visit_code):
        raise ServiceError(_CONFLICT, 409)
    charge_id = f"CK-{int(batch_id):06d}-{int(row_id):06d}"
    try:
        conn.execute(
            """INSERT INTO idem_append_keys(
                 batch_id, row_id, site_id, subject_key, visit_code, charge_id, side, state
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pinned')""",
            (batch_id, row_id, site_id, subject_key, visit_code, charge_id, SIDE),
        )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(_CONFLICT, 409) from exc
    _attempt(conn, batch_id, subject_key, visit_code, ACCEPTED, charge_id)
    return charge_id


def release_for_batch(conn, batch_id: int) -> int:
    """批次关闭时释放幂等键，让其他批次可再次承接同一受试者访视键。"""
    cur = conn.execute(
        """UPDATE idem_append_keys SET state='released'
           WHERE batch_id=? AND state='pinned'""",
        (int(batch_id),),
    )
    return cur.rowcount or 0


def voucher_for_row(row_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM idem_append_keys WHERE row_id=?", (int(row_id),)
        ).fetchone()
        return dict(row) if row else None


def list_ledger(
    batch_id: int | None = None, db_path: str | None = None
) -> list[dict]:
    with get_db(db_path) as conn:
        if batch_id:
            rows = conn.execute(
                """SELECT * FROM idem_append_keys WHERE batch_id=?
                   ORDER BY id""",
                (int(batch_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM idem_append_keys ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def list_attempts(db_path: str | None = None, limit: int = 200) -> list[dict]:
    limit = max(1, min(1000, int(limit or 200)))
    with get_db(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM idem_append_attempts
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _counts(conn, batch_id: int) -> tuple[int, int]:
    item_count = conn.execute(
        "SELECT COUNT(*) AS c FROM visit_rows WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    append_count = conn.execute(
        "SELECT COUNT(*) AS c FROM idem_append_keys WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    return item_count, append_count


def batch_status(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        batch = conn.execute(
            "SELECT id, batch_code, status FROM visit_batches WHERE id=?",
            (int(batch_id),),
        ).fetchone()
        if not batch:
            raise ServiceError("访视批次不存在", 404)
        item_count, append_count = _counts(conn, int(batch_id))
        pinned = conn.execute(
            """SELECT COUNT(*) AS c FROM idem_append_keys
               WHERE batch_id=? AND state='pinned'""",
            (int(batch_id),),
        ).fetchone()["c"]
    aligned = item_count == append_count
    return {
        "batch_id": int(batch_id),
        "batch_code": batch["batch_code"],
        "status": batch["status"],
        "item_count": item_count,
        "append_count": append_count,
        "pinned_count": pinned,
        "aligned": aligned,
        "chip": OK if aligned else BAD,
        "blocked": not aligned,
        "side": SIDE,
    }


def gate_allows_append(batch_id: int, db_path: str | None = None) -> bool:
    return batch_status(batch_id, db_path)["aligned"]


def require_aligned_or_raise(batch_id: int, db_path: str | None = None) -> None:
    status = batch_status(batch_id, db_path)
    if not status["aligned"]:
        raise ServiceError(
            f"{_TITLE}读轨未对齐（账册{status['append_count']} / 访视行{status['item_count']}），"
            "已挡重复追加",
            409,
        )


def request_refresh(batch_id: int, note: str = "", db_path: str | None = None) -> dict:
    """甲路拒回刷：账册只认当刻凭证，任何回刷申请一律拒绝。"""
    status = batch_status(batch_id, db_path)
    record_attempt(
        batch_id,
        "",
        "",
        REFRESH_REJECTED,
        f"{note or '申请回刷'} | 甲路拒回刷",
        db_path=db_path,
    )
    return {
        "rejected": True,
        "batch_id": int(batch_id),
        "append_count": status["append_count"],
        "item_count": status["item_count"],
        "aligned": status["aligned"],
        "side": SIDE,
        "message": "甲路账册拒回刷：读轨只认旁路账册当刻凭证",
    }


def board(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        batches = conn.execute(
            """SELECT b.id, b.batch_code, b.status, s.name AS site_name,
                      (SELECT COUNT(*) FROM visit_rows r WHERE r.batch_id=b.id) AS item_count,
                      (SELECT COUNT(*) FROM idem_append_keys k WHERE k.batch_id=b.id) AS append_count
               FROM visit_batches b
               JOIN trial_sites s ON s.id=b.site_id
               ORDER BY b.id DESC"""
        ).fetchall()
        voucher_total = conn.execute(
            "SELECT COUNT(*) AS c FROM idem_append_keys"
        ).fetchone()["c"]
        pinned_total = conn.execute(
            "SELECT COUNT(*) AS c FROM idem_append_keys WHERE state='pinned'"
        ).fetchone()["c"]
        released_total = conn.execute(
            "SELECT COUNT(*) AS c FROM idem_append_keys WHERE state='released'"
        ).fetchone()["c"]
        attempts = conn.execute(
            "SELECT COUNT(*) AS c FROM idem_append_attempts"
        ).fetchone()["c"]
    rows = []
    mismatch = 0
    for b in batches:
        aligned = b["item_count"] == b["append_count"]
        if not aligned:
            mismatch += 1
        rows.append(
            {
                "batch_id": b["id"],
                "batch_code": b["batch_code"],
                "status": b["status"],
                "site_name": b["site_name"],
                "item_count": b["item_count"],
                "append_count": b["append_count"],
                "aligned": aligned,
                "chip": OK if aligned else BAD,
                "blocked": not aligned,
            }
        )
    return {
        "title": _TITLE,
        "label": "幂等钉定",
        "side": SIDE,
        "fate": FATE,
        "rejected_fate": REJECTED_FATE,
        "chip": OK if mismatch == 0 else BAD,
        "aligned": mismatch == 0,
        "mismatch_count": mismatch,
        "voucher_total": voucher_total,
        "pinned_total": pinned_total,
        "released_total": released_total,
        "attempt_total": attempts,
        "rows": rows,
        "ok_chip": OK,
        "bad_chip": BAD,
        "gate": "append_blocked_on_drift",
        "refresh": "rejected",
    }


def dashboard(db_path: str | None = None) -> dict:
    b = board(db_path)
    b["recent_attempts"] = list_attempts(db_path, limit=20)
    return b


def seed_vouchers(db_path: str | None = None, conn=None) -> int:
    """为已有访视行补齐账册凭证（种子与历史数据迁移共用）。"""
    if conn is not None:
        return _seed_vouchers_on(conn)
    with get_db(db_path) as own:
        return _seed_vouchers_on(own)


def _seed_vouchers_on(conn) -> int:
    made = 0
    missing = conn.execute(
        """SELECT r.id, r.batch_id, r.subject_key, r.visit_code,
                  b.site_id, b.status AS batch_status
           FROM visit_rows r
           JOIN visit_batches b ON b.id=r.batch_id
           LEFT JOIN idem_append_keys k ON k.row_id=r.id
           WHERE k.id IS NULL
           ORDER BY r.id"""
    ).fetchall()
    for r in missing:
        charge_id = f"CK-{int(r['batch_id']):06d}-{int(r['id']):06d}"
        state = "released" if r["batch_status"] == "已关闭" else "pinned"
        try:
            conn.execute(
                """INSERT INTO idem_append_keys(
                     batch_id, row_id, site_id, subject_key, visit_code,
                     charge_id, side, state
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["batch_id"],
                    r["id"],
                    r["site_id"],
                    r["subject_key"],
                    r["visit_code"],
                    charge_id,
                    SIDE,
                    state,
                ),
            )
            made += 1
        except sqlite3.IntegrityError:
            # 同一中心开放批已有钉定凭证：记冲突，不覆盖原券
            conn.execute(
                """INSERT INTO idem_append_attempts(
                     batch_id, subject_key, visit_code, outcome, reason
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    r["batch_id"],
                    r["subject_key"],
                    r["visit_code"],
                    REJECTED,
                    "历史行与已钉定凭证重复",
                ),
            )
    return made
