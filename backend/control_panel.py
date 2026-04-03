"""Control panel for managing scraper, viewer, and export."""
import asyncio
import os
import sys
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from backend import database

control_router = APIRouter(prefix="/panel")

# ── Scrape job state ──
_scrape_state = {
    "status": "idle",  # idle | running | completed | failed
    "started_at": None,
    "finished_at": None,
    "message": "",
    "process": None,
}

# ── Export state ──
_export_state = {
    "status": "idle",
    "file_path": None,
    "message": "",
}

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "scrape.log")


class ScrapeRequest(BaseModel):
    incremental: bool = True
    filter: str = ""


class ExportRequest(BaseModel):
    format: str = "jsonl"
    filter: str = ""


@control_router.get("", response_class=HTMLResponse)
@control_router.get("/", response_class=HTMLResponse)
async def panel_page():
    return PANEL_HTML


@control_router.get("/api/status")
async def panel_status():
    stats = database.get_stats()
    # Last message time
    from backend.database import get_db
    conn = get_db()
    row = conn.execute("SELECT MAX(last_message_time) FROM conversations").fetchone()
    last_time = row[0] if row and row[0] else 0
    # Conversations list for filter dropdown
    convs = conn.execute("SELECT name FROM conversations ORDER BY last_message_time DESC").fetchall()
    conn.close()
    return {
        "conversations": stats["conversations"],
        "messages": stats["messages"],
        "users": stats["users"],
        "last_message_time": last_time,
        "conversation_names": [c[0] for c in convs if c[0]],
        "scrape": {
            "status": _scrape_state["status"],
            "started_at": _scrape_state["started_at"],
            "finished_at": _scrape_state["finished_at"],
            "message": _scrape_state["message"],
        },
        "export": {
            "status": _export_state["status"],
            "file_path": _export_state["file_path"],
            "message": _export_state["message"],
        },
    }


@control_router.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    cmd = [sys.executable, "-u", "extract.py"]
    if req.incremental:
        cmd.append("--incremental")
    if req.filter:
        cmd.extend(["--filter", req.filter])

    _scrape_state["status"] = "running"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = f"{'增量' if req.incremental else '全量'}采集"
    if req.filter:
        _scrape_state["message"] += f" (过滤: {req.filter})"

    asyncio.create_task(_run_scrape(cmd))
    return {"status": "started", "message": _scrape_state["message"]}


async def _run_scrape(cmd):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            _scrape_state["process"] = proc
            await proc.wait()

        if proc.returncode == 0:
            _scrape_state["status"] = "completed"
            _scrape_state["message"] = "采集完成"
        else:
            _scrape_state["status"] = "failed"
            _scrape_state["message"] = f"采集失败 (exit code {proc.returncode})"
    except Exception as e:
        _scrape_state["status"] = "failed"
        _scrape_state["message"] = f"采集错误: {e}"
    finally:
        _scrape_state["finished_at"] = time.time()
        _scrape_state["process"] = None


@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 50):
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        with open(LOG_PATH, "r", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.post("/api/scrape/stop")
async def stop_scrape():
    proc = _scrape_state.get("process")
    if proc and proc.returncode is None:
        proc.terminate()
        _scrape_state["status"] = "idle"
        _scrape_state["message"] = "已停止"
        return {"status": "stopped"}
    return {"status": "not_running"}


@control_router.post("/api/export")
async def start_export(req: ExportRequest):
    if _export_state["status"] == "running":
        return JSONResponse({"error": "Export already running"}, status_code=409)

    _export_state["status"] = "running"
    _export_state["message"] = "正在导出..."

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_export, req.format, req.filter)
    return {
        "status": _export_state["status"],
        "message": _export_state["message"],
        "file_path": _export_state["file_path"],
    }


def _do_export(fmt: str, filter_name: str):
    try:
        from extractor.exporter import ChatLabExporter

        ext = ".json" if fmt == "json" else ".jsonl"
        output_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", f"export{ext}"
        )
        exporter = ChatLabExporter(conv_name=filter_name or None, output_format=fmt)
        exporter.export(output_path)
        _export_state["status"] = "completed"
        _export_state["file_path"] = f"export{ext}"
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        _export_state["message"] = f"导出完成 ({size_mb:.1f} MB)"
    except Exception as e:
        _export_state["status"] = "failed"
        _export_state["message"] = f"导出失败: {e}"


@control_router.get("/api/export/download")
async def download_export():
    if not _export_state["file_path"]:
        return JSONResponse({"error": "No export file"}, status_code=404)
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", _export_state["file_path"]
    )
    if not os.path.exists(path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=_export_state["file_path"])


# ── Inline HTML ──

PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Control Panel - 抖音聊天记录</title>
<style>
:root {
  --bg: #0f0f1a;
  --bg2: #1a1a2e;
  --bg3: #252540;
  --accent: #6c63ff;
  --accent-hover: #7b73ff;
  --text: #e0e0e0;
  --text2: #999;
  --border: #333;
  --green: #4caf50;
  --red: #f44336;
  --yellow: #ff9800;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
.container { max-width: 800px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; margin-bottom: 24px; color: var(--accent); }
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }
.card { background: var(--bg2); border-radius: 10px; padding: 16px; text-align: center; border: 1px solid var(--border); }
.card .num { font-size: 28px; font-weight: 700; color: var(--accent); }
.card .label { font-size: 12px; color: var(--text2); margin-top: 4px; }
.section { background: var(--bg2); border-radius: 10px; padding: 20px; margin-bottom: 16px; border: 1px solid var(--border); }
.section h2 { font-size: 15px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
select, input[type=text] { background: var(--bg3); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; font-size: 13px; outline: none; }
select:focus, input:focus { border-color: var(--accent); }
.btn { padding: 8px 20px; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; font-weight: 500; transition: all 0.15s; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-danger { background: var(--red); color: #fff; }
.btn-success { background: var(--green); color: #fff; }
.btn-link { background: none; color: var(--accent); border: 1px solid var(--accent); }
.btn-link:hover { background: var(--accent); color: #fff; }
.status { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 500; }
.status-idle { background: var(--bg3); color: var(--text2); }
.status-running { background: rgba(255,152,0,0.15); color: var(--yellow); }
.status-completed { background: rgba(76,175,80,0.15); color: var(--green); }
.status-failed { background: rgba(244,67,54,0.15); color: var(--red); }
.log-box { background: #0a0a14; border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-top: 12px; max-height: 200px; overflow-y: auto; font-family: 'SF Mono', 'Consolas', monospace; font-size: 12px; line-height: 1.5; color: var(--text2); white-space: pre-wrap; word-break: break-all; display: none; }
.log-box.show { display: block; }
.time { font-size: 12px; color: var(--text2); }
.viewer-link { display: inline-flex; align-items: center; gap: 6px; margin-bottom: 20px; }
.toggle { display: flex; gap: 0; border-radius: 6px; overflow: hidden; border: 1px solid var(--border); }
.toggle label { padding: 6px 14px; font-size: 12px; cursor: pointer; background: var(--bg3); transition: all 0.15s; }
.toggle input { display: none; }
.toggle input:checked + label { background: var(--accent); color: #fff; }
</style>
</head>
<body>
<div class="container">
  <h1>Douyin Chat Export</h1>
  <a href="/" class="viewer-link btn btn-link">Chat Viewer &rarr;</a>

  <div class="cards">
    <div class="card"><div class="num" id="convCount">-</div><div class="label">Conversations</div></div>
    <div class="card"><div class="num" id="msgCount">-</div><div class="label">Messages</div></div>
    <div class="card"><div class="num" id="userCount">-</div><div class="label">Users</div></div>
  </div>

  <!-- Scraper -->
  <div class="section">
    <h2>Scraper <span class="status status-idle" id="scrapeStatus">idle</span></h2>
    <div class="row">
      <div class="toggle">
        <input type="radio" name="mode" id="modeIncr" value="incremental" checked>
        <label for="modeIncr">Incremental</label>
        <input type="radio" name="mode" id="modeFull" value="full">
        <label for="modeFull">Full</label>
      </div>
      <select id="scrapeFilter"><option value="">All conversations</option></select>
      <button class="btn btn-primary" id="scrapeBtn" onclick="startScrape()">Start</button>
      <button class="btn btn-danger" id="stopBtn" onclick="stopScrape()" style="display:none">Stop</button>
    </div>
    <div class="time" id="scrapeTime"></div>
    <div class="log-box" id="scrapeLog"></div>
  </div>

  <!-- Export -->
  <div class="section">
    <h2>Export <span class="status status-idle" id="exportStatus">idle</span></h2>
    <div class="row">
      <select id="exportFormat">
        <option value="jsonl">JSONL</option>
        <option value="json">JSON</option>
      </select>
      <select id="exportFilter"><option value="">All conversations</option></select>
      <button class="btn btn-primary" id="exportBtn" onclick="startExport()">Export</button>
      <a class="btn btn-success" id="downloadBtn" style="display:none;text-decoration:none" href="/panel/api/export/download">Download</a>
    </div>
    <div class="time" id="exportMsg"></div>
  </div>
</div>

<script>
let refreshTimer = null;

async function loadStatus() {
  try {
    const r = await fetch('/panel/api/status');
    const d = await r.json();
    document.getElementById('convCount').textContent = d.conversations;
    document.getElementById('msgCount').textContent = d.messages.toLocaleString();
    document.getElementById('userCount').textContent = d.users;

    // Scrape status
    const ss = d.scrape;
    const se = document.getElementById('scrapeStatus');
    se.textContent = ss.status;
    se.className = 'status status-' + ss.status;
    document.getElementById('scrapeBtn').disabled = ss.status === 'running';
    document.getElementById('stopBtn').style.display = ss.status === 'running' ? '' : 'none';
    let timeStr = '';
    if (ss.started_at) timeStr += 'Started: ' + new Date(ss.started_at * 1000).toLocaleTimeString();
    if (ss.finished_at) timeStr += '  Finished: ' + new Date(ss.finished_at * 1000).toLocaleTimeString();
    if (ss.message) timeStr += '  ' + ss.message;
    document.getElementById('scrapeTime').textContent = timeStr;

    // Show log if running or just finished
    if (ss.status === 'running' || ss.status === 'completed' || ss.status === 'failed') {
      loadLog();
    }

    // Export status
    const es = d.export;
    const ee = document.getElementById('exportStatus');
    ee.textContent = es.status;
    ee.className = 'status status-' + es.status;
    document.getElementById('exportBtn').disabled = es.status === 'running';
    document.getElementById('exportMsg').textContent = es.message || '';
    document.getElementById('downloadBtn').style.display = (es.status === 'completed' && es.file_path) ? '' : 'none';

    // Populate filter dropdowns
    const filters = [document.getElementById('scrapeFilter'), document.getElementById('exportFilter')];
    for (const sel of filters) {
      const cur = sel.value;
      sel.innerHTML = '<option value="">All conversations</option>';
      for (const name of d.conversation_names) {
        const opt = document.createElement('option');
        opt.value = name; opt.textContent = name;
        sel.appendChild(opt);
      }
      sel.value = cur;
    }
  } catch (e) { console.error('Status fetch failed:', e); }
}

async function loadLog() {
  try {
    const r = await fetch('/panel/api/scrape/log?lines=80');
    const d = await r.json();
    const box = document.getElementById('scrapeLog');
    box.textContent = d.log || '(no output)';
    box.classList.add('show');
    box.scrollTop = box.scrollHeight;
  } catch {}
}

async function startScrape() {
  const incremental = document.getElementById('modeIncr').checked;
  const filter = document.getElementById('scrapeFilter').value;
  document.getElementById('scrapeBtn').disabled = true;
  await fetch('/panel/api/scrape', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ incremental, filter }),
  });
  loadStatus();
}

async function stopScrape() {
  await fetch('/panel/api/scrape/stop', { method: 'POST' });
  loadStatus();
}

async function startExport() {
  const format = document.getElementById('exportFormat').value;
  const filter = document.getElementById('exportFilter').value;
  document.getElementById('exportBtn').disabled = true;
  document.getElementById('exportStatus').textContent = 'running';
  document.getElementById('exportStatus').className = 'status status-running';
  await fetch('/panel/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ format, filter }),
  });
  loadStatus();
}

loadStatus();
refreshTimer = setInterval(loadStatus, 5000);
</script>
</body>
</html>
"""
