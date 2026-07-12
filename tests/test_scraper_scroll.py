"""滚动回退模式的终止性（issue #25：全量采集卡在一个会话里无限循环）。

reporter 的日志（#410~#440）里三个数字说明了一切：

    saved=0  new=0  msgs=13  scrollTop=1944  stuck=15  noNew=440  oldestStable=22

- `saved=0` 是累计值：440 轮一条都没存下。能读到 13 条消息却存不下，只有一种可能——
  这些消息 content 是空的，`_store_messages` 里 `if not content: continue` 全跳过了。
- 于是 `newly_inserted` 恒为 0，`noNew` 单调涨到 440+。
- 而另外两个退出条件都是"连续计数"：虚拟列表一抖动（scrollTop 1944↔1655、
  最旧可见时间偶尔变一下），`stuck` 和 `oldestStable` 就被清零，永远够不到 60/100。

结果：`while True` 里三个退出条件全都够不着，唯一单调的进度信号 noNew 没人看。
"""
import asyncio

import pytest

from common.db import connect, upsert_conversation
from extractor.web_scraper import WebChatScraper

CONV = "conv_osc"
MAX_ROUNDS = 900  # 远超任何合法退出所需轮数；超了就是死循环


class _Mouse:
    async def move(self, x, y):
        pass

    async def wheel(self, dx, dy):
        pass


class _Handle:
    async def bounding_box(self):
        return {"x": 0, "y": 0, "width": 800, "height": 600}


class _Page:
    def __init__(self):
        self.mouse = _Mouse()

    async def query_selector(self, sel):
        return _Handle()

    async def evaluate(self, script, arg=None):
        return None


def _blank_msg(server_id, created_at, idx):
    """能被读到、但 content 为空 —— 永远存不进库（reporter 的 saved=0）。"""
    return {
        "server_id": str(server_id),
        "content": "",
        "msg_type": "other",
        "created_at": created_at,
        "virtual_index": idx,
        "virtual_height": 60,
        "sender_uid": "u1",
        "is_self": False,
        "order_high": 0,
        "order_low": int(server_id),
    }


@pytest.fixture
def stuck_scraper(temp_db, monkeypatch):
    async def _no_sleep(*a, **k):
        pass

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    s = WebChatScraper(incremental=False)
    s._db_conn = connect(foreign_keys=True)
    upsert_conversation(s._db_conn, CONV, name="卡住的会话")
    s._db_conn.commit()
    s.page = _Page()
    s.rounds = 0

    async def _read_messages():
        s.rounds += 1
        if s.rounds > MAX_ROUNDS:
            raise AssertionError(
                f"滚动循环没有终止：已跑 {s.rounds} 轮仍在转（issue #25 死循环）"
            )
        # 每 40 轮抖一下最旧可见时间 → oldestStable 够不到 100
        jitter = s.rounds // 40
        return [
            _blank_msg(1, f"2026-07-08T13:12:{14 + jitter:02d}Z", 41),
            _blank_msg(2, "2026-07-08T14:37:54Z", 28),
        ]

    async def _get_scroll_info():
        # 每 10 轮动一下 scrollTop → stuck 够不到 60
        top = 1944 if (s.rounds // 10) % 2 else 1655
        return {"scrollTop": top, "scrollHeight": 50000, "clientHeight": 600,
                "scrollable": True, "tagName": "DIV", "className": "list"}

    async def _js_scroll(delta):
        pass

    s._read_messages = _read_messages
    s._get_scroll_info = _get_scroll_info
    s._js_scroll = _js_scroll
    return s


def test_no_progress_loop_terminates(stuck_scraper):
    """一条消息都存不下、且计数器被抖动反复清零时，必须能退出。"""
    s = stuck_scraper
    total_saved, _ = asyncio.run(s._scroll_up_and_collect(CONV, incremental=False))

    assert s.rounds <= MAX_ROUNDS, "循环没有终止"
    assert total_saved == 0
