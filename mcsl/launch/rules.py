"""Mojang 版本 JSON 的 rules 求值。

规则语义（与官方启动器一致）：
    * 没有 rules        → 允许
    * 有 rules          → 默认不允许；按顺序找第一条全部条件都匹配的规则，
                          该规则的动作（allow / disallow）就是最终结论
    * 条件包括 os.name / os.version（正则）/ os.arch，以及 features 开关
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

log = logging.getLogger(__name__)


def evaluate_rules(rules: list[dict[str, Any]] | None,
                   *, os_name: str = "windows", os_version: str = "",
                   os_arch: str = "x86_64",
                   features: Mapping[str, bool] | None = None) -> bool:
    if not rules:
        return True
    features = features or {}

    for rule in rules:
        if _rule_matches(rule, os_name=os_name, os_version=os_version,
                         os_arch=os_arch, features=features):
            return rule.get("action", "disallow") == "allow"
    return False


def _rule_matches(rule: dict[str, Any], *, os_name: str, os_version: str,
                  os_arch: str, features: Mapping[str, bool]) -> bool:
    os_spec = rule.get("os")
    if os_spec:
        name = os_spec.get("name")
        if name and name != os_name:
            return False
        arch = os_spec.get("arch")
        if arch and arch != os_arch:
            return False
        version_re = os_spec.get("version")
        if version_re:
            try:
                if not re.search(version_re, os_version or ""):
                    return False
            except re.error as exc:
                log.warning("版本规则正则非法 %r：%s", version_re, exc)
                return False

    rule_features = rule.get("features")
    if rule_features:
        for key, expected in rule_features.items():
            if bool(features.get(key, False)) != bool(expected):
                return False

    # 只带 "action" 的规则（无任何条件）视为无条件匹配
    return True
