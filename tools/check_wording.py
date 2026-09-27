"""扫描面向用户的文本，禁止出现"只对某台机器/某个人的历史成立"的措辞。

    python tools/check_wording.py

为什么要有它：程序的说明文本必须任何用户都看得懂、且对任何环境都成立。像
"不用再去 HMCL 导出脚本"、"你原来那套调参"、"…\\zulu-25\\bin\\java.exe" 这类
写死了某个人工作流或某台机器目录的句子，对别的用户就是误导。做成检查是为了
以后不再写回去。

怎么界定"面向用户"：用 AST 取出字符串字面量（因此**代码注释天然不在范围内** ——
注释里写 "参数与 HMCL 对齐" 是给维护者看的工程依据，不是用户会读到的文案），
再跳过 docstring，只留下含中文的字面量，以及含禁用词/疑似写死路径的字面量。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (ROOT / "mcsl",)
ALSO_SCAN = (ROOT / "README.md",)

# 只对某一台机器 / 某一段个人历史成立的措辞（会印给用户看的）
BANNED = (
    "HMCL",
    "1.ps1",
    "Minecraft Speedrun.ps1",
    "你原来", "你之前", "你以前", "你的电脑", "你手上的", "你可以看到我",
    "我写的", "我们之前", "我之前", "我的", "咱们",
    "这台机器", "本机上测得",
    "zulu-",            # 某台机器装的具体发行版目录
    "速通模组",          # 某个人的整合包内容
    "Blazkness",         # 某个人的账号名（发布者标识 SampleBlazkness 不算）
)

# 这些文件里出现具体路径是"配置默认值"，不是文案
SKIP_FILES = {ROOT / "mcsl" / "config" / "defaults.json"}

# README 里提到别的启动器（HMCL / 官方启动器）是"与谁兼容、跟谁对比"的工程说明，
# 任何用户读了都成立，不算针对性文案；但个人化的措辞同样不许出现。
README_BANNED = tuple(word for word in BANNED if word != "HMCL")


def iter_user_strings(path: Path):
    """产出 (行号, 字符串) —— 只含面向用户的那些。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        text = node.value
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)
        if has_cjk or any(word in text for word in BANNED):
            yield node.lineno, text


def main() -> int:
    violations: list[tuple[Path, int, str, str]] = []

    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.py")):
            if path in SKIP_FILES:
                continue
            for lineno, text in iter_user_strings(path):
                for word in BANNED:
                    if word in text and not (word == "Blazkness" and "SampleBlazkness" in text):
                        violations.append((path.relative_to(ROOT), lineno, word,
                                           text.replace("\n", " ")[:120]))
                        break

    for path in ALSO_SCAN:
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for word in README_BANNED:
                if word in line and not (word == "Blazkness" and "SampleBlazkness" in line):
                    violations.append((path.relative_to(ROOT), lineno, word, line.strip()[:120]))
                    break

    if not violations:
        print("文案检查通过：面向用户的文本里没有只对某台机器/某个人成立的措辞")
        return 0
    print(f"发现 {len(violations)} 处需要改写：")
    for path, lineno, word, snippet in violations:
        print(f"  {path}:{lineno}  [{word}]  {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
