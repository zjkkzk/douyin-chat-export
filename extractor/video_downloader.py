"""Backfill self-recorded video MP4 files via the IM batch_play_info API.

Discovery (see tools/probe_video_url.py history):
  POST https://www.douyin.com/aweme/v1/web/maya/story/batch_play_info/v1/
  Body: {"req_infos":[{"item_id":0,"tos_key":"vid-...","type":2},...],"with_caption":true}
  Resp: {"data":{"play_infos":[{"encrypted_url":{"main_url":"https://...douyinvod.com/.../?biz_tag=aweme_im&..."},
                                  "original_encrypted_url":{"main_url":"...","data_size":1528396}},...]}}

The URL is signed but server returns ~1h expiry. Cookies alone authenticate;
msToken/a_bogus are NOT required when calling from the page context (or any
context with the login cookie).

Strategy:
  1. Open Playwright once → /chat (loads cookies + SDK warm-up)
  2. Batch DB vids in groups of N → page.evaluate(fetch) → get URLs
  3. Download each MP4 directly via urllib, save to data/media/videos/<server_id>.mp4
  4. Update messages.media_local_path
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.web_scraper import WebChatScraper
from extractor.models import get_db


VIDEOS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "media", "videos",
)

BATCH_SIZE = 10
BATCH_API_PATH = (
    "/aweme/v1/web/maya/story/batch_play_info/v1/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web"
    "&app_name=douyin_web&pc_client_type=1"
)


def pending_videos(conn, conv_id=None, limit=None):
    """List video messages missing a local mp4.

    The SQL pre-filter is broad (any 'tkey' anywhere in raw_data), which
    catches text replies whose ref_msg quotes a video — we then drop those
    in Python by requiring cj.video.tkey to actually exist on the message.
    """
    sql = """SELECT msg_id, conv_id, timestamp, raw_data, media_local_path
             FROM messages
             WHERE (msg_type = 5 OR (raw_data LIKE '%tkey%' AND raw_data LIKE '%poster%'))
               AND (media_local_path IS NULL OR media_local_path = ''
                    OR media_local_path NOT LIKE '%.mp4')"""
    args = []
    if conv_id:
        sql += " AND conv_id = ?"
        args.append(conv_id)
    sql += " ORDER BY timestamp DESC"
    rows = conn.execute(sql, args).fetchall()

    # Filter: must have cj.video.tkey (excludes text replies whose ref_msg
    # contains a quoted video).
    out = []
    for r in rows:
        if _msg_tkey(r):
            out.append(r)
            if limit and len(out) >= limit:
                break
    return out


def _msg_tkey(msg) -> str | None:
    """Extract video tkey ('vid-xxx') from a message row."""
    try:
        ro = json.loads(msg["raw_data"])
        cj = json.loads(ro.get("content_json", "{}"))
        return (cj.get("video") or {}).get("tkey")
    except Exception:
        return None


def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


async def _resolve_batch(page, tkeys: list[str]) -> dict[str, str]:
    """Resolve a batch of tos_keys to main_url. Returns {tkey: url} for successful ones."""
    result = await page.evaluate(
        r"""async ({path, tkeys}) => {
            try {
                const body = JSON.stringify({
                    req_infos: tkeys.map(k => ({ item_id: 0, tos_key: k, type: 2 })),
                    with_caption: true,
                });
                const r = await fetch(path, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body,
                });
                const text = await r.text();
                return { status: r.status, body: text };
            } catch (e) { return { err: String(e) }; }
        }""", {"path": BATCH_API_PATH, "tkeys": tkeys},
    )
    if result.get("err") or result.get("status") != 200:
        return {}
    try:
        payload = json.loads(result["body"])
    except Exception:
        return {}
    if payload.get("err_no") != 0:
        return {}
    out = {}
    # response play_infos is ordered same as req_infos
    infos = (payload.get("data") or {}).get("play_infos") or []
    for tkey, info in zip(tkeys, infos):
        eu = (info or {}).get("encrypted_url") or {}
        url = eu.get("main_url") or eu.get("backup_url")
        if url:
            out[tkey] = url
    return out


async def backfill(conv_id: str | None = None, limit: int | None = None,
                   batch_size: int = BATCH_SIZE, progress_cb=None) -> dict:
    """Backfill all pending videos. Returns summary dict."""
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    conn = get_db()

    pending = pending_videos(conn, conv_id=conv_id, limit=limit)
    log = [f"[*] {len(pending)} pending videos"]
    if not pending:
        return {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "log": log}

    # Group by tkey availability
    msgs_by_tkey = {}
    no_tkey = 0
    for m in pending:
        tk = _msg_tkey(m)
        if not tk:
            no_tkey += 1
            continue
        msgs_by_tkey.setdefault(tk, []).append(m)
    log.append(f"[*] {no_tkey} skipped (no tkey in cj)")
    log.append(f"[*] {len(msgs_by_tkey)} unique tkeys to resolve")

    ok = 0
    fail = 0
    skipped = no_tkey

    s = WebChatScraper()
    await s.launch()
    if not await s.wait_for_login():
        log.append("[-] login required")
        return {"total": len(pending), "ok": 0, "fail": 0,
                "skipped": skipped, "log": log}
    page = s.page

    log.append("[*] warming SDK at /chat ...")
    await page.goto("https://www.douyin.com/chat", wait_until="commit", timeout=30000)
    await asyncio.sleep(6)

    tkeys = list(msgs_by_tkey.keys())
    log.append(f"[*] resolving in batches of {batch_size} ...")

    for batch_idx in range(0, len(tkeys), batch_size):
        batch = tkeys[batch_idx:batch_idx + batch_size]
        urls = await _resolve_batch(page, batch)
        log.append(f"  batch {batch_idx//batch_size + 1}: requested {len(batch)} → resolved {len(urls)}")

        # Download each URL, save under each owning msg's server_id
        for tk in batch:
            for msg in msgs_by_tkey[tk]:
                sid = msg["msg_id"]
                out_rel = f"videos/{sid}.mp4"
                out_abs = os.path.join(VIDEOS_DIR, f"{sid}.mp4")
                if os.path.exists(out_abs):
                    conn.execute("UPDATE messages SET media_local_path=? WHERE msg_id=?",
                                 (out_rel, sid))
                    conn.commit()
                    skipped += 1
                    continue
                url = urls.get(tk)
                if not url:
                    fail += 1
                    log.append(f"    [-] {sid}: no URL from resolver")
                    continue
                try:
                    data = await asyncio.to_thread(_download, url, 60)
                    if len(data) < 1024 or data[4:8] != b'ftyp':
                        raise ValueError(f"bad mp4 ({len(data)}B magic={data[:8].hex()})")
                    with open(out_abs, "wb") as f:
                        f.write(data)
                    conn.execute("UPDATE messages SET media_local_path=? WHERE msg_id=?",
                                 (out_rel, sid))
                    conn.commit()
                    ok += 1
                except Exception as e:
                    fail += 1
                    log.append(f"    [-] {sid}: download failed: {e}")
                if progress_cb:
                    progress_cb({"ok": ok, "fail": fail, "skipped": skipped,
                                 "total": len(pending), "current": sid})

    try: await asyncio.wait_for(s.close(), timeout=10)
    except Exception: pass

    log.append(f"[+] done: ok={ok} fail={fail} skipped={skipped}")
    return {"total": len(pending), "ok": ok, "fail": fail,
            "skipped": skipped, "log": log}


async def main():
    """CLI: backfill (--limit N optional)."""
    limit = None
    if len(sys.argv) > 1:
        try: limit = int(sys.argv[1])
        except ValueError: pass

    def cb(p):
        print(f"  → ok={p['ok']} fail={p['fail']} skipped={p['skipped']} / {p['total']} (last: {p['current']})", flush=True)

    result = await backfill(limit=limit, progress_cb=cb)
    print("\n=== log ===")
    for line in result["log"][-25:]:
        print(line)
    print(f"\n=== summary ===")
    print(f"  total={result['total']} ok={result['ok']} fail={result['fail']} skipped={result['skipped']}")


if __name__ == "__main__":
    asyncio.run(main())
