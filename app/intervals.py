"""超温区间计算与差异比对（纯函数，便于单测）。

规则：
- 同设备有效（未撤销）读数按 (采样时间, 事件号) 升序排列；
- 温度严格大于上限的读数为“超限”（每条读数的上限可由其采样时间所处策略决定）；
- 连续 >= 2 条超限读数构成一个超温区间；
- 区间起点为首个超限读数，终点为区间后首个不超限读数；
- 若超限一直延续到最后一条读数，则区间未结束，终点为空；
- 区间以起点读数的事件号为唯一标识。
"""
from bisect import bisect_right
from typing import Callable, Dict, List, Sequence, Tuple, Union

# 上限可以是固定数值（全局上限），也可以是“采样时间 -> 上限”的回调（按策略解析）
LimitSource = Union[float, Callable[[str], float]]


def policy_limit_resolver(
    policies: Sequence[Tuple[str, float]], default_limit: float
) -> Callable[[str], float]:
    """把按生效时间升序的 (effective_from, temp_limit) 策略列表解析为上限回调。

    某采样时间适用的上限 = 最后一个 effective_from <= 采样时间的策略阈值
    （生效时间含该时刻本身）；所有策略生效之前的时间回退到全局上限。
    时间为固定宽度 UTC ISO 字符串，字典序即时间序，可直接二分查找。
    """
    times = [p[0] for p in policies]
    limits = [p[1] for p in policies]

    def resolve(sampled_at: str) -> float:
        idx = bisect_right(times, sampled_at) - 1
        return limits[idx] if idx >= 0 else default_limit

    return resolve


def compute_intervals(readings: List[dict], limit: LimitSource) -> List[dict]:
    """由有序有效读数计算当前全部超温区间。

    limit 为数值时所有读数共用一个上限；为回调时按每条读数的采样时间
    解析其适用上限（用于按采样时间生效的温度上限策略）。
    """
    resolve = limit if callable(limit) else (lambda _sampled_at: limit)
    intervals: List[dict] = []
    run: List[dict] = []

    def close(run: List[dict], end: dict | None) -> dict:
        return {
            "start_event_no": run[0]["event_no"],
            "start_sampled_at": run[0]["sampled_at"],
            "start_temperature": run[0]["temperature"],
            "end_event_no": end["event_no"] if end else None,
            "end_sampled_at": end["sampled_at"] if end else None,
            "end_temperature": end["temperature"] if end else None,
            "reading_count": len(run),
            "max_temperature": max(r["temperature"] for r in run),
        }

    for r in readings:
        if r["temperature"] > resolve(r["sampled_at"]):
            run.append(r)
        else:
            if len(run) >= 2:
                intervals.append(close(run, r))
            run = []
    if len(run) >= 2:
        intervals.append(close(run, None))
    return intervals


def diff_intervals(before: List[dict], after: List[dict]) -> Dict[str, List[dict]]:
    """以起点事件号为键比对前后区间集合。

    - added:       新出现的事件号；
    - invalidated: 消失的事件号（起点改变时旧区间即在此体现）；
    - changed:     事件号相同但内容不同（如终点、峰值变化），取变更后版本。
    """
    before_by_key = {i["start_event_no"]: i for i in before}
    after_by_key = {i["start_event_no"]: i for i in after}
    added = [i for k, i in after_by_key.items() if k not in before_by_key]
    invalidated = [i for k, i in before_by_key.items() if k not in after_by_key]
    changed = [
        after_by_key[k]
        for k in before_by_key.keys() & after_by_key.keys()
        if before_by_key[k] != after_by_key[k]
    ]
    return {"added": added, "changed": changed, "invalidated": invalidated}
