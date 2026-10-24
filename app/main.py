"""ShareBox - 实时共享文本/文件看板, 适合跑在 NAS Docker 中

所有设备打开同一个网页即可实时看到所有人分享的文本和文件。
"""
import os
import mimetypes
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
UPLOAD_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "sharebox.db"
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
               frm     TEXT NOT NULL DEFAULT '',  -- 分享者昵称
               size    INTEGER NOT NULL DEFAULT 0,
               expire  INTEGER NOT NULL DEFAULT 0,  -- 兼容旧库, 已不再使用
               created INTEGER NOT NULL
           )"""
    )
    # 兼容旧库: 补 frm 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shares)")}
    if "frm" not in cols:
        conn.execute("ALTER TABLE shares ADD COLUMN frm TEXT NOT NULL DEFAULT ''")
    return conn


def new_id() -> str:
    return secrets.token_urlsafe(6)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".json", ".xml", ".yaml", ".yml",
    ".csv", ".tsv", ".html", ".htm", ".css", ".js", ".ts", ".py", ".sh",
    ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sql", ".ini", ".toml", ".conf",
}


def file_kind(name: str) -> str:
    """image: 图片内联显示; pdf: 浏览器原生预览; text: 文本预览; other: 仅下载"""
    ext = Path(name or "").suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


def file_mime(name: str) -> str:
    return mimetypes.guess_type(name or "")[0] or "application/octet-stream"


@app.get("/", response_class=HTMLResponse)
def index():
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/p/{sid}", response_class=HTMLResponse)
def preview_page(sid: str):
    return (static_dir / "preview.html").read_text(encoding="utf-8")


def board_data() -> dict:
    """看板数据: 最近的文本和文件, 所有设备一致"""
    texts, files = [], []
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, type, name, frm, size, created FROM shares"
            " ORDER BY created DESC LIMIT 200"
        ).fetchall()
    for sid, typ, name, frm, size, created in rows:
        if typ == "text":
            try:
                content = (UPLOAD_DIR / sid).read_text(encoding="utf-8")
            except Exception:
                continue
            texts.append(
                {
                    "id": sid,
                    "type": "text",
                    "full": content,
                    "frm": frm,
                    "size": size,
                    "created": created,
                }
            )
        else:
            files.append(
                {
                    "id": sid,
                    "type": "file",
                    "name": name or "未命名文件",
                    "frm": frm,
                    "size": size,
                    "created": created,
                    "kind": file_kind(name),
                    "mime": file_mime(name),
                }
            )
    return {"texts": texts, "files": files}


@app.get("/api/board")
def board():
    return board_data()


@app.post("/api/text")
def share_text(content: str = Form(...), frm: str = Form("")):
    content = content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="内容不能为空")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文本超过 2MB 上限")
    sid = new_id()
    (UPLOAD_DIR / sid).write_text(content, encoding="utf-8")
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO shares (id, type, name, frm, size, expire, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, "text", content[:60], frm[:30],
             len(content.encode("utf-8")), 0, int(time.time())),
        )
        conn.commit()
    return board_data()


@app.post("/api/file")
async def share_file(file: UploadFile = File(...), frm: str = Form("")):
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
                        status_code=413, detail=f"文件超过 {MAX_UPLOAD_MB}MB 上限"
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    filename = (file.filename or "未命名文件").strip() or "未命名文件"
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO shares (id, type, name, frm, size, expire, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, "file", filename, frm[:30], size, 0, int(time.time())),
        )
        conn.commit()
    return board_data()


@app.post("/api/delete")
def delete_item(id: str = Form(...)):
    with closing(db()) as conn:
        row = conn.execute("SELECT id FROM shares WHERE id = ?", (id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="内容不存在或已删除")
        conn.execute("DELETE FROM shares WHERE id = ?", (id,))
        conn.commit()
    (UPLOAD_DIR / id).unlink(missing_ok=True)
    return board_data()


@app.get("/api/raw/{sid}", response_class=PlainTextResponse)
def get_raw_text(sid: str):
    with closing(db()) as conn:
        row = conn.execute("SELECT type FROM shares WHERE id = ?", (sid,)).fetchone()
    if not row or row[0] != "text":
        raise HTTPException(status_code=404, detail="文本不存在或已删除")
    p = UPLOAD_DIR / sid
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文本不存在或已删除")
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.get("/api/download/{sid}")
def download(sid: str):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT type, name FROM shares WHERE id = ?", (sid,)
        ).fetchone()
    if not row or row[0] != "file":
        raise HTTPException(status_code=404, detail="文件不存在或已删除")
    p = UPLOAD_DIR / sid
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已删除")
    return FileResponse(p, filename=row[1])


@app.get("/api/fileinfo/{sid}")
def file_info(sid: str):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT type, name, frm, size, created FROM shares WHERE id = ?", (sid,)
        ).fetchone()
    if not row or row[0] != "file":
        raise HTTPException(status_code=404, detail="文件不存在或已删除")
    name = row[1] or "未命名文件"
    return {
        "id": sid,
        "name": name,
        "frm": row[2],
        "size": row[3],
        "created": row[4],
        "kind": file_kind(name),
        "mime": file_mime(name),
    }


@app.get("/api/file/{sid}")
def file_inline(sid: str):
    """内联提供文件内容, 供图片直接显示 / PDF iframe / 文本预览使用"""
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT type, name FROM shares WHERE id = ?", (sid,)
        ).fetchone()
    if not row or row[0] != "file":
        raise HTTPException(status_code=404, detail="文件不存在或已删除")
    p = UPLOAD_DIR / sid
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已删除")
    return FileResponse(
        p,
        media_type=file_mime(row[1]),
        content_disposition_type="inline",
    )
