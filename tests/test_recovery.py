"""单卷恢复服务关键操作自测。

运行: python3 -m pytest tests/ -v
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app import create_app  # noqa: E402

CFG = dict(BLOCK_SIZE=4096, BLOCK_COUNT=32, BACKFILL_DELAY=0.002)


def wait_for(pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def status(c):
    return c.get("/api/status").get_json()


@pytest.fixture()
def client(tmp_path):
    cfg = dict(CFG, DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))
    app = create_app(cfg)
    app.testing = True
    with app.test_client() as c:
        yield c
    app.vol.close()


def test_read_through_backup_and_not_persisted(client):
    """本地缺块 → 从备份读透;读透不落盘(由后台回填负责补齐)。"""
    r = client.get("/api/block/5")
    assert r.status_code == 200
    j = r.get_json()
    assert j["source"] == "backup"
    assert j["text"].startswith("BACKUP-BLOCK-000005|")
    assert j["size"] == 4096
    st = status(client)
    assert st["missing"] == 32, "读透不应落盘"
    assert st["stats"]["backup_reads"] == 1


def test_backfill_completes_and_reads_become_local(client):
    """启动回填 → 全部补齐 → 读取变为本地命中。"""
    r = client.post("/api/backfill/start")
    assert r.status_code == 200
    assert wait_for(lambda: status(client)["missing"] == 0)
    st = status(client)
    assert st["backfill_complete"] and not st["backfill_running"]
    assert st["stats"]["backfilled"] == 32
    j = client.get("/api/block/5").get_json()
    assert j["source"] == "local" and j["origin"] == "backfill"
    assert j["text"].startswith("BACKUP-BLOCK-000005|")


def test_suspend_write_resume_and_late_backup_never_overwrites(client):
    """挂起期间:读被拒、整块写入可提交;放行补齐后业务数据不被覆盖。"""
    assert client.post("/api/block/7/suspend").status_code == 200
    client.post("/api/backfill/start")
    # 其余块都补齐,只剩被挂起的 7
    assert wait_for(lambda: status(client)["missing"] == 1)
    assert 7 in status(client)["suspended"]

    # 挂起期间:缺块读取被拒(不读备份)
    r = client.get("/api/block/7")
    assert r.status_code == 409

    # 挂起期间:同一块的整块写入仍可提交
    r = client.post("/api/block/7/write", json={"data": "BUSINESS-DATA-7"})
    assert r.status_code == 200
    j = client.get("/api/block/7").get_json()
    assert j["source"] == "local" and j["origin"] == "business"
    assert j["text"] == "BUSINESS-DATA-7"

    # 还有缺块时拒绝断开备份(此时其实已补齐 7 以外的块,先验证拒绝逻辑见下)
    client.post("/api/block/7/resume")
    assert wait_for(lambda: status(client)["missing"] == 0)

    # 迟到的备份数据不能覆盖业务写入
    j = client.get("/api/block/7").get_json()
    assert j["origin"] == "business" and j["text"] == "BUSINESS-DATA-7"


def test_disconnect_refused_until_full_then_detached_volume_rw(client):
    """缺块时拒绝断开;补齐后可断开;断开后数据盘仍可读写。"""
    client.post("/api/backfill/start")
    r = client.post("/api/disconnect")
    assert r.status_code == 409
    assert "拒绝" in r.get_json()["message"]

    assert wait_for(lambda: status(client)["missing"] == 0)
    r = client.post("/api/disconnect")
    assert r.status_code == 200
    assert status(client)["backup_disconnected"]

    # 断开后:读本地、写本地都正常
    j = client.get("/api/block/3").get_json()
    assert j["source"] == "local"
    assert client.post("/api/block/3/write", json={"data": "AFTER-DISCONNECT"}).status_code == 200
    j = client.get("/api/block/3").get_json()
    assert j["text"] == "AFTER-DISCONNECT"

    # 断开后不能再回填、不能重复断开
    assert client.post("/api/backfill/start").status_code == 409
    assert client.post("/api/disconnect").status_code == 409


def test_persistence_across_restart(tmp_path):
    """服务重启后保留已落盘的块,并自动继续回填。"""
    cfg = dict(CFG, DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))

    app1 = create_app(cfg)
    c1 = app1.test_client()
    c1.post("/api/block/3/write", json={"data": "PERSIST-ME"})
    c1.post("/api/backfill/start")
    assert wait_for(lambda: c1.get("/api/status").get_json()["present"] >= 8)
    app1.vol.stop()  # 模拟进程停止(保留持久状态)

    app2 = create_app(cfg)
    c2 = app2.test_client()
    try:
        j = c2.get("/api/block/3").get_json()
        assert j["origin"] == "business" and j["text"] == "PERSIST-ME", "已落盘的块应保留"
        st = c2.get("/api/status").get_json()
        assert st["backfill_running"], "重启后应自动继续回填"
        assert wait_for(lambda: c2.get("/api/status").get_json()["missing"] == 0)
    finally:
        app2.vol.close()


def test_write_validation(client):
    """整块写入校验:超 4 KiB 拒绝;块号越界 404。"""
    r = client.post("/api/block/0/write", json={"data": "x" * 5000})
    assert r.status_code == 400
    r = client.post("/api/block/0/write", json={})
    assert r.status_code == 400
    assert client.post("/api/block/999/write", json={"data": "x"}).status_code == 404
    assert client.get("/api/block/999").status_code == 404
    # 恰好 4096 字节可以
    r = client.post("/api/block/0/write", json={"data": "y" * 4096})
    assert r.status_code == 200


def test_concurrent_business_writes_survive_backfill(client):
    """回填进行中并发业务写入:所有业务数据都不被回填覆盖。"""
    client.post("/api/backfill/start")
    written = {}
    for i in range(0, 32, 3):
        assert client.post(f"/api/block/{i}/write", json={"data": f"BIZ-{i}"}).status_code == 200
        written[i] = f"BIZ-{i}"
    assert wait_for(lambda: status(client)["missing"] == 0)
    for i, text in written.items():
        j = client.get(f"/api/block/{i}").get_json()
        assert j["origin"] == "business" and j["text"] == text, f"块 {i} 被回填覆盖"


# ---------------- 清零(范围) ----------------

def test_zero_range_compact_marks_and_reads_return_zero(client):
    """清零范围用紧凑区间保存(自动合并,不逐块记录);新发起的读返回全零。"""
    r = client.post("/api/zero", json={"start": 4, "end": 6})
    assert r.status_code == 200
    j = client.get("/api/block/5").get_json()
    assert j["source"] == "zero" and j["size"] == 4096 and j["text"] == ""
    st = status(client)
    assert st["zeroed"] == 3 and st["missing"] == 29
    assert st["zero_ranges"] == [[4, 6]], "一个连续范围只存一条区间记录"

    # 重叠 + 相邻清零合并成一条;每块只算一次
    client.post("/api/zero", json={"start": 5, "end": 7})   # 与 [4,6] 重叠
    client.post("/api/zero", json={"start": 9, "end": 10})  # 不相邻,单独一条
    client.post("/api/zero", json={"start": 8, "end": 8})   # 桥接相邻区间
    st = status(client)
    assert st["zero_ranges"] == [[4, 10]], "重叠/相邻区间应合并为一条紧凑记录"
    assert st["zeroed"] == 7 and st["missing"] == 25, "重叠块不得重复计数"


def test_zero_releases_local_space_and_counts_progress(client):
    """清零释放已有数据占用的本地空间;清零块计入恢复进度(进度不回退)。"""
    client.post("/api/block/10/write", json={"data": "BIZ-10"})
    client.post("/api/backfill/start")
    assert wait_for(lambda: status(client)["missing"] == 0)
    st = status(client)
    assert st["present"] == 32 and st["progress"] == 100.0

    r = client.post("/api/zero", json={"start": 8, "end": 15})
    assert r.status_code == 200
    st = status(client)
    assert st["present"] == 24, "范围内本地块(含业务写入的块 10)应被删除,释放空间"
    assert st["zeroed"] == 8
    assert st["missing"] == 0 and st["progress"] == 100.0, "清零块计入恢复进度"
    j = client.get("/api/block/10").get_json()
    assert j["source"] == "zero", "清零后新发起的读返回全零,不再是旧数据"


def test_zero_and_write_overlap_last_commit_wins(client):
    """清零与业务整块写入重叠时,以最后提交的操作为准。"""
    client.post("/api/zero", json={"start": 2, "end": 4})
    # 清零后重写块 3:写入生效,块 3 移出清零区间(区间分裂)
    client.post("/api/block/3/write", json={"data": "REWRITE-3"})
    j = client.get("/api/block/3").get_json()
    assert j["origin"] == "business" and j["text"] == "REWRITE-3"
    st = status(client)
    assert st["zero_ranges"] == [[2, 2], [4, 4]] and st["zeroed"] == 2
    # 再对块 3 清零:后提交的清零生效,业务数据被清除
    client.post("/api/zero", json={"start": 3, "end": 3})
    j = client.get("/api/block/3").get_json()
    assert j["source"] == "zero"
    st = status(client)
    assert st["zero_ranges"] == [[2, 4]] and st["present"] == 0


def test_zero_blocks_count_once_and_allow_disconnect(client):
    """清零块计入恢复进度;清零+回填补齐后允许断开备份;断开后清零块仍读全零。"""
    client.post("/api/zero", json={"start": 0, "end": 15})
    client.post("/api/backfill/start")
    assert wait_for(lambda: status(client)["missing"] == 0)
    st = status(client)
    assert st["zeroed"] == 16 and st["present"] == 16
    assert st["stats"]["backfilled"] == 16, "后台回填应跳过清零块"
    r = client.post("/api/disconnect")
    assert r.status_code == 200, "清零块补齐进度,应允许断开备份"
    j = client.get("/api/block/5").get_json()
    assert j["source"] == "zero", "断开后清零块仍返回全零"
    j = client.get("/api/block/20").get_json()
    assert j["source"] == "local" and j["origin"] == "backfill"


def test_inflight_backfill_not_written_back_after_zero(tmp_path):
    """在途回填(已从备份取数、尚未落盘)目标块被清零后,备份数据不得写回。"""
    cfg = dict(CFG, BLOCK_COUNT=8, BACKFILL_DELAY=0.3,
               DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))
    app = create_app(cfg)
    c = app.test_client()
    try:
        c.post("/api/backfill/start")
        # 等 worker 取出块 0(在途),落盘前清零它
        assert wait_for(lambda: status(c)["stats"]["backup_reads"] >= 1
                        and status(c)["stats"]["backfilled"] == 0)
        r = c.post("/api/zero", json={"start": 0, "end": 0})
        assert r.status_code == 200
        assert wait_for(lambda: status(c)["missing"] == 0, timeout=20)
        j = c.get("/api/block/0").get_json()
        assert j["source"] == "zero", "在途回填数据不得写回已清零块"
        assert status(c)["stats"]["zero_dropped"] >= 1
    finally:
        app.vol.close()


def test_zero_invalid_range_keeps_data_and_progress(client):
    """清零操作失败时,数据和恢复进度都维持原状。"""
    client.post("/api/block/1/write", json={"data": "KEEP-1"})
    before = status(client)
    for body in ({"start": -1, "end": 3}, {"start": 5, "end": 2},
                 {"start": 0, "end": 999}, {"start": "x", "end": 3}, {}):
        r = client.post("/api/zero", json=body)
        assert r.status_code == 400, body
    after = status(client)
    assert after["present"] == before["present"]
    assert after["zeroed"] == 0 and after["zero_ranges"] == []
    assert after["missing"] == before["missing"] and after["progress"] == before["progress"]
    assert client.get("/api/block/1").get_json()["text"] == "KEEP-1"


def test_zero_persists_across_restart_and_reset_clears(tmp_path):
    """清零状态重启后保留;重置卷时清除。"""
    cfg = dict(CFG, DATA_DIR=str(tmp_path / "data"), BACKUP_IMAGE=str(tmp_path / "backup.img"))
    app1 = create_app(cfg)
    c1 = app1.test_client()
    c1.post("/api/zero", json={"start": 5, "end": 9})
    c1.post("/api/block/7/write", json={"data": "BIZ-7"})  # 清零后重写:块 7 移出清零区间
    app1.vol.stop()  # 模拟进程停止

    app2 = create_app(cfg)
    c2 = app2.test_client()
    try:
        st = status(c2)
        assert st["zeroed"] == 4 and st["zero_ranges"] == [[5, 6], [8, 9]], "清零标记应跨重启保留"
        assert c2.get("/api/block/6").get_json()["source"] == "zero"
        assert c2.get("/api/block/7").get_json()["text"] == "BIZ-7"
        # 重置卷 → 清零标记清除,块重新从备份读透
        c2.post("/api/reset")
        st = status(c2)
        assert st["zeroed"] == 0 and st["zero_ranges"] == [] and st["missing"] == 32
        assert c2.get("/api/block/6").get_json()["source"] == "backup"
    finally:
        app2.vol.close()


def test_zero_clears_suspend_and_suspended_zeroed_block_reads_zero(client):
    """清零范围同时清除范围内挂起;清零后读返回全零而非 409。"""
    client.post("/api/block/7/suspend")
    assert 7 in status(client)["suspended"]
    client.post("/api/zero", json={"start": 7, "end": 7})
    st = status(client)
    assert 7 not in st["suspended"], "清零应清除范围内挂起(块已不需要备份)"
    j = client.get("/api/block/7").get_json()
    assert j["source"] == "zero"
