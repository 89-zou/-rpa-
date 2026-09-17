# -*- coding: utf-8 -*-
"""本地文件数据源读取（纯函数，不依赖 PyQt）。

支持四种类型，统一返回「行变量字典」列表，执行器把每行并入 {{变量}}：

- excel  (.xlsx/.xls)：第一行作表头，每行产出 {"row.列名": 值, ...}
- json   ：JSON 数组，每个对象产出 {"row.键": 值, ...}；单个对象产出 1 行
- txt    ：单个文本文件，产出 1 行，含 file.name/file.stem/file.path/
           file.parent_name/file.content
- folder ：按通配符（默认递归 *.txt）遍历文件，每个文件产出同样一组 file.* 变量
           —— 对应「标题=父文件夹名、正文=正文.txt」的目录式数据结构。

field_map 是「这个数据源要保存哪些变量」的清单（可改名），四种类型通用：
[{"field": "content"（原始列名/文件字段）, "var": "row.正文"（变量名）}, ...]
清单为空时表示未挑选过，产出全部原始变量（兼容旧项目）。

另有 loop.index（当前行号，从 1 开始）由执行器注入。
"""
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

TXT_EXTS = {".txt", ".md", ".csv", ".log"}
ENCODING_TRY = ("utf-8-sig", "utf-8", "gbk", "gb18030")

# 文件类数据源可读取的字段（field key → 中文名），用于挑选变量并自定义命名
FILE_FIELDS = [
    ("content", "文件内容"),
    ("name", "文件名（含扩展名）"),
    ("stem", "文件名（不含扩展名）"),
    ("parent_name", "父文件夹名"),
    ("path", "完整路径"),
    ("suffix", "扩展名"),
    ("size", "文件大小（字节）"),
]


@dataclass
class DataSourceConfig:
    """数据源配置（steps.json 中的 data_source 节）。"""
    type: str = ""                 # excel/json/txt/folder，空表示未配置
    path: str = ""
    sheet: str = ""                # excel：指定 sheet 名，空取第一个
    has_header: bool = True        # excel：首行是否为表头
    encoding: str = "auto"         # txt/folder：auto/utf-8/gbk
    pattern: str = "*.txt"         # folder：文件通配
    recursive: bool = True         # folder：是否递归子目录
    # 「要保存哪些变量」清单（四种类型通用，可改名）：
    # [{"field": "content", "var": "row.正文"}, ...]
    field_map: List[Dict[str, str]] = field(default_factory=list)
    # 是否已经在【数据源…】里挑过变量。
    # 没挑过 → 产出全部原始变量（兼容旧项目）；
    # 挑过（哪怕是空清单）→ 只产出清单里的变量。
    vars_picked: bool = False

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "DataSourceConfig":
        if not d:
            return cls()
        return cls(
            type=d.get("type", ""),
            path=d.get("path", ""),
            sheet=d.get("sheet", ""),
            has_header=bool(d.get("has_header", True)),
            encoding=d.get("encoding", "auto"),
            pattern=d.get("pattern", "*.txt") or "*.txt",
            recursive=bool(d.get("recursive", True)),
            field_map=[m for m in d.get("field_map", []) if isinstance(m, dict)],
            vars_picked="field_map" in d,
        )

    def to_dict(self) -> dict:
        d = {
            "type": self.type,
            "path": self.path,
            "sheet": self.sheet,
            "has_header": self.has_header,
            "encoding": self.encoding,
            "pattern": self.pattern,
            "recursive": self.recursive,
        }
        if self.vars_picked or self.field_map:
            d["field_map"] = self.field_map
        return d

    @property
    def configured(self) -> bool:
        return bool(self.type and self.path)


class DataSourceError(Exception):
    """数据源读取失败。"""


# ------------------------------
# 文本读取（编码兜底）
# ------------------------------
def read_text_file(path: Path, encoding: str = "auto") -> str:
    """按指定编码读文本；auto 时依次尝试常见中文编码。"""
    encs = (encoding,) if encoding and encoding != "auto" else ENCODING_TRY
    last_err = None
    for enc in encs:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError) as e:
            last_err = e
    # 最后用替换模式兜底，保证不崩
    return path.read_text(encoding="utf-8", errors="replace") if last_err else ""


def _cell_to_str(v: Any) -> str:
    """单元格值转字符串。None→空串；浮点整数去掉 .0。"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# ------------------------------
# Excel
# ------------------------------
def _read_xlsx(path: Path, cfg: DataSourceConfig,
               max_rows: Optional[int] = None) -> List[List[str]]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[cfg.sheet] if cfg.sheet and cfg.sheet in wb.sheetnames else wb.worksheets[0]
        rows: List[List[str]] = []
        for raw in ws.iter_rows(values_only=True):
            rows.append([_cell_to_str(c) for c in raw])
            if max_rows is not None and len(rows) >= max_rows:
                break
        return rows
    finally:
        wb.close()


def _read_xls(path: Path, cfg: DataSourceConfig,
              max_rows: Optional[int] = None) -> List[List[str]]:
    import xlrd
    book = xlrd.open_workbook(str(path))
    if cfg.sheet:
        sh = book.sheet_by_name(cfg.sheet)
    else:
        sh = book.sheet_by_index(0)
    rows = []
    limit = sh.nrows if max_rows is None else min(sh.nrows, max_rows)
    for r in range(limit):
        rows.append([_cell_to_str(sh.cell_value(r, c)) for c in range(sh.ncols)])
    return rows


def load_excel(cfg: DataSourceConfig) -> List[Dict[str, str]]:
    path = Path(cfg.path)
    if not path.is_file():
        raise DataSourceError(f"Excel 文件不存在：{path}")
    ext = path.suffix.lower()
    if ext == ".xlsx":
        rows = _read_xlsx(path, cfg)
    elif ext == ".xls":
        rows = _read_xls(path, cfg)
    else:
        raise DataSourceError(f"不支持的 Excel 格式：{ext}")

    rows = [r for r in rows if any((c or "") != "" for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)

    if cfg.has_header:
        head = rows[0] + [""] * (width - len(rows[0]))
        names, used = [], {}
        for i, h in enumerate(head):
            name = h.strip() or f"col_{i+1}"
            # 重名列自动去重
            if name in used:
                used[name] += 1
                name = f"{name}_{used[name]}"
            else:
                used[name] = 1
            names.append(name)
        body = rows[1:]
    else:
        names = [f"col_{i+1}" for i in range(width)]
        body = rows

    result = []
    for r in body:
        r = r + [""] * (width - len(r))
        if not any(c != "" for c in r):
            continue
        result.append({f"row.{names[i]}": r[i] for i in range(width)})
    return [_apply_field_map(r, cfg) for r in result]


# ------------------------------
# JSON
# ------------------------------
def load_json(cfg: DataSourceConfig) -> List[Dict[str, str]]:
    path = Path(cfg.path)
    if not path.is_file():
        raise DataSourceError(f"JSON 文件不存在：{path}")
    try:
        data = json.loads(read_text_file(path, cfg.encoding))
    except json.JSONDecodeError as e:
        raise DataSourceError(f"JSON 解析失败：{e}")

    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    else:
        raise DataSourceError("JSON 顶层必须是对象或对象数组")

    result = []
    for obj in items:
        base = {f"row.{k}": _cell_to_str(v) for k, v in obj.items()}
        result.append(_apply_field_map(base, cfg))
    return result


# ------------------------------
# TXT 单文件 / 文件夹
# ------------------------------
def _file_vars(path: Path, encoding: str) -> Dict[str, str]:
    """文件内置变量：内容/名称/路径/父文件夹/扩展名/大小。"""
    try:
        size = str(path.stat().st_size)
    except OSError:
        size = ""
    return {
        "file.name": path.name,
        "file.stem": path.stem,
        "file.path": str(path),
        "file.parent_name": path.parent.name,
        "file.content": read_text_file(path, encoding),
        "file.suffix": path.suffix.lstrip("."),
        "file.size": size,
    }


def resolve_src_key(cfg_type: str, fld: str) -> str:
    """把 field_map 里的 field 还原成数据行里的原始键。

    既接受短名（content / 标题），也接受完整名（file.content / row.标题）：
    - txt、folder：文件字段 → file.<field>
    - excel、json：列名/键名 → row.<field>
    """
    f = (fld or "").strip()
    if not f:
        return ""
    if f.startswith(("row.", "file.")):
        return f
    return ("file." if cfg_type in ("txt", "folder") else "row.") + f


def _apply_field_map(base: Dict[str, str], cfg: DataSourceConfig) -> Dict[str, str]:
    """按 field_map 挑选变量并改名。

    没挑过（vars_picked=False）→ 原样返回，产出全部原始变量（兼容旧项目）。
    挑过 → 只产出清单里的变量，名字用 var，顺序与清单一致。
    """
    if not cfg.vars_picked:
        return dict(base)
    out: Dict[str, str] = {}
    for m in cfg.field_map or []:
        var = (m.get("var") or "").strip()
        key = resolve_src_key(cfg.type, m.get("field", ""))
        if var and key in base:
            out[var] = base[key]
    return out


def load_txt(cfg: DataSourceConfig) -> List[Dict[str, str]]:
    path = Path(cfg.path)
    if not path.is_file():
        raise DataSourceError(f"文本文件不存在：{path}")
    return [_apply_field_map(_file_vars(path, cfg.encoding), cfg)]


def load_folder(cfg: DataSourceConfig) -> List[Dict[str, str]]:
    folder = Path(cfg.path)
    if not folder.is_dir():
        raise DataSourceError(f"文件夹不存在：{folder}")
    walker = folder.rglob if cfg.recursive else folder.glob
    files = sorted(p for p in walker(cfg.pattern) if p.is_file())
    return [_apply_field_map(_file_vars(p, cfg.encoding), cfg) for p in files]


# ------------------------------
# 统一入口
# ------------------------------
_LOADERS: Dict[str, Callable[[DataSourceConfig], List[Dict[str, str]]]] = {
    "excel": load_excel,
    "json": load_json,
    "txt": load_txt,
    "folder": load_folder,
}


def load_rows(cfg: DataSourceConfig) -> List[Dict[str, str]]:
    """按配置读取全部数据行。未配置数据源返回空列表。"""
    if not cfg.configured:
        return []
    loader = _LOADERS.get(cfg.type)
    if loader is None:
        raise DataSourceError(f"未知数据源类型：{cfg.type}")
    return loader(cfg)


def preview(cfg: DataSourceConfig, n: int = 5) -> Tuple[List[Dict[str, str]], int, List[str]]:
    """预览前 n 行的「原始变量」，返回 (前n行, 总行数, 原始变量名列表)。

    故意忽略 field_map：这是「勾选要保存哪些变量」的候选清单，
    用户在这里改名/勾选，保存后才由 load_rows + list_columns 生效。
    """
    raw = replace(cfg, field_map=[], vars_picked=False)
    rows = load_rows(raw)
    columns: List[str] = []
    for r in rows:
        for k in r:
            if k not in columns:
                columns.append(k)
    return rows[:n], len(rows), columns


def raw_columns(cfg: DataSourceConfig) -> List[str]:
    """该数据源的全部原始变量名（不受 field_map 影响）。

    只读表头/首个对象/文件字段表，不读全量内容；失败返回空列表。
    """
    if not cfg.configured:
        return []
    try:
        if cfg.type == "excel":
            path = Path(cfg.path)
            if not path.is_file():
                return []
            if path.suffix.lower() == ".xlsx":
                rows = _read_xlsx(path, cfg, max_rows=1)
            else:
                rows = _read_xls(path, cfg, max_rows=1)
            rows = [r for r in rows if any((c or "") != "" for c in r)]
            if not rows:
                return []
            first = rows[0]
            if cfg.has_header:
                return [f"row.{(h or '').strip() or f'col_{i+1}'}"
                        for i, h in enumerate(first)]
            return [f"row.col_{i+1}" for i in range(len(first))]
        if cfg.type == "json":
            path = Path(cfg.path)
            if not path.is_file():
                return []
            data = json.loads(read_text_file(path, cfg.encoding))
            obj = data[0] if isinstance(data, list) and data else data
            if not isinstance(obj, dict):
                return []
            return [f"row.{k}" for k in obj.keys()]
        if cfg.type in ("txt", "folder"):
            return [f"file.{key}" for key, _ in FILE_FIELDS]
    except Exception:
        return []
    return []


def list_columns(cfg: DataSourceConfig) -> List[str]:
    """列出该数据源实际会产出的变量名（供变量下拉/变量管理）。

    挑过变量 → 只返回挑选的（改名后的），可能为空（都被删掉了）；
    没挑过 → 返回全部原始变量（兼容旧项目）。
    """
    if not cfg.configured:
        return []
    if cfg.vars_picked:
        out: List[str] = []
        for m in cfg.field_map:
            var = (m.get("var") or "").strip()
            if var and var not in out:
                out.append(var)
        return out
    return raw_columns(cfg)


def without_variable(cfg: DataSourceConfig,
                     var: str) -> Optional[DataSourceConfig]:
    """返回「去掉某个变量」后的新配置；失败返回 None。

    没挑过变量时，先按当前数据源物化出全部变量清单，再删掉目标——
    这样在【变量管理】里删掉一个数据源变量，就真的不再产出它。
    """
    items = [dict(m) for m in cfg.field_map]
    if not cfg.vars_picked:
        cols = raw_columns(cfg)
        if not cols:
            return None
        items = [{"field": c, "var": c} for c in cols]
    kept = [m for m in items if (m.get("var") or "").strip() != var]
    if len(kept) == len(items):
        return None
    return replace(cfg, field_map=kept, vars_picked=True)
