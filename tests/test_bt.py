"""P3-1：BT 子进程协议 + 跨进程暂停恢复（离线，本机做种）。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent", reason="需要 libtorrent（P3 可选依赖）")

from downloadx.bt import BtClient, BtError  # noqa: E402
from tests.conftest import run  # noqa: E402

SIZE = 4 * 1024 * 1024
PIECE = 16 * 1024


@pytest.fixture()
def seeded(tmp_path):
    """本机做种端：生成 4MB 数据、建种子、开一个只读的海种会话。"""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    payload = seed_dir / "payload.bin"
    data = random.Random(7).randbytes(SIZE)
    payload.write_bytes(data)

    fs = lt.file_storage()
    lt.add_files(fs, str(payload))
    creator = lt.create_torrent(fs, PIECE)
    lt.set_piece_hashes(creator, str(seed_dir))
    torrent_path = tmp_path / "payload.torrent"
    torrent_path.write_bytes(lt.bencode(creator.generate()))
    ti = lt.torrent_info(str(torrent_path))

    session = lt.session({"listen_interfaces": "127.0.0.1:0"})
    params = lt.add_torrent_params()
    params.ti = ti
    params.save_path = str(seed_dir)
    params.flags |= lt.torrent_flags.seed_mode
    handle = session.add_torrent(params)
    deadline = time.time() + 15
    while not handle.is_seed() and time.time() < deadline:
        time.sleep(0.05)
    assert handle.is_seed(), "做种端没就绪"

    yield {
        "data": data,
        "digest": hashlib.sha256(data).hexdigest(),
        "torrent": torrent_path,
        "peer": ("127.0.0.1", session.listen_port()),
    }
    session.pause()


def test_bt_subprocess_download_and_cross_process_resume(tmp_path, seeded):
    down_dir = tmp_path / "down"
    down_dir.mkdir()

    async def first_pass() -> dict:
        client = BtClient()
        await client.start()
        try:
            info = await client.add(
                torrent_path=seeded["torrent"],
                save_path=down_dir,
                peers=[seeded["peer"]],
                download_limit=2 * 1024 * 1024,
            )
            assert info["restored"] is False, "第一次不该有 resume data"
            current = await client.wait(info["id"], until="30", timeout=90)
            paused = await client.pause(info["id"])
            return {
                "id": info["id"],
                "pieces": paused["pieces"],
                "percent": current["percent"],
                "resume_file": Path(info["resume_file"]),
            }
        finally:
            await client.close()

    paused = run(first_pass())
    assert paused["pieces"] > 0, f"暂停时应有已完成分片：{paused}"
    assert paused["percent"] >= 30, paused

    async def second_pass() -> dict:
        client = BtClient()
        await client.start()
        try:
            info = await client.add(
                torrent_path=seeded["torrent"],
                save_path=down_dir,
                peers=[seeded["peer"]],
            )
            assert info["restored"] is True, "新进程应当读到了 resume data"
            final = await client.wait(info["id"], until="completed", timeout=120)
            return final
        finally:
            await client.close()

    final = run(second_pass())
    assert final["is_seeding"] or final["percent"] >= 100.0, final
    saved = down_dir / "payload.bin"
    assert saved.exists()
    assert saved.read_bytes() == seeded["data"], "跨进程续传后文件必须逐字节一致"


def test_bt_client_reports_missing_task(tmp_path):
    async def scenario():
        client = BtClient()
        await client.start()
        try:
            with pytest.raises(BtError):
                await client.pause("not-a-real-hash")
        finally:
            await client.close()

    run(scenario())
