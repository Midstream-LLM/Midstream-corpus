"""朝代标签到年份区间的规则映射。

年份用负数表示公元前，-1 为公元前 1 年，没有公元 0 年。区间两端都包含。
没有收录的标签返回 None，不做猜测。
"""

RULE_VERSION = "era-v1"

ERA_YEARS: dict[str, tuple[int, int]] = {
    "周": (-1046, -256),
    "春秋": (-770, -476),
    "战国": (-475, -221),
    "汉": (-202, 220),
    "晋": (266, 420),
    "南朝宋": (420, 479),
    "南朝梁": (502, 557),
    "北齐": (550, 577),
    "唐": (618, 907),
    "宋": (960, 1279),
}


def era_years(label: str | None) -> tuple[int, int] | None:
    if not label:
        return None
    return ERA_YEARS.get(label.strip())
