import json
from pathlib import Path

import envelope



# --- 模块级：只读一次盘 ---
_RULES_PATH = Path(__file__).parent.parent / "config" / "policy_rules.json"
_POLICY = json.loads(_RULES_PATH.read_text(encoding="utf-8"))

_THEMES = _POLICY["_meta"]["themes"]      # {"order_scope": "订单范围（...）", ...}
_RULES  = _POLICY["rules"]                # 20 条

# 用主题说明生成 description 的那一段
_THEME_LINES = "\n".join(f"- {name:<20}{desc}" for name, desc in _THEMES.items())
_THEME_LINES += f"\n- {'all':<20}不确定时传这个，返回全部规则"



policy_lookup_schema = {
    "type":"function",
    "function":{
        "name":"policy_lookup",
        "description":(
            "本工具适用于系统提示词中提供的信息不足时进行主题查阅。\n"
            "可用主题：\n"+_THEME_LINES+"\n"
        ),
        "parameters":{
            "type":"object",
            "properties":{
                "themes":{
                    "type":"array",
                    "items":{
                        "type":"string",
                        "enum":[
                            "order_scope","time_range","metric_definition","algorithm_choice","money_unit","result_disclosure","price_history","address_compare","status_history","user_intent","all"
                        ]
                    },
                    "description":"主题名，可传多个"
                }
            },"required":["themes"]
        },
    }
}


def _order_key(rule):
    prefix, num = rule["rule_id"].split("-")
    return (0 if prefix == "DEF" else 1, int(num))


def policy_lookup_func(themes):
    """按主题返回项目口径规则原文。themes 为字符串数组，可含 "all"。"""

    if isinstance(themes, str):
        themes = [themes]

    if not isinstance(themes, list) or not themes:
        return envelope.fail("bad_parameter", "themes 必须是含至少一个主题名的数组")

    allowed = set(_THEMES) | {"all"}
    unknown = [t for t in themes if t not in allowed]
    if unknown:
        legal = ", ".join(sorted(allowed))
        return envelope.fail("bad_parameter", f"未知主题：{unknown}。可选：{legal}")

    if "all" in themes:
        picked = list(_RULES)
    else:
        wanted = set(themes)
        picked = [rule for rule in _RULES if wanted & set(rule["themes"])]

    picked.sort(key=_order_key)

    return envelope.ok({
        "themes_requested": list(themes),
        "matched": len(picked),
        "rules": [
            {"rule_id": rule["rule_id"], "kind": rule["kind"], "text": rule["text"]}
            for rule in picked
        ],
    })
