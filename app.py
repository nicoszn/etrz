import os
import uuid
import threading
import time
import json
import zipfile
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

app = FastAPI(title="yt-dlp server", version="5.1.0")

DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "./downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = Path("./cookies.txt")
CLEANUP_AFTER_MINUTES = int(os.environ.get("CLEANUP_AFTER_MINUTES", 15))

progress_store = {}

# ─────────────────────────────
# Models
# ─────────────────────────────

class QuickDownloadRequest(BaseModel):
    url: str
    mode: Optional[str] = "video"
    format_id: Optional[str] = None


# ─────────────────────────────
# yt-dlp base (anti-bot)
# ─────────────────────────────

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 5,
    "fragment_retries": 5,
    "concurrent_fragment_downloads": 3,

    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web", "mweb"]
        }
    },

    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:python"],

    "http_headers": {
        "User-Agent": "Mozilla/5.0"
    },
}


# ─────────────────────────────
# Helpers
# ─────────────────────────────

def human_bytes(b):
    if not b:
        return "unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def classify_format(f):
    v = f.get("vcodec")
    a = f.get("acodec")

    if v != "none" and a != "none":
        t = "combined"
    elif v != "none":
        t = "video_only"
    else:
        t = "audio_only"

    size = f.get("filesize") or f.get("filesize_approx")

    return {
        "format_id": f.get("format_id"),
        "ext": f.get("ext"),
        "type": t,
        "resolution": f.get("resolution"),
        "filesize": size,
        "filesize_human": human_bytes(size),
    }


def safe_yt(opts):
    try:
        return yt_dlp.YoutubeDL(opts)
    except Exception:
        if COOKIES_FILE.exists():
            opts["cookiefile"] = str(COOKIES_FILE)
            return yt_dlp.YoutubeDL(opts)
        raise


def fetch_info(url):
    opts = {**BASE_OPTS, "skip_download": True}

    with safe_yt(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = [classify_format(f) for f in info.get("formats", [])]

    return {
        "title": info.get("title"),
        "formats": formats
    }


# ─────────────────────────────
# Download Worker
# ─────────────────────────────

def download_worker(url, download_id, mode, format_id):
    uid = download_id[:8]
    output = str(DOWNLOADS_DIR / f"%(title)s-{uid}.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            percent = (done / total * 100) if total else 0

            progress_store[download_id].update({
                "status": "downloading",
                "progress": round(percent, 2),
                "text": f"{round(percent,2)}%"
            })

        elif d["status"] == "finished":
            progress_store[download_id].update({
                "status": "processing",
                "progress": 95,
                "text": "processing..."
            })

    try:
        fmt = format_id or "b"

        opts = {
            **BASE_OPTS,
            "format": fmt,
            "outtmpl": output,
            "progress_hooks": [hook],
        }

        if mode == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
            }]

        if mode == "subs":
            opts.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "skip_download": True,
            })

        with safe_yt(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

        file = Path(filepath)

        if mode == "bundle":
            zip_path = DOWNLOADS_DIR / f"{file.stem}.zip"
            with zipfile.ZipFile(zip_path, "w") as z:
                if file.exists():
                    z.write(file, file.name)

                meta = json.dumps(info, indent=2)
                meta_path = DOWNLOADS_DIR / f"{file.stem}.json"
                meta_path.write_text(meta)
                z.write(meta_path, meta_path.name)

            file = zip_path

        progress_store[download_id].update({
            "status": "completed",
            "progress": 100,
            "text": "done",
            "filename": file.name,
            "filepath": str(file),
        })

        threading.Thread(target=cleanup_file, args=(file,), daemon=True).start()

    except Exception as e:
        progress_store[download_id].update({
            "status": "error",
            "error": str(e),
        })


def cleanup_file(file: Path):
    time.sleep(CLEANUP_AFTER_MINUTES * 60)
    try:
        if file.exists():
            file.unlink()
    except:
        pass


# ─────────────────────────────
# UI (RESTORED + UPGRADED)
# ─────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!DOCTYPE html>
<html>
<head>
<title>Downloader</title>
<style>
body{font-family:sans-serif;background:#0f172a;color:#fff;text-align:center;padding-top:60px}
input,select{width:420px;padding:10px;border-radius:6px;border:none;margin-top:10px}
button{padding:10px 18px;background:#3b82f6;color:#fff;border:none;border-radius:6px;margin-top:10px}
.bar{width:420px;height:8px;background:#333;margin:20px auto;border-radius:4px}
.fill{height:100%;width:0;background:#22c55e}
</style>
</head>
<body>

<h2>yt-dlp advanced downloader</h2>

<input id="url" placeholder="paste url"/><br>

<select id="mode">
<option value="video">video</option>
<option value="audio">audio</option>
<option value="subs">subtitles</option>
<option value="bundle">bundle</option>
</select><br>

<select id="format"></select><br>

<button onclick="preview()">preview</button>
<button onclick="start()">download</button>

<div class="bar"><div id="fill" class="fill"></div></div>
<p id="text"></p>

<script>
let id=null;

async function preview(){
    const url=document.getElementById("url").value;
    const res=await fetch("/preview?url="+encodeURIComponent(url));
    const data=await res.json();

    const select=document.getElementById("format");
    select.innerHTML="";

    data.formats.forEach(f=>{
        const opt=document.createElement("option");
        opt.value=f.format_id;
        opt.text=f.format_id+" | "+f.resolution+" | "+f.filesize_human;
        select.appendChild(opt);
    });
}

async function start(){
    const url=document.getElementById("url").value;
    const mode=document.getElementById("mode").value;
    const format_id=document.getElementById("format").value;

    const res=await fetch("/quick",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({url,mode,format_id})
    });

    const data=await res.json();
    id=data.download_id;

    poll();
}

async function poll(){
    const res=await fetch("/progress/"+id);
    const data=await res.json();

    document.getElementById("fill").style.width=data.progress+"%";
    document.getElementById("text").innerText=data.text||data.status;

    if(data.status==="completed"){
        window.location=data.download_url;
        return;
    }

    if(data.status==="error"){
        document.getElementById("text").innerText=data.error;
        return;
    }

    setTimeout(poll,1000);
}
</script>

</body>
</html>
"""


# ─────────────────────────────
# API
# ─────────────────────────────

@app.get("/preview")
def preview(url: str):
    return fetch_info(url)


@app.post("/quick")
def quick(req: QuickDownloadRequest):
    download_id = str(uuid.uuid4())

    progress_store[download_id] = {
        "status": "starting",
        "progress": 0,
        "text": "starting...",
    }

    threading.Thread(
        target=download_worker,
        args=(req.url, download_id, req.mode, req.format_id),
        daemon=True
    ).start()

    return {"download_id": download_id}


@app.get("/progress/{download_id}")
def progress(download_id: str):
    data = progress_store.get(download_id)

    if not data:
        return {"status": "not_found"}

    if data.get("status") == "completed":
        data["download_url"] = f"/download/{download_id}"

    return data


@app.get("/download/{download_id}")
def serve(download_id: str):
    data = progress_store.get(download_id)

    if not data or data.get("status") != "completed":
        raise HTTPException(404, "file not ready")

    path = Path(data["filepath"])

    if not path.exists():
        raise HTTPException(404, "file missing")

    return FileResponse(path=str(path), filename=data["filename"])
