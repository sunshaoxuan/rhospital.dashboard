"""Read-only colony lab analytics from retained game logs."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WIN_MESSAGE = "挑战成功，奖励已发放，本次不消耗实验机会。"
LOSS_MESSAGE = "挑战失败，本次消耗1次实验机会。"
EMPTY_MESSAGE = "实验机会已用完，可前往商店补充实验机会。"
REFILL_REASONS = ("菌落清除室补满实验机会", "商店购买: 菌落清除室实验机会补满 x 1")


def local_time(value, zone):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(zone)


def rate(numerator, denominator):
    return round(numerator / denominator * 100, 2) if denominator else None


def build_bacteria_stats(events, purchases, players, levels, now, zone_id):
    zone = ZoneInfo(zone_id)
    now = now.astimezone(zone)
    first_day = now.date() - timedelta(days=6)
    daily = {(first_day + timedelta(days=i)).isoformat(): {
        "day": (first_day + timedelta(days=i)).isoformat(), "wins": 0, "losses": 0,
        "settlements": 0, "active_hospitals": 0, "purchase_count": 0,
        "buyers": 0, "yuanbao": 0, "empty_requests": 0,
    } for i in range(7)}
    people = {}

    def person(hospital_id):
        return people.setdefault(hospital_id, {"hospital_id": hospital_id, "settlements": 0,
            "wins": 0, "losses": 0, "active_days": 0, "purchase_count": 0, "yuanbao": 0,
            "empty_requests": 0, "retry_samples": 0, "retry_total": 0, "max_retry": None})

    for row in players:
        person(row["hospital_id"]).update(row)
    active_by_day = defaultdict(set)
    buyers_by_day = defaultdict(set)
    days_by_person = defaultdict(set)
    hourly = [{"hour": i, "wins": 0, "losses": 0} for i in range(24)]
    streaks = Counter()
    anchored = set()
    retries = []
    observed_start = None
    # First observed wins have left-censored failure histories and are excluded.
    for row in sorted(events, key=lambda r: (local_time(r["create_time"], zone), r["id"])):
        at = local_time(row["create_time"], zone)
        day = at.date().isoformat()
        if day not in daily or at > now:
            continue
        hospital_id = row["hospital_id"]
        p = person(hospital_id)
        kind = row["kind"]
        if kind == "EMPTY":
            p["empty_requests"] += 1
            daily[day]["empty_requests"] += 1
            continue
        observed_start = min(observed_start, at) if observed_start else at
        key = "wins" if kind == "WON" else "losses"
        p[key] += 1
        p["settlements"] += 1
        p["last_settled_at"] = at.isoformat()
        daily[day][key] += 1
        daily[day]["settlements"] += 1
        hourly[at.hour][key] += 1
        active_by_day[day].add(hospital_id)
        days_by_person[hospital_id].add(day)
        if kind == "LOST":
            streaks[hospital_id] += 1
        else:
            if hospital_id in anchored:
                retry = streaks[hospital_id]
                retries.append(retry)
                p["retry_samples"] += 1
                p["retry_total"] += retry
                p["max_retry"] = max(p["max_retry"] or 0, retry)
            anchored.add(hospital_id)
            streaks[hospital_id] = 0

    for row in purchases:
        at = local_time(row["create_time"], zone)
        day = at.date().isoformat()
        spent = row["spent"]
        if day not in daily or at > now or spent <= 0:
            continue
        p = person(row["hospital_id"])
        p["purchase_count"] += 1
        p["yuanbao"] += spent
        daily[day]["purchase_count"] += 1
        daily[day]["yuanbao"] += spent
        buyers_by_day[day].add(row["hospital_id"])

    for day, row in daily.items():
        row["active_hospitals"] = len(active_by_day[day])
        row["buyers"] = len(buyers_by_day[day])
    for hospital_id, p in people.items():
        p["active_days"] = len(days_by_person[hospital_id])
        p["win_rate"] = rate(p["wins"], p["settlements"])
        p["per_active_day"] = round(p["settlements"] / p["active_days"], 2) if p["active_days"] else None
        p["avg_retry"] = round(p["retry_total"] / p["retry_samples"], 2) if p["retry_samples"] else None
        p["trailing_losses"] = streaks[hospital_id]

    active = [p for p in people.values() if p["settlements"]]
    buyers = [p for p in people.values() if p["purchase_count"]]
    wins = sum(p["wins"] for p in active)
    losses = sum(p["losses"] for p in active)
    active_days = sum(p["active_days"] for p in active)
    summary = {
        "state_hospitals": len(players),
        "ever_played_hospitals": sum(bool(p.get("last_settled_attempt_id") or p.get("active_attempt_id")) for p in players),
        "active_hospitals": len(active), "settlements": wins + losses, "wins": wins, "losses": losses,
        "win_rate": rate(wins, wins + losses), "buyers": len(buyers),
        "purchase_count": sum(p["purchase_count"] for p in buyers),
        "yuanbao": sum(p["yuanbao"] for p in buyers),
        "repeat_buyers": sum(p["purchase_count"] > 1 for p in buyers),
        "active_buyer_rate": rate(sum(p["purchase_count"] > 0 for p in active), len(active)),
        "per_active_day": round((wins + losses) / active_days, 2) if active_days else None,
        "empty_requests": sum(p["empty_requests"] for p in people.values()),
        "empty_hospitals": sum(p["empty_requests"] > 0 for p in people.values()),
        "retry_samples": len(retries),
        "avg_retry": round(sum(retries) / len(retries), 2) if retries else None,
        "max_retry": max(retries) if retries else None,
        "active_attempts": sum(bool(p.get("active_attempt_id")) for p in players),
    }
    frequency = [{"active_days": i, "hospitals": sum(p["active_days"] == i for p in active)} for i in range(1, 8)]
    progress_counts = Counter()
    for p in players:
        if not (p.get("last_settled_attempt_id") or p.get("active_attempt_id")):
            continue
        level = p.get("highest_cleared_level") or 0
        band = "尚未通关" if level == 0 else "1至5关" if level <= 5 else "6至10关" if level <= 10 else "11至20关" if level <= 20 else "21至50关" if level <= 50 else "51关及以上"
        progress_counts[band] += 1
    progress = [{"band": b, "hospitals": progress_counts[b]} for b in ("尚未通关", "1至5关", "6至10关", "11至20关", "21至50关", "51关及以上")]
    retry_distribution = [{"band": b, "wins": sum(low <= r <= high for r in retries)}
        for b, low, high in (("0次", 0, 0), ("1次", 1, 1), ("2次", 2, 2), ("3至5次", 3, 5), ("6次及以上", 6, float("inf")))]
    ranked = sorted((p for p in people.values() if p["settlements"] or p["purchase_count"] or p["empty_requests"]),
        key=lambda p: (-p["settlements"], -p["purchase_count"], p["hospital_id"]))
    # Attempt UUIDs classify state only and never leave the statistics API.
    public_people = [{k: v for k, v in p.items() if k not in {"active_attempt_id", "last_settled_attempt_id"}} for p in ranked]
    return {
        "generatedAt": now.isoformat(), "zoneId": zone_id, "sourceError": None,
        "windowStart": first_day.isoformat(), "windowEnd": now.date().isoformat(),
        "observedSettlementStart": observed_start.isoformat() if observed_start else None,
        "summary": summary, "dailyTrend": list(daily.values()), "hourly": hourly,
        "frequency": frequency, "progress": progress, "retryDistribution": retry_distribution,
        "levels": levels, "hospitals": public_people[:50], "hospitalCount": len(ranked),
        "buyerHospitals": sorted((p for p in public_people if p["purchase_count"]),
            key=lambda p: (-p["purchase_count"], -p["yuanbao"], p["hospital_id"]))[:50],
        "limitations": [
            "统计最近7天（含今天），今天的数据截至最近一次刷新。有通关或失败记录的医院计为活跃医院。",
            "挑战次数只统计已记录通关或失败的挑战，包含重玩；通关率为通关次数除以挑战次数。记录保留7天，直接退出、尚无结果的挑战和页面访问未完整记录。",
            "日均挑战按每家医院实际游玩的天数计算，未游玩的日期不计入平均值。",
            "平均连败统计同一医院两次通关之间的失败次数，可能跨关卡，也包含重玩。最近7天内第一次通关前的失败、最后一次通关后的失败均不计入。失败记录未保存关卡号，无法统计每关首次通关的重试次数。",
            "购买机会按实际扣除的元宝统计，每次购买将机会补满至5次。记录无法确定每次补了几次机会，也无法推算现金收入。",
            "机会用完后尝试进入会分别计数，同一医院可能尝试多次。未记录商店浏览和购买意向。",
            "最高已通关关卡取自玩家当前进度，仅统计曾开始挑战的医院。曾打开实验室的医院包含只查看页面的医院。",
        ],
    }


def load_bacteria_stats(conn, query_list, now, zone_id):
    local_now = now.astimezone(ZoneInfo(zone_id))
    start = (local_now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    bounds = (start.astimezone(timezone.utc).replace(tzinfo=None), now.astimezone(timezone.utc).replace(tzinfo=None))
    events = query_list(conn, """
        select id, hospital_id, create_time,
               case content when %s then 'WON' when %s then 'LOST' else 'EMPTY' end as kind
        from t_log_right_bottom
        where create_time >= %s and create_time <= %s and content in (%s, %s, %s)
        order by create_time, id
        """, (WIN_MESSAGE, LOSS_MESSAGE, *bounds, WIN_MESSAGE, LOSS_MESSAGE, EMPTY_MESSAGE))
    purchases = query_list(conn, """
        select hospital_id, create_time, old_value - new_value as spent
        from t_log_yuanbao
        where create_time >= %s and create_time <= %s and reason in (%s, %s)
          and old_value > new_value
        """, (*bounds, *REFILL_REASONS))
    players = query_list(conn, """
        select s.hospital_id, h.hospital_name, h.director_name,
               s.highest_cleared_level, s.unlocked_level, s.available_lives,
               s.active_level, s.active_attempt_id, s.last_settled_attempt_id
        from t_bacteria_lab_player_state s left join t_hospitals h on h.id = s.hospital_id
        """)
    levels = query_list(conn, """
        with rewards as (
            select hospital_id, substring(content from '^菌落清除室第([0-9]+)号样本')::integer as level,
                   case when content ~ '^菌落清除室第[0-9]+号样本重玩奖励' then 'REPLAY' else 'FIRST' end as kind
            from t_log_system where create_time >= %s and create_time <= %s
              and content ~ '^菌落清除室第[0-9]+号样本(通关奖励|重玩奖励) 变化：'
        )
        select level, count(*) filter (where kind = 'FIRST') as first_clears,
               count(*) filter (where kind = 'REPLAY') as replay_clears,
               count(distinct hospital_id) as hospitals
        from rewards group by level order by level
        """, bounds)
    return build_bacteria_stats(events, purchases, players, levels, now, zone_id)


def unavailable_bacteria_stats(error, now, zone_id):
    result = build_bacteria_stats([], [], [], [], now, zone_id)
    result["sourceError"] = str(error)
    result["summary"] = {}
    return result
