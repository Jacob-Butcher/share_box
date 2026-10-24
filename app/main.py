"""ShareBox - 轻量文本/文件分享服务，适合跑在 NAS Docker 中"""
import os
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
UPLOAD_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "sharebox.db"
ACCESS_CODE = os.environ.get("ACCESS_CODE", "").strip()
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1024"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ShareBox")
static_dir = Path(__file__).parent / "static"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shares (
               id      TEXT PRIMARY KEY,
               type    TEXT NOT NULL,          -- 'text' | 'file'
               name    TEXT,                   -- 文件原始文件名
               size    INTEGER NOT NULL DEFAULT 0,
               expire  INTEGER NOT NULL DEFAULT 0,  -- 有效秒数, 0=永久
               created INTEGER NOT NULL
           )"""
    )
    return conn


def check_code(code: str):
    if ACCESS_CODE and code != ACCESS_CODE:
        raise HTTPException(status_code=403, detail="访问口令错误")


def cleanup():
    """删除已过期的分享及对应文件"""
    now = int(time.time())
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, type FROM shares WHERE expire > 0 AND created + expire < ?",
            (now,),
        ).fetchall()
        if not rows:
            return
        for sid, _ in rows:
            p = UPLOAD_DIR / sid
            if p.is_file():
                p.unlink(missing_ok=True)
        conn.execute(
            "DELETE FROM shares WHERE expire > 0 AND created + expire < ?", (now,)
        )
        conn.commit()


def cleanup_loop():
    while True:
        try:
            cleanup()
        except Exception:
            pass
        time.sleep(300)


threading.Thread(target=cleanup_loop, daemon=True).start()


def new_id() -> str:
    return secrets.token_urlsafe(6)


@app.get("/", response_class=HTMLResponse)
def index():
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/t/{sid}", response_class=HTMLResponse)
def text_page(sid: str):
    return (static_dir / "text.html").read_text(encoding="utf-8")


@app.get("/f/{sid}", response_class=HTMLResponse)
def file_page(sid: str):
    return (static_dir / "file.html").read_text(encoding="utf-8")


@app.post("/api/text")
def share_text(content: str = Form(...), expiry: int = Form(0), code: str = Form("")):
    check_code(code)
    content = content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="内容不能为空")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文本超过 2MB 上限")
    sid = new_id()
    (UPLOAD_DIR / sid).write_text(content, encoding="utf-8")
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO shares (id, type, size, expire, created) VALUES (?,?,?,?,?)",
            (sid, "text", len(content.encode("utf-8")), expiry, int(time.time())),
        )
        conn.commit()
    return {"id": sid, "url": f"/t/{sid}"}


@app.get("/api/text/{sid}")
def get_text(sid: str):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT type FROM shares WHERE id = ?", (sid,)
        ).fetchone()
    if not row or row[0] != "text":
        raise HTTPException(status_code=404, detail="分享不存在或已过期")
    p = UPLOAD_DIR / sid
    if not p.is_file():
        raise HTTPException(status_code=404, detail="分享不存在或已过期")
    return JSONResponse({"id": sid, "content": p.read_text(encoding="utf-8")})


@app.get("/api/raw/{sid}", response_class=PlainTextResponse)
def get_raw_text(sid: str):
    # 文本内容保存在 files/<id> 中, 与文件共用存储
    p = UPLOAD_DIR / sid
    if not p.is_file():
        raise HTTPException(status_code=404, detail="分享不存在或已过期")
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.post("/api/file")
async def share_file(
    file: UploadFile = File(...),
    expiry: int = Form(0),
    code: str = Form(""),
):
    check_code(code)
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    sid = new_id()
    dest = UPLOAD_DIR / sid
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {MAX_UPLOAD_MB}MB 上限",
                    )
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    filename = (file.filename or "未命名文件").strip() or "未命名文件"
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO shares (id, type, name, size, expire, created) VALUES (?,?,?,?,?,?)",
            (sid, "file", filename, size, expiry, int(time.time())),
        )
        conn.commit()
    return {"id": sid, "url": f"/f/{sid}"}


@app.get("/api/info/{sid}")
def info(sid: str):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id, type, name, size, expire, created FROM shares WHERE id = ?",
            (sid,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="分享不存在或已过期")
    sid_, typ, name, size, expire, created = row
    return {
        "id": sid_,
        "type": typ,
        "name": name,
        "size": size,
        "expire": expire,
        "created": created,
        "left": expire - (int(time.time()) - created) if expire else 0,
    }


@app.get("/api/download/{sid}")
def download(sid: str):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT type, name FROM shares WHERE id = ?", (sid,)
        ).fetchone()
    if not row or row[0] != "file":
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    p = UPLOAD_DIR / sid
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(p, filename=row[1])


app.mount("/static", StaticFiles(directory=static_dir), name="static")
