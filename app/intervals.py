"""超温区间计算与差异比对（纯函数，便于单测）。

规则：
- 同设备有效（未撤销）读数按 (采样时间, 事件号) 升序排列；
- 温度严格大于上限的读数为“超限”；
- 连续 >= 2 条超限读数构成一个超温区间；
- 区间起点为首个超限读数，终点为区间后首个不超限读数；
- 若超限一直延续到最后一条读数，则区间未结束，终点为空；
- 区间以起点读数的事件号为唯一标识。
"""
from typing import Dict, List


def compute_intervals(readings: List[dict], limit: float) -> List[dict]:
    """由有序有效读数计算当前全部超温区间。"""
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
        if r["temperature"] > limit:
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
