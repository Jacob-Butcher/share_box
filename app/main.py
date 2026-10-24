"""ShareBox - 实时共享文本/文件看板, 适合跑在 NAS Docker 中

所有设备打开同一个网页即可实时看到所有人分享的文本和文件。

同步设计 (避免全量轮询):
- 写操作 (分享/删除) 都会往 events 表追加一条事件 (add/del)
- 客户端首次加载只取最近 N 条 (/api/board?limit=50), 并记下事件序号
- 之后每 2 秒只拉取该序号之后的事件 (/api/events?since=seq),
  没有新内容时几乎零开销; 历史消息通过「加载更多」按 created 游标分页
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
               seq INTEGER PRIMARY KEY AUTOINCREMENT,
               op  TEXT NOT NULL,              -- 'add' | 'del'
               id  TEXT NOT NULL
           )"""
    )
    # 兼容旧库: 补 frm 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shares)")}
    if "frm" not in cols:
        conn.execute("ALTER TABLE shares ADD COLUMN frm TEXT NOT NULL DEFAULT ''")
    return conn


def new_id() -> str:
    return secrets.token_urlsafe(6)


def log_event(conn: sqlite3.Connection, op: str, sid: str):
    conn.execute("INSERT INTO events (op, id) VALUES (?,?)", (op, sid))


def max_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]


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


def build_item(conn: sqlite3.Connection, row, seq: int = 0) -> dict | None:
    """把一行 shares 记录组装成前端消息对象 (row 前 6 列为 shares 字段)"""
    sid, typ, name, frm, size, created = row[:6]
    if typ == "text":
        try:
            content = (UPLOAD_DIR / sid).read_text(encoding="utf-8")
        except Exception:
            return None
        return {
            "id": sid,
            "type": "text",
            "full": content,
            "frm": frm,
            "size": size,
            "created": created,
            "seq": seq,
        }
    name = name or "未命名文件"
    return {
        "id": sid,
        "type": "file",
        "name": name,
        "frm": frm,
        "size": size,
        "created": created,
        "kind": file_kind(name),
        "mime": file_mime(name),
        "seq": seq,
    }


COLS = "s.id, s.type, s.name, s.frm, s.size, s.created"


@app.get("/api/board")
def board(limit: int = 50, beforeSeq: int = 0):
    """初始/分页加载: 按事件序号倒序取最近 limit 条 (同一秒的消息也不会漏)"""
    limit = min(max(int(limit), 1), 200)
    with closing(db()) as conn:
        seq = max_seq(conn)
        sql = (
            f"SELECT {COLS}, e.seq FROM events e JOIN shares s ON s.id = e.id"
            " WHERE e.op = 'add'"
        )
        params: list = []
        if beforeSeq:
            sql += " AND e.seq < ?"
            params.append(int(beforeSeq))
        sql += " ORDER BY e.seq DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        items = [it for it in (build_item(conn, r, r[6]) for r in rows) if it]
    return {"items": items, "maxSeq": seq, "hasMore": len(items) >= limit}


@app.get("/api/events")
def events(since: int = 0, limit: int = 200):
    """增量同步: 只返回 seq 之后的事件; add 事件附带完整消息内容"""
    limit = min(max(int(limit), 1), 500)
    with closing(db()) as conn:
        seq = max_seq(conn)
        rows = conn.execute(
            "SELECT seq, op, id FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
            (since, limit),
        ).fetchall()
        out = []
        for eseq, op, sid in rows:
            if op == "add":
                row = conn.execute(
                    f"SELECT {COLS} FROM shares s WHERE s.id = ?", (sid,)
                ).fetchone()
                if not row:
                    continue  # 添加后又被删除, 客户端无需看到
                item = build_item(conn, row, eseq)
                if item:
                    out.append({"seq": eseq, "op": "add", "item": item})
            else:
                out.append({"seq": eseq, "op": "del", "id": sid})
    return {"events": out, "maxSeq": seq}


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
        log_event(conn, "add", sid)
        conn.commit()
    return {"ok": True}


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
        log_event(conn, "add", sid)
        conn.commit()
    return {"ok": True}


@app.post("/api/delete")
def delete_item(id: str = Form(...)):
    with closing(db()) as conn:
        row = conn.execute("SELECT id FROM shares WHERE id = ?", (id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="内容不存在或已删除")
        conn.execute("DELETE FROM shares WHERE id = ?", (id,))
        log_event(conn, "del", id)
        conn.commit()
    (UPLOAD_DIR / id).unlink(missing_ok=True)
    return {"ok": True}


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
            f"SELECT {COLS} FROM shares WHERE id = ?", (sid,)
        ).fetchone()
    if not row or row[0] != "file":
        raise HTTPException(status_code=404, detail="文件不存在或已删除")
    item = build_item(conn, row)
    return item


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
