# -*- coding: utf-8 -*-
"""项目资料：把「这个项目里现成有什么」自动读出来，交给页面里的 AI。

任务节点是**一句人话**，但一句话要落地，往往得知道项目里有什么现成的东西：
变量清单里有哪些名字、img/ 里有哪些图、有没有登录态、data/ 里采到了什么、
函数库里有哪些函数。这些不该让用户每次手打一遍 —— 这里自动读出来，拼成一段
「项目资料」附在任务描述后面，AI 一眼就知道有什么可用。

**只给名字，不给值**（这是这一版最重要的规矩）：
变量值、采集到的数据内容都可能敏感（密码、cookie、客户资料……），所以一律不进模型。
AI 想用某个值就写 `{{名字}}`，程序在**真正敲进输入框之前**才替换成真值
（见 page_agent/bridge.py 的 `_fill_vars`）。要给 AI 看内容本身，那是显式动作：
让它调 `get_local_variable` 工具去取（那一步会把值发给模型）。

其它规矩：
· **只读**，不改任何文件；
· 全部有上限（变量 40 个、图片 30 张、总字数 4000……）—— 这段文字每次调模型都会带上；
· 运行时看得到**有哪些变量**（循环里的 loop.item.* 也在），编辑时看的是项目里定义的清单。
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
#: 变量名连着写时的折行宽度
NAME_WRAP = 68
#: 这一段的总字数上限（兜底，防止某个项目特别大）
MAX_TOTAL_CHARS = 4000

HEADER = "【项目资料】（程序自动读的，下面这些可以直接用，不用再问我）"
#: 不给值时的提醒（默认）
NOTE_HIDE = ("★ 变量只给了名字，**值不会发给你**：要填某个值就在文本里原样写 "
             "{{名字}}（花括号别改），程序会在真正输入的那一刻替换成真值。")
#: 这个节点勾了「把变量值也给 AI 看」时的提醒
NOTE_SHOW = ("★ 这一步勾了「把变量值也给 AI 看」，所以下面是真值 —— "
             "含密码之类的敏感内容时要留神（值会随任务发给模型）。")


def _clip(text: Any, limit: int) -> str:
    """压成一行、截断（换行会打乱 prompt 的结构）。"""
    flat = " ".join(str(text if text is not None else "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _flat(value: Any) -> str:
    """任意值 → 一行短文本（只在「允许给 AI 看值」时才用）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _clip(value, MAX_VAR_CHARS)
    try:
        return _clip(json.dumps(value, ensure_ascii=False), MAX_VAR_CHARS)
    except Exception:
        return _clip(value, MAX_VAR_CHARS)


def _wrap_names(names: List[str], prefix: str = "· ", sep: str = "、",
                width: int = NAME_WRAP) -> List[str]:
    """一串名字连着写、到宽度就折行（比一行一个省地方）。"""
    lines: List[str] = []
    cur = prefix
    for name in names:
        piece = name if cur == prefix else sep + name
        if cur != prefix and len(cur) + len(piece) > width:
            lines.append(cur)
            cur = prefix + name
        else:
            cur += piece
    if cur != prefix:
        lines.append(cur)
    return lines


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
def _variables(store: ProjectStore, runtime: Optional[Dict[str, Any]],
               with_values: bool = False) -> List[str]:
    """变量清单。

    默认**只报名字不报值**（值可能敏感，由程序在输入时替换）；节点上勾了
    「把变量值也给 AI 看」时才带上值（`with_values=True`）。

    运行时（runtime）传进来的变量一并算上：比如循环里的 loop.item.*、上一步产出的字段。
    """
    static = store.load_all_variables()
    live = dict(runtime or {})
    names = list(static.keys()) + [k for k in live if k not in static]
    if not names:
        return ["（这个项目还没有变量；用「读取数据」节点或「采集」节点能产出变量）"]

    if with_values:
        out = []
        for name in names[:MAX_VARS]:
            value = live.get(name, static.get(name, ""))
            shown = _flat(value)
            out.append(f"· {name} = {shown}" if shown else f"· {name}（空）")
        out.append(f"（共 {len(names)} 个变量{_more(MAX_VARS, len(names))}，"
                   "长值截断了；写 {{名字}} 也一样能用）")
        return out

    out = _wrap_names(names[:MAX_VARS])
    out.append(f"（共 {len(names)} 个变量{_more(MAX_VARS, len(names))}。"
               "写 {{名字}} 就能把值填进去 —— 值由程序替换，不会发给你；"
               "这个节点要是需要读懂内容本身（比如要改写它），"
               "在节点上勾「把变量值也给 AI 看」）")
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
    """采集数据：**只说有多少、有哪些字段和文件，不给内容**（内容可能敏感）。

    记录内容想进模型得走显式动作（`{{变量}}` 由程序填值，或让 AI 用取变量工具）。
    """
    out: List[str] = []
    records_path = datastore.records_path(project_dir)
    if records_path.is_file():
        try:
            total = datastore.count_records(project_dir)
            sample = datastore.read_records(project_dir, limit=1)
            cols = datastore.columns(sample) or []
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
    out.append("（只报数量、字段名和文件名，**记录内容不给你**；要填进页面就在文本里写 "
               "{{变量名}}，程序会替换）")
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
def collect(project_dir, variables: Optional[Dict[str, Any]] = None,
            with_values: bool = False) -> Dict[str, Any]:
    """把项目的五类资料汇总成一段文字。

    with_values=True 时变量清单**带上真值**（只有节点上勾了「把变量值也给 AI 看」
    才这样，默认不给 —— 值可能敏感）。

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
        ("变量清单", lambda: _variables(store, variables, with_values)),
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

    parts = [HEADER, NOTE_SHOW if with_values else NOTE_HIDE]
    for title, lines in sections:
        parts.append(f"◆ {title}：")
        parts.extend(lines)
    text = "\n".join(parts)
    if len(text) > MAX_TOTAL_CHARS:
        text = text[:MAX_TOTAL_CHARS] + "\n（项目资料太长，后面截掉了）"
    counts = {title: len([x for x in lines if str(x).startswith("·")])
              for title, lines in sections}
    return {"text": text, "counts": counts}


def text_for(project_dir, variables: Optional[Dict[str, Any]] = None,
             with_values: bool = False) -> str:
    """只要那段文字（给 bridge 用）。"""
    return str(collect(project_dir, variables, with_values).get("text") or "")


def summary(project_dir, variables: Optional[Dict[str, Any]] = None,
            with_values: bool = False) -> str:
    """一句话概括读到了多少（打进运行日志用）。"""
    counts = collect(project_dir, variables, with_values).get("counts") or {}
    if not counts:
        return "没有可读的项目资料"
    bits = [f"{k} {v} 条" for k, v in counts.items() if v]
    return "、".join(bits) if bits else "都是空的"
