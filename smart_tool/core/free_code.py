# -*- coding: utf-8 -*-
"""自由代码 / 函数库的语法：把用户写的「一个真正的函数」翻译成能跑的代码。

用户写的就是一个普通的函数定义（Python 或 JavaScript），只有三个「引用符号」：

    def 清洗标题(#价格表=D:/data/价格表.xlsx, 后缀='（已处理）'):
        标题 = @原始标题.strip()      # 读变量清单（直接写 标题 也行）
        @结果 = 标题 + 后缀           # 等号左边是 @名字 → 写回变量清单
        /封面 = 'D:/图片/封面.png'    # 等号左边是 /图片名 → 存回图片库
        return @结果                  # 其它位置的 @名字 → 读变量清单

    · @名字  变量清单里的变量；名字里带点（loop.item.标题）时，
             按「变量清单里最长的那个名字」匹配
    · /名字  图片库里的图片：读＝得到 img/名字.xxx 的绝对路径，
             写（等号左边）＝把图片存回图片库
    · #名字  电脑里的文件（xlsx/csv/txt…）：只写在参数表里，值就是文件路径
             （#价格表=D:/data/价格表.xlsx，路径里可以写 {{变量}}）

执行方式：系统把参数表里的文件路径准备好，**自动调用第一个函数**；
写回完全由 @名字 = 值 完成，return 的值只打进运行日志。
"""
import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# 引用的三种来源
KIND_VAR, KIND_IMG, KIND_FILE = "var", "img", "file"
_SIGILS = {"@": KIND_VAR, "/": KIND_IMG, "#": KIND_FILE}

# 引用名：字母数字下划线中文，点用来接 loop.item.标题 这种
_NAME_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+(?:\.[A-Za-z0-9_\u4e00-\u9fff]+)*")
# 形参名（后面可能跟 =默认值）
_PARAM_RE = re.compile(
    r"(?:async\s+)?def\s+([A-Za-z0-9_\u4e00-\u9fff]+)\s*\(([^)]*)\)")
_JS_FUNC_RE = re.compile(
    r"(?:async\s+)?function\s*\*?\s*([A-Za-z0-9_$\u4e00-\u9fff]+)\s*\(([^)]*)\)")
_JS_ARROW_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z0-9_$\u4e00-\u9fff]+)\s*=\s*"
    r"(?:async\s*)?\(([^)]*)\)\s*=>")

# 这些词后面跟一个表达式，所以 `return /封面` 里的 / 不是除法
_EXPR_WORDS = {
    "python": {
        "return", "yield", "in", "not", "and", "or", "if", "else", "elif",
        "lambda", "assert", "del", "await", "for", "while", "with", "as",
        "import", "from", "global", "nonlocal", "print",
    },
    "javascript": {
        "return", "typeof", "new", "in", "of", "delete", "void", "case",
        "await", "yield", "else", "do", "throw", "instanceof",
    },
}

# 生成 JS 时不能用这些名字注入变量（会被外面的壳占着）
_JS_TAKEN = {"logs", "log", "url", "project_dir", "arg", "vars", "page"}


@dataclass
class Param:
    """函数的一个形参。"""

    name: str                       # 代码里用的名字
    kind: str = "plain"             # file（#文件）/ plain（普通形参）
    default: str = ""               # 文件参数＝路径原文；普通参数＝默认值源码
    has_default: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "default": self.default, "has_default": self.has_default}


@dataclass
class Function:
    """用户代码里的一个函数。"""

    name: str
    params: List[Param] = field(default_factory=list)
    signature: str = ""             # 原文里的签名（报错提示用）
    line_start: int = 1             # 在用户原文里占第几行到第几行（含）
    line_end: int = 1

    @property
    def param_names(self) -> List[str]:
        return [p.name for p in self.params]


@dataclass
class FreeCode:
    """一份用户代码解析出来的东西。"""

    lang: str = "python"
    source: str = ""                # 预处理后的代码（@名字 已换成占位符）
    refs: Dict[str, Tuple[str, str]] = field(default_factory=dict)   # 占位符→(kind,名字)
    funcs: List[Function] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def main(self) -> Optional[Function]:
        """主函数：第一个函数定义，系统自动调用它。"""
        return self.funcs[0] if self.funcs else None

    @property
    def file_params(self) -> List[Param]:
        return [p for p in (self.main.params if self.main else []) if p.kind == KIND_FILE]

    # ------------------------------
    # 生成可执行的东西
    # ------------------------------
    def python_tree(self, args: Dict[str, str]) -> ast.Module:
        """预处理过的代码 + 自动调用主函数 → 可编译的语法树。

        调用时把实参放进 __args__（命名空间里的一个字典），函数按形参名取。
        """
        main = self.main
        tail = f"\n__ret__ = {main.name}(**__args__)\n"
        tree = ast.parse(self.source + tail, "<自由代码>")
        tree = _RefTransform(self.refs).visit(tree)
        ast.fix_missing_locations(tree)
        return tree

    def js_body(self, variables: Sequence[str] = (),
                timeout_ms: int = 30000) -> str:
        """生成网页里执行的 JS（用户在代码框里写的函数 + 自动调用）。"""
        main = self.main
        arg_list = ", ".join("__args__[%s]" % json.dumps(p.name, ensure_ascii=False)
                             for p in main.params)
        inject = []
        for name in variables:
            if not name or name.startswith("__") or name in _JS_TAKEN:
                continue
            if re.fullmatch(r"[^\W\d][\w$]*", name, re.UNICODE) \
                    and not _is_js_reserved(name):
                inject.append(f"  const {name} = __get_var__({json.dumps(name, ensure_ascii=False)});")
        user_code = _indent(self.source, 2)
        return (
            "async (arg) => {\n"
            "  const logs = [];\n"
            "  const log = (m) => logs.push(String(m));\n"
            "  const __vars__ = arg.vars || {};\n"
            "  const __imgs__ = arg.imgs || {};\n"
            "  const __out_imgs__ = {};\n"
            "  const __args__ = arg.args || {};\n"
            "  const url = arg.url;\n"
            "  const project_dir = arg.project_dir;\n"
            "  const __get_var__ = (n) => (__vars__[n] === undefined || __vars__[n] === null)"
            " ? '' : __vars__[n];\n"
            "  const __set_var__ = (n, v) => { __vars__[n] ="
            " (v === undefined || v === null) ? '' : String(v); };\n"
            "  const __img_path__ = (n) => {\n"
            "    const p = __imgs__[n];\n"
            "    if (!p) throw new Error('图片库里没有「' + n + '」这张图');\n"
            "    return p;\n"
            "  };\n"
            "  const __save_img__ = (n, v) => { __out_imgs__[n] = v; };\n"
            + ("\n".join(inject) + "\n" if inject else "")
            + "  let __timer = null;\n"
            "  const __limit = new Promise((_ok, bad) => {\n"
            f"    __timer = setTimeout(() => bad(new Error('__SCRIPT_TIMEOUT__')), {int(timeout_ms)});\n"
            "  });\n"
            "  let __ret;\n"
            "  try {\n"
            "    __ret = await Promise.race([\n"
            "      Promise.resolve().then(async () => {\n"
            + user_code +
            f"\n        return await {main.name}({arg_list});\n"
            "      }),\n"
            "      __limit,\n"
            "    ]);\n"
            "  } finally { clearTimeout(__timer); }\n"
            "  return { vars: __vars__, logs: logs,\n"
            "           ret: __ret === undefined ? null : __ret,\n"
            "           imgs: __out_imgs__ };\n"
            "}"
        )


# ============================================================
# 解析
# ============================================================
def analyze(code: str, lang: str = "python",
            variables: Sequence[str] = (), images: Sequence[str] = (),
            require_func: bool = True,
            require_file_paths: bool = True) -> FreeCode:
    """解析用户代码：找函数、认引用、做预处理。

    返回的 FreeCode.errors 是中文错误提示（空＝没问题）。

    require_file_paths：`#文件` 没写路径算不算错。
    自由代码节点算（没有调用方给它传路径），函数库里的函数不算（调用方给）。
    """
    lang = "javascript" if str(lang).lower().startswith("java") else "python"
    fc = FreeCode(lang=lang)
    if not (code or "").strip():
        return fc
    fc.source, fc.refs, files, fc.errors = _scan(
        code, lang, set(variables or ()), set(images or ()), require_file_paths)
    fc.funcs = _read_functions(fc.source, lang, files, fc.errors)

    if require_func and not fc.funcs:
        if lang == "javascript":
            fc.errors.append(
                "没找到函数：代码里要写一个函数定义，"
                "如 function 处理(#文件=D:/a.xlsx) { … }")
        else:
            fc.errors.append(
                "没找到函数：代码里要写一个函数定义，"
                "如 def 处理(#文件=D:/a.xlsx): … （第一个函数会被自动调用）")
    return fc


def written_vars(code: str, lang: str = "python") -> List[str]:
    """静态看这段代码会写回哪些变量（给「变量没有来源」检查用）。

    只看等号左边的 @名字，宽松一点没关系——宁可少报一个警告，
    也别把用户自己脚本产出的变量报成「没有来源」。
    """
    out: List[str] = []
    for m in re.finditer(
            r"@([A-Za-z0-9_\u4e00-\u9fff.]+)\s*(?:\+|-|\*|/|%|//)?=(?!=)", code or ""):
        name = m.group(1)
        if name and name not in out:
            out.append(name)
    return out


def auto_args(fc: FreeCode, resolve=None) -> Dict[str, str]:
    """自动调用主函数时的实参。

    · 文件参数（#名字=路径）→ 路径（resolve 用来把 {{变量}} 换掉）
    · 普通参数有默认值 → 不传（用函数自己的默认值）
    · 普通参数没默认值 → 空文本
    """
    out: Dict[str, str] = {}
    if fc.main is None:
        return out
    for p in fc.main.params:
        if p.kind == KIND_FILE:
            path = str(p.default or "")
            out[p.name] = resolve(path) if resolve else path
        elif not p.has_default:
            out[p.name] = ""
    return out


def function_source(original: str, fn: Function) -> str:
    """从用户原文里切出这个函数那一段（存进函数库时用，保留 @名字 写法）。

    切的是**原文**（不是预处理后的代码），所以进函数库的代码还是原样可读，
    而且只有这一个函数——别处调用它时不会误跑到同代码框里的另一个函数。
    """
    lines = (original or "").split("\n")
    start = max(0, int(fn.line_start) - 1)
    end = max(start + 1, int(fn.line_end))
    return "\n".join(lines[start:end]).strip("\n")


def parse_call_args(raw: str) -> List[Tuple[str, str]]:
    """「调用函数」节点的实参文本 → [(形参名, 值), …]。

    写法 `形参名=值`，值可写 {{变量}} 或字面量，逗号分隔（如 `单价={{价格}}, 倍数=2`）。
    只写名字没写 `=` 的，当作「把同名变量传进去」。
    """
    out: List[Tuple[str, str]] = []
    for chunk in str(raw or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        name = name.strip()
        if not name:
            continue
        out.append((name, value.strip() if sep else f"{{{{{name}}}}}"))
    return out


# ============================================================
# 内部：扫描 + 预处理
# ============================================================
# JS 里「等号左边的 @名字 / /图片名」：扫描时先记成写标记，最后统一改写成写回调用
_JS_WRITE_RE = re.compile(
    r"(?m)^([ \t]*)(__w_(?:var|img)_\d+__)[ \t]*(\+=|-=|\*=|/=|=)(?!=)(.*)$")


def _is_js_assign(code: str, i: int) -> bool:
    """这个位置后面紧跟着赋值吗（`=` / `+=` …，不是 `==`）。"""
    return bool(re.match(r"[ \t]*(\+=|-=|\*=|/=|=)(?!=)", code[i:]))


def _strip_js_comment(line: str) -> str:
    """去掉行尾的 // 注释（字符串里的 // 不算，比如 https://）。"""
    out, i, n = [], 0, len(line)
    while i < n:
        ch = line[i]
        if ch in "\"'`":
            j = _skip_string(line, i, ch)
            out.append(line[i:j])
            i = j
            continue
        if line[i:i + 2] == "//":
            break
        if line[i:i + 2] == "/*":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _unbalanced(expr: str) -> bool:
    """括号/花括号数量是不是对不上（对不上说明写回跨了行）。"""
    depth, i, n = 0, 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch in "\"'`":
            i = _skip_string(expr, i, ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return depth != 0


def _js_writes(source: str, refs: Dict[str, Tuple[str, str]],
               errors: List[str]) -> str:
    """把 JS 里「等号左边」的引用改写成写回调用（一行一个）。"""
    def repl(m) -> str:
        indent, holder, op, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        kind, name = refs[holder]
        key = json.dumps(name, ensure_ascii=False)
        expr = _strip_js_comment(rest).strip()
        semi = ""
        if expr.endswith(";"):
            expr, semi = expr[:-1].rstrip(), ";"
        if not expr or _unbalanced(expr):
            errors.append(
                f"「{name}」这一行写回要写在一行里（@名字 = 一行表达式），"
                "多行的话请先算出来再赋给它")
            return m.group(0)
        if op != "=":
            get = "__img_path__" if kind == KIND_IMG else "__get_var__"
            expr = f"{get}({key}) {op[0]} ({expr})"
        func = "__save_img__" if kind == KIND_IMG else "__set_var__"
        return f"{indent}{func}({key}, {expr}){semi}"

    return _JS_WRITE_RE.sub(repl, source)


def _scan(code: str, lang: str, known_vars: Set[str],
          known_imgs: Set[str], require_paths: bool = True
          ) -> Tuple[str, Dict[str, Tuple[str, str]], Dict[str, str], List[str]]:
    """逐字符走一遍：跳过字符串与注释，把引用换成占位符。

    返回 (改写后的代码, {占位符: (kind, 名字)}, {文件形参: 路径}, 错误列表)。
    """
    out: List[str] = []
    refs: Dict[str, Tuple[str, str]] = {}
    files: Dict[str, str] = {}
    errors: List[str] = []
    i, n = 0, len(code)
    depth = 0
    in_params = False          # 是否在 def 的括号里（Python 的 #文件 只在这儿认）
    sig_pending = False        # 刚看到 def，下一个 ( 就是参数表
    prev_char = ""             # 前一个非空白字符（判断 / 是除法还是引用）
    prev_word = ""             # 前一个单词（return / 这样的关键字）
    cache: Dict[Tuple[str, str], str] = {}     # 同名的引用共用一个占位符

    while i < n:
        c = code[i]

        # ---- 字符串原样跳过（里面的符号不算引用）----
        if c in "\"'":
            quote = code[i:i + 3] if (lang == "python"
                                      and code[i:i + 3] in ("'''", '"""')) else c
            j = _skip_string(code, i, quote)
            out.append(code[i:j])
            i, prev_char, prev_word = j, quote[-1], ""
            continue
        if c == "`" and lang == "javascript":
            j = _skip_string(code, i, "`")
            out.append(code[i:j])
            i, prev_char, prev_word = j, "`", ""
            continue
        if c == "#" and lang == "python" and not in_params:
            j = code.find("\n", i)
            j = n if j < 0 else j
            out.append(code[i:j])
            i, prev_char, prev_word = j, "", ""
            continue
        if lang == "javascript" and code[i:i + 2] == "//":
            j = code.find("\n", i)
            j = n if j < 0 else j
            out.append(code[i:j])
            i, prev_char, prev_word = j, "", ""
            continue
        if lang == "javascript" and code[i:i + 2] == "/*":
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(code[i:j])
            i, prev_char, prev_word = j, "", ""
            continue

        # ---- 标识符 ----
        if c.isalpha() or c == "_" or c == "$" or ord(c) > 127:
            m = re.match(r"[A-Za-z0-9_$\u4e00-\u9fff]+", code[i:])
            word = m.group(0)
            out.append(word)
            i += len(word)
            prev_char, prev_word = word[-1], word
            if (lang == "python" and word == "def") or \
                    (lang == "javascript" and word == "function"):
                sig_pending = True
            continue

        # ---- 括号 ----
        if c == "(":
            if depth == 0 and sig_pending:
                in_params, sig_pending = True, False
            depth += 1
            out.append(c)
            i, prev_char, prev_word = i + 1, "(", ""
            continue
        if c == ")":
            depth = max(0, depth - 1)
            if depth == 0:
                in_params = False
            out.append(c)
            i, prev_char, prev_word = i + 1, ")", ""
            continue

        # ---- 引用：@变量 / /图片 / #文件 ----
        if c in _SIGILS:
            kind = _SIGILS[c]
            if kind == KIND_FILE and (in_params or (
                    lang == "javascript" and prev_char in "(,")):
                # 参数表里的 #名字=路径 —— 直接换成「形参名=路径字符串」
                m = re.match(r"#([A-Za-z0-9_\u4e00-\u9fff]+)\s*=\s*([^,)\n]*)", code[i:])
                if m:
                    name, path = m.group(1), m.group(2).strip()
                    files[name] = path
                    out.append(f"{name}={_quote(path, lang)}")
                    i += m.end()
                    prev_char, prev_word = ")", ""
                    continue
                m = re.match(r"#([A-Za-z0-9_\u4e00-\u9fff]+)", code[i:])
                if m:
                    name = m.group(1)
                    files[name] = ""
                    if require_paths:
                        errors.append(
                            f"文件参数「#{name}」没写路径：写成 "
                            f"#{name}=D:/目录/文件.xlsx")
                    out.append(f"{name}={_quote('', lang)}")
                    i += m.end()
                    prev_char, prev_word = ")", ""
                    continue
            elif kind != KIND_FILE:
                if _ref_allowed(c, code, i, lang, prev_char, prev_word):
                    name, end = _take_name(code, i + 1, known_vars if kind == KIND_VAR
                                           else known_imgs)
                    if name:
                        # JS 里「等号左边」＝写回：先记成写标记，最后统一改写
                        is_write = (lang == "javascript"
                                    and _is_js_assign(code, end))
                        key = (kind, name, is_write)
                        placeholder = cache.get(key)
                        if placeholder is None:
                            tag = "w" if is_write else "r"
                            placeholder = f"__{tag}_{kind}_{len(cache)}__"
                            cache[key] = placeholder
                            refs[placeholder] = (kind, name)
                        out.append(placeholder)
                        i = end
                        prev_char, prev_word = "_", ""
                        continue

        out.append(c)
        if c in "\n\r":
            prev_char, prev_word = "", ""       # 换行＝新的一行开头
        elif not c.isspace():
            prev_char, prev_word = c, ""
        i += 1

    text = "".join(out)
    if lang == "javascript":
        text = _js_writes(text, refs, errors)   # 等号左边的引用 → 写回调用
        for holder, (kind, name) in refs.items():
            if not holder.startswith("__r_"):   # 剩下的都是读取
                continue
            func = "__img_path__" if kind == KIND_IMG else "__get_var__"
            text = text.replace(
                holder, f"{func}({json.dumps(name, ensure_ascii=False)})")
    return text, refs, files, errors


def _skip_string(code: str, i: int, quote: str) -> int:
    """跳过一段字符串（含三引号 / 反引号），返回结束位置。"""
    n = len(code)
    j = i + len(quote)
    while j < n:
        if code[j] == "\\":
            j += 2
            continue
        if code.startswith(quote, j):
            return j + len(quote)
        j += 1
    return n


def _quote(text: str, lang: str) -> str:
    """把一个路径原样变成字符串字面量（反斜杠按各自语言的规则转义）。"""
    if lang == "javascript":
        return json.dumps(text or "", ensure_ascii=False)
    return repr(text or "")


def _is_value_end(prev_char: str, prev_word: str, lang: str) -> bool:
    """前面是不是「一个值的结尾」——是的话 / 就是除法、@ 就是矩阵乘。"""
    if not prev_char:
        return False
    if prev_word and prev_word in _EXPR_WORDS.get(lang, set()):
        return False
    return prev_char.isalnum() or prev_char in "_)]}.'\"`"


def _ref_allowed(ch: str, code: str, i: int, lang: str,
                 prev_char: str, prev_word: str) -> bool:
    """这个符号现在算引用，还是普通运算符 / 装饰器？"""
    if _is_value_end(prev_char, prev_word, lang):
        return False
    if ch == "/" and lang == "javascript":
        # 正则字面量 /xxx/ 和除法 a / b 都不当图片引用
        m = _NAME_RE.match(code, i + 1)
        if m and code[m.end():m.end() + 1] == "/":
            return False
    if ch == "@" and lang == "python" and prev_char in ("", "\n"):
        # 行首的 @ 默认当装饰器；只有后面跟着赋值才算「写回变量」
        return bool(re.match(r"@[A-Za-z0-9_\u4e00-\u9fff.]+"
                             r"\s*(?:\+|-|\*|/|%|//|\*\*)?=(?!=)", code[i:]))
    return True


def _take_name(code: str, i: int, known: Set[str]) -> Tuple[str, int]:
    """从 i 开始取引用名。

    名字里带点时按「已知名字里最长的那个」匹配，这样
    `@标题.strip()` 取到 `标题`、`@loop.item.标题` 取到 `loop.item.标题`。
    """
    m = _NAME_RE.match(code, i)
    if not m:
        return "", i
    raw = m.group(0)
    if "." in raw:
        parts = raw.split(".")
        for k in range(len(parts), 0, -1):
            cand = ".".join(parts[:k])
            if cand in known:
                return cand, i + len(cand)
    return raw, m.end()


def _is_js_reserved(name: str) -> bool:
    return name in {
        "var", "let", "const", "function", "class", "return", "if", "else",
        "for", "while", "do", "new", "this", "super", "typeof", "in", "of",
        "try", "catch", "finally", "throw", "switch", "case", "default",
        "break", "continue", "delete", "void", "yield", "await", "async",
        "import", "export", "extends", "static", "get", "set", "null",
        "true", "false", "undefined", "NaN", "Infinity", "arguments",
    }


def _indent(code: str, levels: int) -> str:
    pad = "  " * levels
    return "\n".join((pad + line) if line.strip() else line
                     for line in str(code or "").split("\n"))


# ============================================================
# 内部：读取函数签名
# ============================================================
def _read_functions(source: str, lang: str, files: Dict[str, str],
                    errors: List[str]) -> List[Function]:
    """从预处理后的代码里读出函数（名字 + 形参）。"""
    if lang == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            errors.append(f"第 {int(e.lineno or 1)} 行语法有问题：{e.msg}")
            return []
        out: List[Function] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(_python_func(node, files))
        return out
    return _js_functions(source, files)


def _python_func(node, files: Dict[str, str]) -> Function:
    args = node.args
    defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    params: List[Param] = []
    for a, d in zip(args.args, defaults):
        if a.arg in files:
            params.append(Param(name=a.arg, kind=KIND_FILE,
                                default=files[a.arg], has_default=True))
        else:
            params.append(Param(
                name=a.arg, kind="plain",
                default=ast.unparse(d) if d is not None else "",
                has_default=d is not None))
    if args.vararg or args.kwonlyargs:
        pass                    # *args / 关键字参数不参与自动调用，忽略即可
    start = min([int(node.lineno)] + [int(d.lineno) for d in node.decorator_list])
    return Function(name=node.name, params=params,
                    signature=f"def {node.name}(...)",
                    line_start=start,
                    line_end=int(getattr(node, "end_lineno", node.lineno)))


def _js_functions(source: str, files: Dict[str, str]) -> List[Function]:
    out: List[Function] = []
    for rx in (_JS_FUNC_RE, _JS_ARROW_RE):
        for m in rx.finditer(source):
            name, raw_params = m.group(1), m.group(2)
            if any(f.name == name for f in out):
                continue
            params: List[Param] = []
            for chunk in _split_params(raw_params):
                pname, sep, pdefault = chunk.partition("=")
                pname = pname.strip().lstrip(".")
                if not pname:
                    continue
                if pname in files:
                    params.append(Param(name=pname, kind=KIND_FILE,
                                        default=files[pname], has_default=True))
                else:
                    params.append(Param(name=pname, kind="plain",
                                        default=pdefault.strip(),
                                        has_default=bool(sep)))
            start, end = _js_func_span(source, m)
            out.append(Function(name=name, params=params,
                                signature=f"function {name}(...)",
                                line_start=source.count("\n", 0, start) + 1,
                                line_end=source.count("\n", 0, end) + 1))
    return out


def _js_func_span(source: str, m) -> Tuple[int, int]:
    """从签名匹配处往后，找出整个函数（含大括号函数体）的起止位置。"""
    n = len(source)
    i = m.end()
    while i < n and source[i].isspace():
        i += 1
    if i >= n or source[i] != "{":
        # 箭头函数直接写表达式（没大括号）：算到这一行结束
        k = source.find("\n", m.start())
        return m.start(), (n if k < 0 else k)
    depth, j = 0, i
    while j < n:
        ch = source[j]
        if ch in "\"'`":
            j = _skip_string(source, j, ch)
            continue
        if source[j:j + 2] == "//":
            k = source.find("\n", j)
            j = n if k < 0 else k
            continue
        if source[j:j + 2] == "/*":
            k = source.find("*/", j + 2)
            j = n if k < 0 else k + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return m.start(), j + 1
        j += 1
    return m.start(), n


def _split_params(raw: str) -> List[str]:
    """按顶层逗号切形参（默认值里可能有逗号）。"""
    out, depth, buf = [], 0, ""
    for ch in raw or "":
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return [x.strip() for x in out if x.strip()]


# ============================================================
# 内部：Python 的 @名字 变换（读 / 写）
# ============================================================
class _RefTransform(ast.NodeTransformer):
    """把占位符换成读写调用。

    - 等号左边（Store）：`@结果 = 值` → `__set_var__('结果', 值)`
    - 其它位置（Load）：`@结果`   → `__get_var__('结果')`
    - 图片：读 → `__img_path__('封面')`；写 → `__save_img__('封面', 值)`
    """

    def __init__(self, refs: Dict[str, Tuple[str, str]]):
        self.refs = refs

    # ---- 组装小工具 ----
    def _call(self, func: str, args: List[ast.expr], node) -> ast.expr:
        return ast.copy_location(
            ast.Call(func=ast.Name(id=func, ctx=ast.Load()),
                     args=args, keywords=[]), node)

    def _name(self, text: str, node) -> ast.expr:
        return ast.copy_location(ast.Constant(value=text), node)

    def _write_call(self, kind: str, name: str, value: ast.expr, node) -> ast.Expr:
        func = "__save_img__" if kind == KIND_IMG else "__set_var__"
        return ast.copy_location(
            ast.Expr(value=self._call(
                func, [self._name(name, node), value], node)), node)

    def _read(self, kind: str, name: str, node) -> ast.expr:
        func = "__img_path__" if kind == KIND_IMG else "__get_var__"
        return self._call(func, [self._name(name, node)], node)

    def _ref_of(self, node) -> Optional[Tuple[str, str]]:
        if isinstance(node, ast.Name) and node.id in self.refs:
            return self.refs[node.id]
        return None

    # ---- 读 ----
    def visit_Name(self, node: ast.Name):
        ref = self._ref_of(node)
        if ref and isinstance(node.ctx, ast.Load):
            return self._read(ref[0], ref[1], node)
        return node

    # ---- 写 ----
    def visit_Assign(self, node: ast.Assign):
        if len(node.targets) == 1:
            ref = self._ref_of(node.targets[0])
            if ref:
                return self._write_call(ref[0], ref[1], self.visit(node.value), node)
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        ref = self._ref_of(node.target)
        if ref and node.value is not None:
            return self._write_call(ref[0], ref[1], self.visit(node.value), node)
        return self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        ref = self._ref_of(node.target)
        if ref:
            kind, name = ref
            left = self._read(kind, name, node.target)
            right = ast.BinOp(left=left, op=node.op, right=self.visit(node.value))
            return self._write_call(kind, name,
                                    ast.copy_location(right, node), node)
        return self.generic_visit(node)

    def visit_For(self, node: ast.For):
        if self._ref_of(node.target):
            return node         # 不支持拿 @名字 当循环变量，原样留着让它报错
        return self.generic_visit(node)
