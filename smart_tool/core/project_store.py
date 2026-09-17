# -*- coding: utf-8 -*-
"""项目与步骤文件管理：读写 steps.json、管理 img 目录。

steps.json 结构：
{
  "steps": [ {id, action, url?, locator?, value?, wait_after?, wait_target?, prompt?, note?}, ... ],
  "variables": {"row.标题": "来自数据源的标题字段", ...}
}
"""
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from smart_tool import paths


@dataclass
class Locator:
    """元素定位：XPath 或 截图。"""
    type: str = "xpath"        # "xpath" | "image"
    value: str = ""


@dataclass
class Step:
    """单个步骤。"""
    id: int
    # navigate/click/fill/select/pause_for_human/loop_start/loop_end/script
    action: str
    url: str = ""                          # navigate 用
    locator: Optional[Locator] = None      # click/fill/select 用
    value: str = ""                        # fill 用（可含 {{变量}}）
    wait_after: str = ""                   # element_present/url_changed/network_idle/manual
    wait_target: str = ""                 # 等待目标（XPath 或 URL 片段）
    wait_seconds: float = 0.0             # 执行完这个步骤后再固定等 N 秒（0=不等）
    prompt: str = ""                      # pause_for_human 的提示
    note: str = ""                        # 用户备注
    # ---- pause_for_human 专用：人工暂停后的恢复信号 ----
    # manual              仅人工点"继续"
    # url_changed         当前 URL 包含 resume_url（支持 * 通配）
    # element_present     resume_element(XPath) 出现在页面且可见
    # url_and_element     双重信号：URL 与元素同时满足（默认推荐，防误判）
    resume_condition: str = "manual"
    resume_url: str = ""
    resume_element: str = ""
    resume_timeout: int = 300             # 等待人工操作的最长秒数
    # 画布坐标 [x, y]（自由拖拽画布用；None 表示首次加载时自动排版）
    pos: Optional[List[float]] = None
    # ---- script 专用：自由代码节点 ----
    script_lang: str = "python"           # python | javascript
    script_code: str = ""
    script_timeout: int = 30              # 秒（JS 生效；Python 无法强制中断）
    script_vars: str = ""                 # 逗号分隔的变量名；空=传入全部变量
    # ---- loop_start 专用：这次循环遍历什么 ----
    # data  数据源（在【数据源…】里配置，每行一项，产出 row.* / file.*）
    # list  变量或手动列表（loop_items 每行一项，可写 {{变量}}）
    # range 索引范围（loop_range：10 = 索引 0~9；0-10；{{变量}} 按变量长度）
    loop_source: str = "data"
    loop_items: str = ""
    loop_range: str = ""
    # ---- condition_start 专用：条件分支 ----
    # cond_mode: equal  把 cond_expr 渲染成文本，跟分支的匹配值比相等
    #            expr   Python 表达式（能当数字的变量按数字代入），
    #                   结果为 True/False 时走第 1/2 个分支，其他结果按值匹配
    cond_mode: str = "equal"
    cond_expr: str = ""
    # 分支清单，顺序＝各分支块的先后： [{"name": "北京", "values": "北京,上海"}, ...]
    cond_branches: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "action": self.action}
        if self.url:
            d["url"] = self.url
        if self.locator:
            d["locator"] = asdict(self.locator)
        if self.value:
            d["value"] = self.value
        if self.wait_after:
            d["wait_after"] = self.wait_after
            if self.wait_target:
                d["wait_target"] = self.wait_target
        if self.wait_seconds:
            d["wait_seconds"] = self.wait_seconds
        if self.prompt:
            d["prompt"] = self.prompt
        if self.note:
            d["note"] = self.note
        if self.action == "pause_for_human":
            d["resume_condition"] = self.resume_condition
            if self.resume_url:
                d["resume_url"] = self.resume_url
            if self.resume_element:
                d["resume_element"] = self.resume_element
            d["resume_timeout"] = self.resume_timeout
        if self.action == "script":
            d["script_lang"] = self.script_lang
            d["script_code"] = self.script_code
            d["script_timeout"] = self.script_timeout
            if self.script_vars:
                d["script_vars"] = self.script_vars
        if self.action == "loop_start":
            d["loop_source"] = self.loop_source
            if self.loop_items:
                d["loop_items"] = self.loop_items
            if self.loop_range:
                d["loop_range"] = self.loop_range
        if self.action == "condition_start":
            d["cond_mode"] = self.cond_mode
            if self.cond_expr:
                d["cond_expr"] = self.cond_expr
            d["cond_branches"] = [dict(m) for m in self.cond_branches]
        if self.pos is not None:
            d["pos"] = [float(self.pos[0]), float(self.pos[1])]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Step":
        loc = d.get("locator")
        locator = Locator(**loc) if loc else None
        pos = d.get("pos")
        if pos is not None and len(pos) >= 2:
            pos = [float(pos[0]), float(pos[1])]
        else:
            pos = None
        return cls(
            id=d["id"],
            action=d["action"],
            url=d.get("url", ""),
            locator=locator,
            value=d.get("value", ""),
            wait_after=d.get("wait_after", ""),
            wait_target=d.get("wait_target", ""),
            wait_seconds=float(d.get("wait_seconds", 0) or 0),
            prompt=d.get("prompt", ""),
            note=d.get("note", ""),
            resume_condition=d.get("resume_condition", "manual"),
            resume_url=d.get("resume_url", ""),
            resume_element=d.get("resume_element", ""),
            resume_timeout=int(d.get("resume_timeout", 300)),
            pos=pos,
            script_lang=d.get("script_lang", "python"),
            script_code=d.get("script_code", ""),
            script_timeout=int(d.get("script_timeout", 30)),
            script_vars=d.get("script_vars", ""),
            loop_source=d.get("loop_source", "data") or "data",
            loop_items=d.get("loop_items", ""),
            loop_range=d.get("loop_range", ""),
            cond_mode=d.get("cond_mode", "equal") or "equal",
            cond_expr=d.get("cond_expr", ""),
            cond_branches=[dict(m) for m in d.get("cond_branches", [])
                           if isinstance(m, dict)],
        )


class ProjectStore:
    """单个自动化项目的存储管理。"""

    def __init__(self, project_dir: Path):
        self.dir = project_dir
        self.steps_file = project_dir / "steps.json"
        self.img_dir = project_dir / "img"

    @property
    def name(self) -> str:
        return self.dir.name

    def ensure(self):
        """确保项目目录与 img 目录存在。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.img_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        """读取整个 steps.json。"""
        if not self.steps_file.exists():
            return {"steps": [], "variables": {}, "data_source": {}, "layout": ""}
        return json.loads(self.steps_file.read_text(encoding="utf-8"))

    def load_steps(self) -> List[Step]:
        return [Step.from_dict(s) for s in self.load().get("steps", [])]

    def load_variables(self) -> Dict[str, str]:
        return self.load().get("variables", {})

    def load_data_source(self) -> dict:
        return self.load().get("data_source", {}) or {}

    def load_layout_version(self) -> str:
        """画布排版版本；与当前版本不一致时自动重排为横向布局。"""
        return self.load().get("layout", "") or ""

    def save(self, steps: List[Step], variables: Optional[Dict[str, str]] = None,
             layout_version: Optional[str] = None):
        """保存步骤与变量。variables/data_source/layout 为 None 时保留原值。"""
        self.ensure()
        old = self.load()
        data: Dict[str, Any] = {"steps": [s.to_dict() for s in steps]}
        data["variables"] = (
            variables if variables is not None else old.get("variables", {})
        )
        data["data_source"] = old.get("data_source", {})
        data["layout"] = (
            layout_version if layout_version is not None
            else old.get("layout", "")
        )
        self.steps_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_variables(self, variables: Dict[str, str]):
        """只更新变量，步骤与数据源保持不变。"""
        self.save(self.load_steps(), variables)

    def save_data_source(self, data_source: dict):
        """只更新数据源配置，步骤与变量保持不变。"""
        self.ensure()
        old = self.load()
        data = {
            "steps": old.get("steps", []),
            "variables": old.get("variables", {}),
            "data_source": data_source,
            "layout": old.get("layout", ""),
        }
        self.steps_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def image_count(self) -> int:
        """img 目录内图片文件数量（删除前提示用）。"""
        if not self.img_dir.exists():
            return 0
        exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
        return sum(1 for f in self.img_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in exts)

    def delete(self):
        """删除整个项目目录（含 steps.json 与 img 截图）。"""
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


# Windows 文件夹名非法字符
_INVALID_NAME_CHARS = set('\\/:*?"<>|')


def validate_project_name(name: str) -> Optional[str]:
    """返回错误信息；合法返回 None。"""
    name = name.strip()
    if not name:
        return "项目名称不能为空"
    if any(c in _INVALID_NAME_CHARS for c in name):
        return '项目名称不能包含 \\ / : * ? " < > | 等字符'
    if name in (".", ".."):
        return "项目名称不合法"
    if (paths.PROJECTS_DIR / name).exists():
        return f"项目「{name}」已存在"
    return None


def list_projects() -> List[ProjectStore]:
    """列出所有项目（含 steps.json 的目录）。"""
    paths.ensure_dirs()
    result = []
    for p in sorted(paths.PROJECTS_DIR.iterdir()):
        if p.is_dir() and (p / "steps.json").exists():
            result.append(ProjectStore(p))
    return result


def create_project(name: str, initial_url: str = "") -> ProjectStore:
    """新建项目。initial_url 非空时自动生成第 1 步 navigate。"""
    paths.ensure_dirs()
    err = validate_project_name(name)
    if err:
        raise ValueError(err)
    store = ProjectStore(paths.PROJECTS_DIR / name.strip())
    store.ensure()
    if not store.steps_file.exists():
        steps: List[Step] = []
        url = initial_url.strip()
        if url:
            steps.append(Step(id=1, action="navigate", url=url))
        store.save(steps, {})
    return store
