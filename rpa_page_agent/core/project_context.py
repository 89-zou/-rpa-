# -*- coding: utf-8 -*-
"""项目资料：把「这个项目里现成有什么」自动读出来，交给页面里的 AI。

任务节点是**一句人话**，但一句话要落地，往往得知道项目里有什么现成的东西：
变量清单里有哪些名字、img/ 里有哪些图、有没有登录态、data/ 里采到了什么、
函数库里有哪些函数。这些不该让用户每次手打一遍 —— 这里自动读出来，拼成一段
「项目资料」附在任务描述后面，AI 一眼就知道有什么可用、能直接引用。

规矩：
· **只读**，不改任何文件；
· 全部有上限（变量 40 个、每个值 80 字、图片 30 张……）—— 这段文字每次调模型
  都会带上，撑爆了白花 token；
· 运行时给的是**真实值**（循环当前项 `loop.item` 也在里面），编辑时给的是清单里的静态值。
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from rpa_page_agent.core import auth_store, datastore
from rpa_page_agent.core.project_store import ProjectStore

#: 各类资料的上限（都是「显示多少个/多少字」）
MAX_VARS = 40
MAX_VAR_CHARS = 80
MAX_IMAGES = 30
MAX_DATA_FILES = 15
MAX_FUNCS = 20
MAX_RECORDS = 2
MAX_RECORD_CHARS = 200
#: 这一段的总字数上限（兜底，防止某个项目特别大）
MAX_TOTAL_CHARS = 4000

HEADER = "【项目资料】（程序自动读的，下面这些可以直接用，不用再问我）"


def _clip(text: Any, limit: int) -> str:
    """压成一行、截断（换行会打乱 prompt 的结构）。"""
    flat = " ".join(str(text if text is not None else "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _flat(value: Any) -> str:
    """任意值 → 一行文本（列表/字典用 JSON，别写成一坨 Python repr）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _clip(value, MAX_VAR_CHARS)
    try:
        return _clip(json.dumps(value, ensure_ascii=False), MAX_VAR_CHARS)
    except Exception:
        return _clip(value, MAX_VAR_CHARS)


def _size_kb(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return ""
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} B"


def _more(shown: int, total: int) -> str:
    return f"，还有 {total - shown} 个没列出来" if total > shown else ""


# ----------------------------------------------------------------------
# 五类资料
# ----------------------------------------------------------------------
def _variables(store: ProjectStore, runtime: Optional[Dict[str, Any]]) -> List[str]:
    """变量清单：项目里定义的 + 运行时真有的（运行时值优先）。"""
    static = store.load_all_variables()
    live = dict(runtime or {})
    names = list(static.keys()) + [k for k in live if k not in static]
    if not names:
        return ["（这个项目还没有变量；用「读取数据」节点或「采集」节点能产出变量）"]
    out = []
    for name in names[:MAX_VARS]:
        value = live.get(name, static.get(name, ""))
        shown = _flat(value)
        out.append(f"· {name} = {shown}" if shown else f"· {name}（空）")
    out.append(f"（共 {len(names)} 个变量，写 {{名字}} 就能用{_more(MAX_VARS, len(names))}；"
               f"也可以随时用 get_local_variable 工具按名字取）")
    return out


def _images(project_dir: Path) -> List[str]:
    """图片库：项目 img/ 里的图（元素模板 / 素材）。"""
    folder = project_dir / "img"
    files = []
    if folder.is_dir():
        files = sorted(p for p in folder.iterdir() if p.is_file())
    if not files:
        return ["（img/ 里还没有图；桌面场景靠图片定位时，先在节点里框一个控件）"]
    names = "、".join(f"{p.name}（{_size_kb(p)}）" for p in files[:MAX_IMAGES] if p.name)
    return [names + _more(MAX_IMAGES, len(files)),
            "（这些图在项目 img/ 目录里，节点里写 img/文件名 就能用）"]


def _auth(project_dir: Path, store: ProjectStore) -> List[str]:
    """登录态：有哪些、什么时候存的、能不能用、本项目当前用哪个。"""
    states = auth_store.list_states(project_dir)
    using = (store.load_auth() or {}).get("name") or ""
    if not states:
        return ["（还没有保存过登录态；勾了「用登录态」跑流程时，会先走登录步骤再存一份）"]
    out = []
    for st in states[:10]:
        mark = "　← 本项目当前在用" if using and st.name == using else ""
        usable = "" if auth_store.is_usable(st.path) else "（用不了：文件是空的或格式不对）"
        out.append(f"· {st.name}：{auth_store.describe(st)}{usable}{mark}")
    if not using:
        out.append("（本项目没指定用哪一份；用本机浏览器时，登录状态由浏览器自己的配置目录保存）")
    return out


def _data(project_dir: Path) -> List[str]:
    """采集数据：采到了多少条、下载了哪些文件、data/ 里还有些什么。"""
    out: List[str] = []
    records_path = datastore.records_path(project_dir)
    records: List[Dict[str, Any]] = []
    if records_path.is_file():
        try:
            total = datastore.count_records(project_dir)
            records = datastore.read_records(project_dir, limit=MAX_RECORDS)
            cols = datastore.columns(records) or []
            out.append(f"· records.jsonl：{total} 条记录"
                       + (f"，字段：{'、'.join(cols[:12])}" if cols else ""))
        except Exception:
            out.append("· records.jsonl（读不出来）")
    files = []
    try:
        files = datastore.list_files(project_dir)
    except Exception:
        pass
    if files:
        newest = sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0,
                        reverse=True)[:3]
        out.append(f"· files/：{len(files)} 个下载文件"
                   + (f"，最近：{'、'.join(p.name for p in newest)}" if newest else ""))

    # data/ 下其它东西（用户自己放的素材、关键词表……）
    data_dir = datastore.data_dir(project_dir)
    files_root = datastore.files_dir(project_dir)
    others: List[Path] = []
    if data_dir.is_dir():
        for p in sorted(data_dir.rglob("*")):
            if not p.is_file() or p.name == records_path.name:
                continue
            if files_root in p.parents:       # files/ 上面已经单独统计过了
                continue
            others.append(p)
    if others:
        shown = others[:MAX_DATA_FILES]
        out.append("· data/ 里还有：" + "、".join(
            f"{p.relative_to(data_dir).as_posix()}（{_size_kb(p)}）" for p in shown)
            + _more(len(shown), len(others)))
    if not out:
        return ["（data/ 里还是空的：采集节点跑过后才会有记录和下载文件）"]
    for rec in records:
        try:
            out.append("· 记录示例：" + _clip(json.dumps(rec, ensure_ascii=False),
                                              MAX_RECORD_CHARS))
        except Exception:
            pass
    return out


def _functions(store: ProjectStore) -> List[str]:
    """函数库：项目里定义好的函数（「调用函数」节点用的那些）。"""
    funcs = store.load_functions()
    if not funcs:
        return ["（函数库里还没有函数；有重复用的逻辑可以定义成函数，一处改、处处生效）"]
    out = []
    for f in funcs[:MAX_FUNCS]:
        params = f.get("params") or ""
        desc = f.get("desc") or ""
        out.append(f"· {f['name']}({params})" + (f" —— {_clip(desc, 60)}" if desc else ""))
    return out + [f"（共 {len(funcs)} 个{_more(MAX_FUNCS, len(funcs))}）"]


# ----------------------------------------------------------------------
# 对外
# ----------------------------------------------------------------------
def collect(project_dir, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把项目的五类资料汇总成一段文字。

    返回 {"text": 段落, "counts": {变量/图片/登录态/数据/函数 的条数}}。
    project_dir 为空或读不到东西时，text 是空串（调用方照旧跑，不因为这里出错而停）。
    """
    if not project_dir:
        return {"text": "", "counts": {}}
    project = Path(project_dir)
    try:
        store = ProjectStore(project)
    except Exception:
        return {"text": "", "counts": {}}

    readers = (
        ("变量清单", lambda: _variables(store, variables)),
        ("图片库", lambda: _images(project)),
        ("登录态", lambda: _auth(project, store)),
        ("采集数据", lambda: _data(project)),
        ("函数库", lambda: _functions(store)),
    )
    sections: List[tuple] = []
    for title, read in readers:
        try:
            sections.append((title, read()))
        except Exception as e:            # 单类读失败不该拖死整段资料
            sections.append((title, [f"（读不出来：{type(e).__name__}）"]))

    parts = [HEADER]
    for title, lines in sections:
        parts.append(f"◆ {title}：")
        parts.extend(lines)
    text = "\n".join(parts)
    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS] + "\n（项目资料太长，后面截掉了）"
    counts = {title: len([x for x in lines if str(x).startswith("·")])
              for title, lines in sections}
    return {"text": text, "counts": counts}


def text_for(project_dir, variables: Optional[Dict[str, Any]] = None) -> str:
    """只要那段文字（给 bridge 用）。"""
    return str(collect(project_dir, variables).get("text") or "")


def summary(project_dir, variables: Optional[Dict[str, Any]] = None) -> str:
    """一句话概括读到了多少（打进运行日志用）。"""
    counts = collect(project_dir, variables).get("counts") or {}
    if not counts:
        return "没有可读的项目资料"
    bits = [f"{k} {v} 条" for k, v in counts.items() if v]
    return "、".join(bits) if bits else "都是空的"
