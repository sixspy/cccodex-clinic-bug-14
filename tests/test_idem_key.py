from __future__ import annotations

import json
import threading

import pytest

from app.db import get_db
from app.services import backfill, idem_key, qc, sites, visits
from app.services.errors import ServiceError


def _payload() -> str:
    return json.dumps(
        {"visit_date": "2026-11-20", "investigator": "王医生", "weight_kg": 61},
        ensure_ascii=False,
    )


def _site(db_path: str) -> dict:
    return next(s for s in sites.list_sites(db_path) if s["name"] == "华东协同中心")


def _new_batch(db_path: str, note: str = "幂等测试批次") -> dict:
    return visits.create_batch(_site(db_path)["id"], note=note, db_path=db_path)


def _ledger_count(db_path: str, batch_id: int) -> int:
    with get_db(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM idem_append_keys WHERE batch_id=?",
            (batch_id,),
        ).fetchone()["c"]


def test_append_writes_voucher_and_counts_align(db_path):
    batch = _new_batch(db_path)
    visits.add_visit_row(batch["id"], "I-0001", "V1", _payload(), db_path=db_path)
    visits.add_visit_row(batch["id"], "I-0002", "V1", _payload(), db_path=db_path)

    ledger = idem_key.list_ledger(batch_id=batch["id"], db_path=db_path)
    assert len(ledger) == 2
    assert {r["subject_key"] for r in ledger} == {"I-0001", "I-0002"}
    assert all(r["charge_id"] for r in ledger)
    assert all(r["side"] == idem_key.SIDE for r in ledger)

    status = idem_key.batch_status(batch["id"], db_path=db_path)
    assert status["item_count"] == 2
    assert status["append_count"] == 2
    assert status["chip"] == idem_key.OK
    assert status["aligned"] is True
    assert status["blocked"] is False
    assert idem_key.gate_allows_append(batch["id"], db_path=db_path) is True


def test_duplicate_append_across_open_batches_is_rejected_and_not_counted(db_path):
    first = _new_batch(db_path, "甲批")
    second = _new_batch(db_path, "乙批")
    visits.add_visit_row(first["id"], "I-0002", "V1", _payload(), db_path=db_path)

    with pytest.raises(ServiceError) as exc:
        visits.add_visit_row(second["id"], "I-0002", "V1", _payload(), db_path=db_path)
    assert exc.value.status_code == 409

    assert _ledger_count(db_path, second["id"]) == 0
    assert visits.get_batch(second["id"], db_path)["item_count"] == 0
    status = idem_key.batch_status(second["id"], db_path=db_path)
    assert status["append_count"] == 0
    assert status["item_count"] == 0
    assert status["chip"] == idem_key.OK

    rejected = [
        a
        for a in idem_key.list_attempts(db_path=db_path)
        if a["outcome"] == idem_key.REJECTED and a["subject_key"] == "I-0002"
    ]
    assert len(rejected) == 1
    assert rejected[0]["batch_id"] == second["id"]


def test_duplicate_append_within_batch_is_rejected_and_logged(db_path):
    batch = _new_batch(db_path)
    visits.add_visit_row(batch["id"], "I-0003", "V1", _payload(), db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        visits.add_visit_row(batch["id"], "I-0003", "V1", _payload(), db_path=db_path)
    assert exc.value.status_code == 409
    assert _ledger_count(db_path, batch["id"]) == 1
    assert idem_key.batch_status(batch["id"], db_path=db_path)["append_count"] == 1


def test_panel_chip_drift_blocks_append_and_refresh(db_path):
    batch = _new_batch(db_path)
    visits.add_visit_row(batch["id"], "I-0004", "V1", _payload(), db_path=db_path)
    # simulate a legacy write that bypassed the ledger
    with get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO visit_rows(batch_id, subject_key, visit_code, payload, quality, status)
               VALUES (?, 'I-0005', 'V2', ?, '合格', '待检')""",
            (batch["id"], _payload()),
        )
    status = idem_key.batch_status(batch["id"], db_path=db_path)
    assert status["item_count"] == 2
    assert status["append_count"] == 1
    assert status["chip"] == idem_key.BAD
    assert status["aligned"] is False
    assert status["blocked"] is True
    assert idem_key.gate_allows_append(batch["id"], db_path=db_path) is False

    with pytest.raises(ServiceError) as exc:
        visits.add_visit_row(batch["id"], "I-0006", "V3", _payload(), db_path=db_path)
    assert exc.value.status_code == 409

    # 甲路：回刷请求被拒，账册不被改写
    result = idem_key.request_refresh(batch["id"], "申请回刷补记凭证", db_path=db_path)
    assert result["rejected"] is True
    assert result["append_count"] == 1
    assert _ledger_count(db_path, batch["id"]) == 1
    outcomes = [a["outcome"] for a in idem_key.list_attempts(db_path=db_path)]
    assert idem_key.REFRESH_REJECTED in outcomes


def test_concurrent_duplicate_writers_pin_exactly_once(db_path):
    site = next(s for s in sites.list_sites(db_path) if s["name"] == "西南观察点")
    batches = [
        visits.create_batch(site["id"], note=f"并发批{i}", db_path=db_path)
        for i in range(3)
    ]
    results: list[str] = []
    barrier = threading.Barrier(len(batches))

    def worker(batch_id: int) -> None:
        barrier.wait()
        try:
            visits.add_visit_row(batch_id, "C-9001", "V1", _payload(), db_path=db_path)
            results.append("ok")
        except ServiceError as exc:
            results.append(f"rejected:{exc.status_code}")
        except Exception as exc:  # noqa: BLE001 - surface lock errors in tests
            results.append(f"error:{exc}")

    threads = [threading.Thread(target=worker, args=(b["id"],)) for b in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, results
    assert results.count("rejected:409") == len(batches) - 1, results
    with get_db(db_path) as conn:
        pinned = conn.execute(
            """SELECT COUNT(*) AS c FROM idem_append_keys
               WHERE subject_key='C-9001' AND state='pinned'"""
        ).fetchone()["c"]
    assert pinned == 1


def test_closed_batch_releases_idempotency_key(db_path):
    first = _new_batch(db_path, "将关闭")
    visits.add_visit_row(first["id"], "R-0001", "V1", _payload(), db_path=db_path)
    visits.close_batch(first["id"], db_path=db_path)

    second = _new_batch(db_path, "承接批")
    row = visits.add_visit_row(second["id"], "R-0001", "V1", _payload(), db_path=db_path)
    assert row["quality"] == "合格"
    assert _ledger_count(db_path, second["id"]) == 1


def test_seed_rows_are_vouchered_and_board_aligned(db_path):
    with get_db(db_path) as conn:
        rows = conn.execute(
            """SELECT COUNT(*) AS c FROM visit_rows r
               WHERE NOT EXISTS (
                 SELECT 1 FROM idem_append_keys k WHERE k.row_id=r.id
               )"""
        ).fetchone()["c"]
    assert rows == 0
    board = idem_key.board(db_path=db_path)
    assert board["side"] == idem_key.SIDE
    assert board["aligned"] is True
    assert board["mismatch_count"] == 0
    assert len(board["rows"]) == len(visits.list_batches(db_path))


def test_qc_scan_cannot_paint_a_broken_read_track_green(db_path):
    batch = _new_batch(db_path)
    visits.add_visit_row(batch["id"], "Q-0001", "V1", _payload(), db_path=db_path)
    with get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO visit_rows(batch_id, subject_key, visit_code, payload, quality, status)
               VALUES (?, 'Q-0002', 'V2', '', '编码空', '待检')""",
            (batch["id"],),
        )
    qc.scan_batch(batch["id"], db_path=db_path)
    status = idem_key.batch_status(batch["id"], db_path=db_path)
    assert status["chip"] == idem_key.BAD
    assert status["aligned"] is False


def test_backfill_success_does_not_launder_duplicate_key(db_path):
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM visit_rows WHERE quality='重复键' LIMIT 1"
        ).fetchone()
    assert row is not None
    job = backfill.enqueue_from_row(row["id"], db_path=db_path)
    done = backfill.run_job(job["id"], succeed=True, db_path=db_path)
    assert done["status"] == "失败"
    with get_db(db_path) as conn:
        after = conn.execute(
            "SELECT quality FROM visit_rows WHERE id=?", (row["id"],)
        ).fetchone()
    assert after["quality"] == "重复键"


def test_batch_detail_counts_come_from_live_rows_not_stale_cache(db_path):
    """详情面板的访视行/缺口与徽标同源：直插行造成缓存列过期时不能再显示旧值。"""
    batch = _new_batch(db_path, "详情同源批")
    visits.add_visit_row(batch["id"], "D-0001", "V1", _payload(), db_path=db_path)
    with get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO visit_rows(batch_id, subject_key, visit_code, payload, quality, status)
               VALUES (?, 'D-0002', 'V1', '', '缺字段', '待检')""",
            (batch["id"],),
        )
    detail = visits.get_batch(batch["id"], db_path=db_path)
    assert detail["item_count"] == 2
    assert detail["gap_count"] == 1
    assert detail["append_count"] == 1
    assert detail["idem_aligned"] is False
    assert detail["idem_chip"] == idem_key.BAD
    with pytest.raises(ServiceError) as err:
        visits.add_visit_row(batch["id"], "D-0003", "V1", _payload(), db_path=db_path)
    assert err.value.status_code == 409
