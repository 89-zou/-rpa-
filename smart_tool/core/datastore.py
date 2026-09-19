# -*- coding: utf-8 -*-
"""采集数据的落盘：项目的 `data/` 目录。

- `records.jsonl`：一行一条 JSON，**追加写**（写到一半断了也不丢已采的、不怕文件大）
- `files/`：图片、附件、截图
- 每条记录统一带 `_time` / `_url` / `_step` 三个下划线开头的元信息，
  这样不会跟用户自己起的字段名（标题、正文…）撞车

导出 CSV 用 utf-8-sig：Excel 双击打开不会乱码。
"""
import csv
import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATA_DIR_NAME = "data"
FILES_DIR_NAME = "files"
RECORDS_NAME = "records.jsonl"
#: 每条记录自动附带的元信息（下划线开头，避开用户字段名）
META_KEYS = ("_time", "_url", "_step")

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\n\r\t]+')
EXT_BY_TYPE = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp",
    "application/pdf": ".pdf", "application/zip": ".zip",
    "text/plain": ".txt", "text/csv": ".csv", "text/html": ".html",
    "application/json": ".json", "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

_lock = threading.Lock()


def data_dir(project_dir) -> Path:
    return Path(project_dir) / DATA_DIR_NAME


def files_dir(project_dir) -> Path:
    return data_dir(project_dir) / FILES_DIR_NAME


def records_path(project_dir) -> Path:
    return data_dir(project_dir) / RECORDS_NAME


def _stamp(path: Path) -> Tuple[int, float]:
    try:
        st = path.stat()
        return int(st.st_size), float(st.st_mtime)
    except OSError:
        return 0, 0.0


def records_stamp(project_dir) -> Tuple[int, float]:
    """(记录文件大小, 修改时间)：给数据面板判断「要不要重新读」用。"""
    return _stamp(records_path(project_dir))


def safe_stem(text: str, limit: int = 40) -> str:
    """把字段名 / 文件名清理成能当文件名的样子。"""
    out = _BAD_CHARS.sub("_", str(text or "")).strip().strip("._")
    return out[:limit] or "file"


def guess_ext(url: str, content_type: str = "") -> str:
    """从 content-type 或 URL 猜一个扩展名。"""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    suffix = Path((url or "").split("?")[0].split("#")[0]).suffix.lower()
    if suffix and len(suffix) <= 6 and re.fullmatch(r"\.[a-z0-9]+", suffix):
        return suffix
    return ".bin"


def save_bytes(project_dir, stem: str, ext: str, data: bytes) -> str:
    """把一段二进制存进 `data/files/`，返回相对项目的路径（files/xxx.png）。"""
    folder = files_dir(project_dir)
    folder.mkdir(parents=True, exist_ok=True)
    ext = ext if ext.startswith(".") else f".{ext}"
    stem = safe_stem(stem)
    if stem.lower().endswith(ext.lower()):      # 字段名本来就带后缀，别写成 .png.png
        stem = stem[: -len(ext)].strip("._") or "file"
    path = folder / f"{stem}{ext}"
    i = 1
    while path.exists():
        path = folder / f"{stem}_{i}{ext}"
        i += 1
    path.write_bytes(data)
    return f"{FILES_DIR_NAME}/{path.name}"


def append_record(project_dir, record: Dict):
    """追加一条记录（一行 JSON）。"""
    path = records_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_records(project_dir, limit: Optional[int] = 2000) -> List[Dict]:
    """读记录；limit 只取最后 N 条（默认 2000，面板够用）。"""
    path = records_path(project_dir)
    if not path.exists():
        return []
    out: List[Dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue        # 半行（正好写到一半）直接跳过
                if isinstance(item, dict):
                    out.append(item)
    except OSError:
        return []
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


def count_records(project_dir) -> int:
    path = records_path(project_dir)
    if not path.exists():
        return 0
    n = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        return 0
    return n


def list_files(project_dir) -> List[Path]:
    folder = files_dir(project_dir)
    if not folder.exists():
        return []
    return sorted((p for p in folder.iterdir() if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def clear(project_dir) -> None:
    """清空记录（文件留下，不删 files/ 里的东西）。"""
    path = records_path(project_dir)
    with _lock:
        try:
            path.unlink()
        except OSError:
            pass


def columns(records: List[Dict]) -> List[str]:
    """这些记录里出现过的列（元信息在前，其余按出现顺序）。"""
    metas = [k for k in META_KEYS if any(k in r for r in records)]
    others: List[str] = []
    for r in records:
        for k in r:
            if k not in META_KEYS and k not in others:
                others.append(k)
    return metas + others


def export_csv(project_dir, target) -> int:
    """导出成 CSV（utf-8-sig，Excel 不乱码），返回写了多少行。"""
    records = read_records(project_dir, limit=None)
    target = Path(target)
    cols = columns(records)
    with target.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        for r in records:
            writer.writerow([_cell(r.get(c)) for c in cols])
    return len(records)


def _cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else value


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
