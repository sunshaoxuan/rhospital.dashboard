import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from flask import Flask, jsonify, render_template, request


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("OPS_DASHBOARD_DATA_DIR", "/data"))
SQLITE_PATH = DATA_DIR / "ops_dashboard.sqlite3"
ZONE_ID = os.getenv("OPS_DASHBOARD_TIME_ZONE", "Asia/Tokyo")
QUERY_TIMEOUT_SECONDS = int(os.getenv("OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS", "10"))
STAT_TABLE_PAGE_SIZES = {20, 50, 100}
STAT_TABLE_TABS = {"items", "money", "yuanbao", "prestige", "guild", "registrants"}
SPECIAL_CLINIC_ZONE_ID = "Asia/Shanghai"
SPECIAL_CLINIC_ITEM_NAMES = {
    1222: "广告牌I",
    1327: "聪明胶囊",
    1329: "胶囊",
    1330: "可爱胶囊",
    1662: "很加快预约",
    1664: "急速研究",
    1665: "加快预约",
    1666: "降低成本",
    1667: "提高效率",
    1671: "专家加班",
    1791: "补签卡",
    1792: "特需门诊票",
}

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))


def now_in_zone():
    return datetime.now(ZoneInfo(ZONE_ID))


def require_config(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required env: {name}")
    return value


@contextmanager
def prod_connection():
    with psycopg.connect(
        require_config("PROD_DB_URL"),
        user=require_config("PROD_DB_USERNAME"),
        password=require_config("PROD_DB_PASSWORD"),
        connect_timeout=QUERY_TIMEOUT_SECONDS,
    ) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set default_transaction_read_only = on")
            cur.execute(f"set statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'")
            cur.execute("set idle_in_transaction_session_timeout = '10s'")
        yield conn


@contextmanager
def sqlite_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def ensure_snapshot_table():
    with sqlite_connection() as conn:
        conn.execute(
            """
            create table if not exists daily_snapshot (
                day text primary key,
                generated_at text not null,
                online_now_accounts integer not null default 0,
                max_online_now_accounts integer not null default 0,
                active_today_accounts integer not null default 0,
                registrations_today integer not null default 0,
                recharge_cny_today real not null default 0,
                recharge_yuanbao_today integer not null default 0,
                recharge_orders_today integer not null default 0,
                skin_owner_accounts integer not null default 0,
                skin_equipped_accounts integer not null default 0,
                skin_free_accounts integer not null default 0,
                skin_purchase_log_accounts integer not null default 0,
                skin_paid_confirmed_accounts integer not null default 0
            )
            """
        )


def decimal_to_float(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def rows_to_dicts(cursor):
    columns = [desc.name for desc in cursor.description]
    return [
        {column: decimal_to_float(value) for column, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def query_one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        row = cur.fetchone()
        if row is None:
            return {}
        return {column: decimal_to_float(value) for column, value in zip(columns, row)}


def query_list(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return rows_to_dicts(cur)


def column_exists(conn, table_name, column_name):
    row = query_one(
        conn,
        """
        select exists (
            select 1
            from information_schema.columns
            where table_name = %s
              and column_name = %s
        ) as exists
        """,
        (table_name, column_name),
    )
    return bool(row.get("exists"))


def special_clinic_depleted_at_select(has_depleted_at):
    if has_depleted_at:
        return (
            "coalesce(to_char(min(c.depleted_at) at time zone 'UTC' at time zone %s, 'MM-DD HH24:MI'), '') as depleted_at",
            (SPECIAL_CLINIC_ZONE_ID,),
        )
    return "'' as depleted_at", ()


def major_amount(value):
    return round(float(value or 0) / 100, 2)


def load_summary(conn):
    row = query_one(
        conn,
        """
        with owned as (
            select h.id as hospital_id, h.director_id, coalesce((b.items ->> '1018')::int, 0) as item_count
            from t_backpack b
            join t_hospitals h on h.id = b.hospital_id
            where jsonb_exists(b.items, '1018') and coalesce((b.items ->> '1018')::int, 0) > 0
        ), equipped as (
            select h.id as hospital_id, h.director_id
            from t_hospitals h
            where h.props ->> 'equippedSkin' = 'hospital-fifa2026'
        ), green_effect_used as (
            select distinct h.director_id
            from t_log_right_bottom r
            join t_hospitals h on h.id = r.hospital_id
            where r.content = '成功使用【绿茵盛典【效】(期间限定)】物品。'
        ), free_log as (
            select distinct h.director_id, r.hospital_id
            from t_log_right_bottom r
            join t_hospitals h on h.id = r.hospital_id
            where r.content = '成功领取1个【荣耀绿茵(期间限定)】'
        ), paid_log as (
            select distinct h.director_id, r.hospital_id
            from t_log_right_bottom r
            join t_hospitals h on h.id = r.hospital_id
            where r.content = '成功购买1个【荣耀绿茵(期间限定)】'
        ), paid_yuanbao as (
            select distinct h.director_id, l.hospital_id
            from t_log_yuanbao l
            join t_hospitals h on h.id = l.hospital_id
            where l.reason = '商店购买: 荣耀绿茵(期间限定) x 1'
        ), recharge_today as (
            select 'stripe' as source, amount, currency, yuanbao_amount
            from t_payment_orders
            where status = 'COMPLETED'
              and (update_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date
            union all
            select 'paddle' as source, amount, currency, yuanbao_amount
            from t_paddle_payment_orders
            where status = 'COMPLETED'
              and (update_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date
            union all
            select 'steam' as source, amount, currency, yuanbao_amount
            from t_steam_payment_orders
            where status = 'COMPLETED' and delivered is true
              and (update_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date
        )
        select
            (select count(distinct director_id) from owned) as skin_owner_accounts,
            (select count(*) from owned) as skin_owner_hospitals,
            (select count(distinct director_id) from equipped) as skin_equipped_accounts,
            (select count(*)
               from (select distinct director_id from equipped) e
               join green_effect_used g on g.director_id = e.director_id) as green_combo_equipped_accounts,
            (select count(distinct director_id) from free_log) as skin_free_accounts,
            (select count(distinct director_id) from paid_log) as skin_purchase_log_accounts,
            (select count(distinct director_id) from paid_yuanbao) as skin_paid_confirmed_accounts,
            (select count(distinct director_id)
               from t_hospitals
              where last_heartbeat_time is not null
                and last_heartbeat_time::timestamptz >= now() - interval '10 minutes') as online_now_accounts,
            (select count(distinct h.director_id)
               from t_log_right_bottom r
               join t_hospitals h on h.id = r.hospital_id
              where r.content like '%%院长 驾到主持工作%%'
                and (r.create_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date) as active_today_accounts,
            (select count(*)
               from t_directors
              where (create_time at time zone 'UTC' at time zone %s)::date = (now() at time zone %s)::date) as registrations_today,
            (select coalesce(sum(amount), 0) from recharge_today where lower(currency) = 'cny') as recharge_cny_minor_today,
            (select coalesce(sum(yuanbao_amount), 0) from recharge_today) as recharge_yuanbao_today,
            (select count(*) from recharge_today) as recharge_orders_today,
            (select coalesce(sum(amount), 0) from recharge_today where source = 'stripe' and lower(currency) = 'cny') as stripe_recharge_cny_minor_today,
            (select coalesce(sum(yuanbao_amount), 0) from recharge_today where source = 'stripe') as stripe_recharge_yuanbao_today,
            (select count(*) from recharge_today where source = 'stripe') as stripe_recharge_orders_today,
            (select coalesce(sum(amount), 0) from recharge_today where source = 'steam' and lower(currency) = 'cny') as steam_recharge_cny_minor_today,
            (select coalesce(sum(yuanbao_amount), 0) from recharge_today where source = 'steam') as steam_recharge_yuanbao_today,
            (select count(*) from recharge_today where source = 'steam') as steam_recharge_orders_today
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID, ZONE_ID),
    )
    row["recharge_cny_today"] = major_amount(row.pop("recharge_cny_minor_today", 0))
    row["stripe_recharge_cny_today"] = major_amount(row.pop("stripe_recharge_cny_minor_today", 0))
    row["steam_recharge_cny_today"] = major_amount(row.pop("steam_recharge_cny_minor_today", 0))
    return row


def load_stats_from_prod():
    with prod_connection() as conn:
        summary = load_summary(conn)
        stats = {
            "generatedAt": now_in_zone().isoformat(),
            "zoneId": ZONE_ID,
            "note": "在线历史来自医院最后心跳时间；最近14日日活来自进入游戏日志。",
            "summary": summary,
            "onlineBuckets": query_list(
                conn,
                """
                select to_char(
                           to_timestamp(floor(extract(epoch from last_heartbeat_time::timestamptz) / 600) * 600)
                               at time zone %s,
                           'YYYY-MM-DD HH24:MI') as label,
                       count(distinct director_id) as count
                from t_hospitals
                where last_heartbeat_time is not null
                  and last_heartbeat_time::timestamptz >= now() - interval '24 hours'
                group by label
                order by label
                """,
                (ZONE_ID,),
            ),
            "dailyRegistrations": query_list(
                conn,
                """
                select ((create_time at time zone 'UTC' at time zone %s)::date)::text as day,
                       count(*) as count
                from t_directors
                where (create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
                group by day
                order by day
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID),
            ),
            "dailyActive": query_list(
                conn,
                """
                select ((r.create_time at time zone 'UTC' at time zone %s)::date)::text as day,
                       count(distinct h.director_id) as count
                from t_log_right_bottom r
                join t_hospitals h on h.id = r.hospital_id
                where r.content like '%%院长 驾到主持工作%%'
                  and (r.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
                group by day
                order by day
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID),
            ),
            "dailyRecharge": load_daily_recharge(conn),
            "hourlyYuanbaoSpending": load_hourly_yuanbao_spending(conn),
            "itemPurchases": load_item_purchases(conn),
            "itemUsages": load_item_usages(conn),
        }
        return stats


def load_daily_recharge(conn):
    rows = query_list(
        conn,
        """
        with orders as (
            select update_time, amount, currency, yuanbao_amount from t_payment_orders where status = 'COMPLETED'
            union all
            select update_time, amount, currency, yuanbao_amount from t_paddle_payment_orders where status = 'COMPLETED'
            union all
            select update_time, amount, currency, yuanbao_amount from t_steam_payment_orders where status = 'COMPLETED' and delivered is true
        )
        select ((update_time at time zone 'UTC' at time zone %s)::date)::text as day,
               lower(coalesce(currency, 'unknown')) as currency,
               count(*) as orders,
               coalesce(sum(amount), 0) as amount_minor,
               coalesce(sum(yuanbao_amount), 0) as yuanbao
        from orders
        where (update_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        group by day, lower(coalesce(currency, 'unknown'))
        order by day, currency
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID),
    )
    for row in rows:
        row["amount"] = major_amount(row.pop("amount_minor", 0))
    return rows


def load_hourly_yuanbao_spending(conn):
    return query_list(
        conn,
        """
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s),
                       'MM-DD HH24:00') as label,
               count(*) as event_count,
               coalesce(sum(greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0)), 0) as yuanbao_spent
        from t_log_yuanbao
        where old_value is not null
          and new_value is not null
          and old_value > new_value
          and (create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        group by label
        order by min(create_time)
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID),
    )


def purchase_category_case() -> str:
    background_names = (
        "几何迷宫【效】", "空间站【效】", "城镇绿地", "海岛风光", "休假岛", "荒漠高原",
        "冰天雪地", "鹰愁涧(马年限定)", "绿水青山", "黄沙荒原", "流波地带", "赤岩绝壁",
        "禁忌遗都", "极地晶域", "迷雾浮岛", "幽冥堡垒",
    )
    quoted_backgrounds = ", ".join(f"'{name}'" for name in background_names)
    return f"""
        case
            when item_name like '%%荣耀绿茵%%' then 'skin'
            when item_name like '%%绿茵盛典%%' or item_name in ({quoted_backgrounds}) then 'background'
            else 'item'
        end
    """


def load_purchase_insights(conn):
    categories = ("item", "background", "skin")
    return {
        category: {
            "top": load_purchase_top(conn, category),
            "buyers": load_purchase_buyers(conn, category),
        }
        for category in categories
    }


def parsed_purchase_cte() -> str:
    category_case = purchase_category_case()
    return f"""
        parsed as (
            select l.hospital_id, h.hospital_name, h.director_name,
                   (regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                   ((regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                   greatest(coalesce(l.old_value, 0) - coalesce(l.new_value, 0), 0) as yuanbao_cost,
                   l.create_time
            from t_log_yuanbao l
            left join t_hospitals h on h.id = l.hospital_id
            where l.reason ~ '^商店购买: .+ x [0-9]+$'
              and (l.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        ), categorized as (
            select *, {category_case} as category
            from parsed
            where item_name is not null
        )
    """


def load_purchase_top(conn, category):
    return query_list(
        conn,
        f"""
        with {parsed_purchase_cte()}
        select item_name, count(*) as event_count, count(distinct hospital_id) as hospital_count,
               coalesce(sum(quantity), 0) as quantity, coalesce(sum(yuanbao_cost), 0) as yuanbao
        from categorized
        where category = %s
        group by item_name
        order by event_count desc, quantity desc, yuanbao desc, item_name
        limit 10
        """,
        (ZONE_ID, ZONE_ID, category),
    )


def load_purchase_buyers(conn, category):
    return query_list(
        conn,
        f"""
        with {parsed_purchase_cte()}
        select to_char((create_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as purchase_time,
               hospital_id, coalesce(hospital_name, '') as hospital_name, coalesce(director_name, '') as director_name,
               item_name, quantity, yuanbao_cost as yuanbao
        from categorized
        where category = %s
        order by create_time desc, hospital_id desc, item_name
        limit 20
        """,
        (ZONE_ID, ZONE_ID, ZONE_ID, category),
    )


def load_item_purchases(conn):
    return query_list(
        conn,
        """
        with parsed as (
            select (regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                   ((regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                   greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0) as yuanbao_cost,
                   hospital_id
            from t_log_yuanbao
            where reason ~ '^商店购买: .+ x [0-9]+$'
              and (create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        )
        select item_name, count(*) as event_count, count(distinct hospital_id) as hospital_count,
               coalesce(sum(quantity), 0) as quantity, coalesce(sum(yuanbao_cost), 0) as yuanbao
        from parsed
        where item_name is not null
        group by item_name
        order by quantity desc, event_count desc, item_name
        limit 12
        """,
        (ZONE_ID, ZONE_ID),
    )


def load_item_usages(conn):
    return query_list(conn, item_usage_sql("summary"), (ZONE_ID, ZONE_ID))


def item_usage_sql(mode):
    select_fields = (
        "item_name, count(*) as event_count, count(distinct hospital_id) as hospital_count, "
        "coalesce(sum(quantity), 0) as quantity, 0::bigint as yuanbao"
        if mode == "summary"
        else "hospital_id, coalesce(hospital_name, '') as hospital_name, coalesce(director_name, '') as director_name, "
        "count(*) as event_count, coalesce(sum(quantity), 0) as quantity, 0::bigint as yuanbao, "
        "to_char((max(create_time) at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as last_time"
    )
    group_by = "item_name" if mode == "summary" else "hospital_id, hospital_name, director_name"
    order_by = "quantity desc, event_count desc, item_name" if mode == "summary" else "quantity desc, event_count desc, max(create_time) desc, hospital_id desc"
    limit = "limit 12" if mode == "summary" else "limit 200"
    filter_item = "" if mode == "summary" else "and item_name = %s"
    return f"""
        with parsed as (
            select r.hospital_id, h.hospital_name, h.director_name,
                   case
                       when r.content ~ '^成功批量使用【.+】物品[0-9]+次' then regexp_replace(r.content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\1')
                       when r.content ~ '^成功使用【.+】物品' then regexp_replace(r.content, '^成功使用【(.+)】物品.*$', '\\1')
                       when r.content ~ '^批量打开【.+】[0-9]+次获得' then regexp_replace(r.content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\1')
                       when r.content ~ '^打开【.+】获得' then regexp_replace(r.content, '^打开【(.+)】获得.*$', '\\1')
                       else null
                   end as item_name,
                   case
                       when r.content ~ '^成功批量使用【.+】物品[0-9]+次' then (regexp_replace(r.content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\2'))::bigint
                       when r.content ~ '^批量打开【.+】[0-9]+次获得' then (regexp_replace(r.content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\2'))::bigint
                       else 1
                   end as quantity,
                   r.create_time
            from t_log_right_bottom r
            left join t_hospitals h on h.id = r.hospital_id
            where (r.content like '成功使用【%%' or r.content like '成功批量使用【%%' or r.content like '打开【%%' or r.content like '批量打开【%%')
              and (r.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
        )
        select {select_fields}
        from parsed
        where item_name is not null {filter_item}
        group by {group_by}
        order by {order_by}
        {limit}
    """


def parse_page_args():
    tab = request.args.get("tab", "items").strip().lower()
    if tab not in STAT_TABLE_TABS:
        tab = "items"
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("pageSize", "20"))
    except ValueError:
        page_size = 20
    if page_size not in STAT_TABLE_PAGE_SIZES:
        page_size = 20
    return tab, page, page_size


def paged_query(conn, count_sql, rows_sql, params, page, page_size, count_params=()):
    total = int(query_one(conn, count_sql, count_params).get("total", 0))
    offset = (page - 1) * page_size
    rows = query_list(conn, rows_sql, (*params, page_size, offset))
    return total, rows


def load_stat_table(tab, page, page_size):
    with prod_connection() as conn:
        if tab == "items":
            payload = load_item_stat_table(conn, page, page_size)
        elif tab == "money":
            payload = load_hospital_value_table(conn, "money", "金钱", page, page_size)
        elif tab == "yuanbao":
            payload = load_hospital_value_table(conn, "ingot", "元宝", page, page_size)
        elif tab == "prestige":
            payload = load_hospital_value_table(conn, "prestige", "声望", page, page_size)
        elif tab == "guild":
            payload = load_guild_table(conn, page, page_size)
        else:
            payload = load_registrant_table(conn, page, page_size)
    payload.update({"tab": tab, "page": page, "pageSize": page_size})
    return payload


def load_item_stat_table(conn, page, page_size):
    count_sql = """
        with item_names as (
            select (regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name
            from t_log_yuanbao
            where reason ~ '^商店购买: .+ x [0-9]+$'
            union
            select case
                       when content ~ '^成功批量使用【.+】物品[0-9]+次' then regexp_replace(content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\1')
                       when content ~ '^成功使用【.+】物品' then regexp_replace(content, '^成功使用【(.+)】物品.*$', '\\1')
                       when content ~ '^批量打开【.+】[0-9]+次获得' then regexp_replace(content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\1')
                       when content ~ '^打开【.+】获得' then regexp_replace(content, '^打开【(.+)】获得.*$', '\\1')
                       else null
                   end as item_name
            from t_log_right_bottom
            where content like '成功使用【%%' or content like '成功批量使用【%%' or content like '打开【%%' or content like '批量打开【%%'
        )
        select count(*) as total
        from item_names
        where item_name is not null
    """
    rows_sql = """
        with purchases as (
            select (regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                   ((regexp_match(reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                   greatest(coalesce(old_value, 0) - coalesce(new_value, 0), 0) as yuanbao_cost
            from t_log_yuanbao
            where reason ~ '^商店购买: .+ x [0-9]+$'
        ), purchase_summary as (
            select item_name, count(*) as purchase_events, coalesce(sum(quantity), 0) as purchased_quantity,
                   coalesce(sum(yuanbao_cost), 0) as yuanbao_used
            from purchases
            where item_name is not null
            group by item_name
        ), usages as (
            select case
                       when content ~ '^成功批量使用【.+】物品[0-9]+次' then regexp_replace(content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\1')
                       when content ~ '^成功使用【.+】物品' then regexp_replace(content, '^成功使用【(.+)】物品.*$', '\\1')
                       when content ~ '^批量打开【.+】[0-9]+次获得' then regexp_replace(content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\1')
                       when content ~ '^打开【.+】获得' then regexp_replace(content, '^打开【(.+)】获得.*$', '\\1')
                       else null
                   end as item_name,
                   case
                       when content ~ '^成功批量使用【.+】物品[0-9]+次' then (regexp_replace(content, '^成功批量使用【(.+)】物品([0-9]+)次.*$', '\\2'))::bigint
                       when content ~ '^批量打开【.+】[0-9]+次获得' then (regexp_replace(content, '^批量打开【(.+)】([0-9]+)次获得.*$', '\\2'))::bigint
                       else 1
                   end as quantity
            from t_log_right_bottom
            where content like '成功使用【%%' or content like '成功批量使用【%%' or content like '打开【%%' or content like '批量打开【%%'
        ), use_summary as (
            select item_name, count(*) as use_events, coalesce(sum(quantity), 0) as consumed_quantity
            from usages
            where item_name is not null
            group by item_name
        ), combined as (
            select coalesce(p.item_name, u.item_name) as item_name,
                   coalesce(p.purchased_quantity, 0) as purchased_quantity,
                   coalesce(u.consumed_quantity, 0) as consumed_quantity,
                   coalesce(p.yuanbao_used, 0) as yuanbao_used,
                   coalesce(p.purchase_events, 0) as purchase_events,
                   coalesce(u.use_events, 0) as use_events
            from purchase_summary p
            full outer join use_summary u on u.item_name = p.item_name
        )
        select item_name, purchased_quantity, consumed_quantity, yuanbao_used, purchase_events, use_events
        from combined
        order by purchased_quantity desc, consumed_quantity desc, yuanbao_used desc, item_name
        limit %s offset %s
    """
    total, rows = paged_query(conn, count_sql, rows_sql, (), page, page_size)
    return {
        "title": "道具统计",
        "description": "按所有日志聚合商品购买数量、消耗数量和元宝使用数量。",
        "columns": [
            {"key": "item_name", "label": "道具"},
            {"key": "purchased_quantity", "label": "购买数量", "type": "number"},
            {"key": "consumed_quantity", "label": "消耗数量", "type": "number"},
            {"key": "yuanbao_used", "label": "元宝使用数量", "type": "number"},
            {"key": "purchase_events", "label": "购买日志", "type": "number"},
            {"key": "use_events", "label": "消耗日志", "type": "number"},
        ],
        "total": total,
        "rows": rows,
    }


def load_hospital_value_table(conn, column, label, page, page_size):
    count_sql = "select count(*) as total from t_hospitals"
    rows_sql = f"""
        select id as hospital_id, coalesce(hospital_name, '') as hospital_name,
               coalesce(director_name, '') as director_name, coalesce({column}, 0) as value,
               to_char((update_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as update_time
        from t_hospitals
        order by coalesce({column}, 0) desc, update_time desc, id desc
        limit %s offset %s
    """
    total, rows = paged_query(conn, count_sql, rows_sql, (ZONE_ID,), page, page_size)
    return {
        "title": f"{label}排行",
        "description": f"按医院当前{label}从多到少排序。",
        "columns": [
            {"key": "hospital_id", "label": "医院 ID", "type": "number"},
            {"key": "hospital_name", "label": "医院名"},
            {"key": "director_name", "label": "院长名"},
            {"key": "value", "label": label, "type": "number"},
            {"key": "update_time", "label": "更新时间"},
        ],
        "total": total,
        "rows": rows,
    }


def load_guild_table(conn, page, page_size):
    count_sql = "select count(*) as total from t_guild"
    rows_sql = """
        select g.id as guild_id, coalesce(g.name, '') as guild_name, coalesce(g.status, '') as status,
               coalesce(g.level, 0) as level, coalesce(g.build_points, 0) as build_points,
               coalesce(g.ingot_pool, 0) as ingot_pool, count(m.id) as members,
               coalesce(sum(m.donation_total), 0) as donation_total,
               to_char((g.create_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as create_time
        from t_guild g
        left join t_guild_member m on m.guild_id = g.id
        group by g.id, g.name, g.status, g.level, g.build_points, g.ingot_pool, g.create_time
        order by coalesce(g.level, 0) desc, coalesce(g.build_points, 0) desc, coalesce(g.ingot_pool, 0) desc, g.id desc
        limit %s offset %s
    """
    total, rows = paged_query(conn, count_sql, rows_sql, (ZONE_ID,), page, page_size)
    return {
        "title": "公会统计",
        "description": "按等级、建设值、元宝池从高到低排序。",
        "columns": [
            {"key": "guild_id", "label": "公会 ID", "type": "number"},
            {"key": "guild_name", "label": "公会名"},
            {"key": "status", "label": "状态"},
            {"key": "level", "label": "等级", "type": "number"},
            {"key": "build_points", "label": "建设值", "type": "number"},
            {"key": "ingot_pool", "label": "元宝池", "type": "number"},
            {"key": "members", "label": "成员数", "type": "number"},
            {"key": "donation_total", "label": "累计贡献", "type": "number"},
            {"key": "create_time", "label": "创建时间"},
        ],
        "total": total,
        "rows": rows,
    }


def load_registrant_table(conn, page, page_size):
    count_sql = "select count(*) as total from t_directors"
    rows_sql = """
        select d.id as director_id, coalesce(d.username, '') as username, coalesce(d.auth_provider, '') as auth_provider,
               to_char((d.create_time at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as create_time,
               count(h.id) as hospital_count,
               coalesce(max(h.hospital_name), '') as hospital_name,
               coalesce(max(h.director_name), '') as director_name
        from t_directors d
        left join t_hospitals h on h.director_id = d.id
        group by d.id, d.username, d.auth_provider, d.create_time
        order by d.create_time desc, d.id desc
        limit %s offset %s
    """
    total, rows = paged_query(conn, count_sql, rows_sql, (ZONE_ID,), page, page_size)
    return {
        "title": "注册者",
        "description": "按账号注册时间从新到旧排序。",
        "columns": [
            {"key": "director_id", "label": "账号 ID", "type": "number"},
            {"key": "username", "label": "用户名"},
            {"key": "auth_provider", "label": "来源"},
            {"key": "create_time", "label": "注册时间"},
            {"key": "hospital_count", "label": "医院数", "type": "number"},
            {"key": "hospital_name", "label": "医院名"},
            {"key": "director_name", "label": "院长名"},
        ],
        "total": total,
        "rows": rows,
    }


def record_snapshot(stats):
    ensure_snapshot_table()
    summary = stats["summary"]
    day = datetime.fromisoformat(stats["generatedAt"]).astimezone(ZoneInfo(ZONE_ID)).date().isoformat()
    with sqlite_connection() as conn:
        old = conn.execute("select max_online_now_accounts from daily_snapshot where day = ?", (day,)).fetchone()
        max_online = max(int(old["max_online_now_accounts"]) if old else 0, int(summary["online_now_accounts"]))
        conn.execute(
            """
            insert into daily_snapshot (
                day, generated_at, online_now_accounts, max_online_now_accounts, active_today_accounts,
                registrations_today, recharge_cny_today, recharge_yuanbao_today, recharge_orders_today,
                skin_owner_accounts, skin_equipped_accounts, skin_free_accounts,
                skin_purchase_log_accounts, skin_paid_confirmed_accounts
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(day) do update set
                generated_at = excluded.generated_at,
                online_now_accounts = excluded.online_now_accounts,
                max_online_now_accounts = excluded.max_online_now_accounts,
                active_today_accounts = excluded.active_today_accounts,
                registrations_today = excluded.registrations_today,
                recharge_cny_today = excluded.recharge_cny_today,
                recharge_yuanbao_today = excluded.recharge_yuanbao_today,
                recharge_orders_today = excluded.recharge_orders_today,
                skin_owner_accounts = excluded.skin_owner_accounts,
                skin_equipped_accounts = excluded.skin_equipped_accounts,
                skin_free_accounts = excluded.skin_free_accounts,
                skin_purchase_log_accounts = excluded.skin_purchase_log_accounts,
                skin_paid_confirmed_accounts = excluded.skin_paid_confirmed_accounts
            """,
            (
                day,
                stats["generatedAt"],
                summary["online_now_accounts"],
                max_online,
                summary["active_today_accounts"],
                summary["registrations_today"],
                summary["recharge_cny_today"],
                summary["recharge_yuanbao_today"],
                summary["recharge_orders_today"],
                summary["skin_owner_accounts"],
                summary["skin_equipped_accounts"],
                summary["skin_free_accounts"],
                summary["skin_purchase_log_accounts"],
                summary["skin_paid_confirmed_accounts"],
            ),
        )


def merge_snapshot_history(stats):
    ensure_snapshot_table()
    cutoff = (now_in_zone().date() - timedelta(days=13)).isoformat()
    with sqlite_connection() as conn:
        rows = conn.execute("select * from daily_snapshot where day >= ? order by day", (cutoff,)).fetchall()
    if not rows:
        return stats
    active = {row["day"]: row["active_today_accounts"] for row in rows}
    registrations = {row["day"]: row["registrations_today"] for row in rows}
    recharge = {
        (row["day"], "cny"): {
            "day": row["day"],
            "currency": "cny",
            "orders": row["recharge_orders_today"],
            "amount": row["recharge_cny_today"],
            "yuanbao": row["recharge_yuanbao_today"],
        }
        for row in rows
    }
    for row in stats["dailyActive"]:
        active[row["day"]] = row["count"]
    for row in stats["dailyRegistrations"]:
        registrations[row["day"]] = row["count"]
    for row in stats["dailyRecharge"]:
        recharge[(row["day"], row["currency"])] = row
    stats["dailyActive"] = [{"day": day, "count": count} for day, count in sorted(active.items())]
    stats["dailyRegistrations"] = [{"day": day, "count": count} for day, count in sorted(registrations.items())]
    stats["dailyRecharge"] = [row for _, row in sorted(recharge.items())]
    return stats


def load_special_clinic_stats_from_prod():
    with prod_connection() as conn:
        hourly_summary = load_special_clinic_hourly_summary(conn)
        tier_distribution = load_special_clinic_tier_distribution(conn)
        patient_distribution = load_special_clinic_patient_distribution(conn)
        reward_items = load_special_clinic_reward_items(conn)
        resource_rewards = load_special_clinic_resource_rewards(conn)
        ticket_flows = load_special_clinic_ticket_flows(conn)
        weekly_cabinet = load_special_clinic_weekly_cabinet(conn)
        hospital_daily = load_special_clinic_hospital_daily(conn)
        audit_checks = load_special_clinic_audit_checks(conn)
        summary = load_special_clinic_summary(conn)
        if weekly_cabinet:
            latest_week = weekly_cabinet[0]
            summary.update({
                "latest_clinic_date": latest_week.get("clinic_date", ""),
                "cabinet_status": latest_week.get("status", ""),
                "initial_total": latest_week.get("initial_total", 0),
                "remaining_total": latest_week.get("remaining_total", 0),
                "total_diagnoses": latest_week.get("total_diagnoses", 0),
                "empty_attempt_count": latest_week.get("empty_attempt_count", 0),
                "critical_admitted_count": latest_week.get("critical_admitted_count", 0),
                "supply_total": latest_week.get("supply_total", 0),
                "consume_rate": latest_week.get("consume_rate", 0),
            })
        return {
            "generatedAt": datetime.now(ZoneInfo(SPECIAL_CLINIC_ZONE_ID)).isoformat(),
            "zoneId": SPECIAL_CLINIC_ZONE_ID,
            "summary": summary,
            "hourlySummary": hourly_summary,
            "tierDistribution": tier_distribution,
            "patientDistribution": patient_distribution,
            "rewardItems": reward_items,
            "resourceRewards": resource_rewards,
            "ticketFlows": ticket_flows,
            "weeklyCabinet": weekly_cabinet,
            "dailyCabinet": weekly_cabinet,
            "hospitalDaily": hospital_daily,
            "auditChecks": audit_checks,
        }


def load_special_clinic_summary(conn):
    row = query_one(
        conn,
        """
        with r as (
            select *
            from t_special_clinic_patient_record
            where clinic_date >= ((now() at time zone %s)::date - 13)
        ), t as (
            select *
            from t_special_clinic_ticket_log
            where clinic_date >= ((now() at time zone %s)::date - 13)
        ), c as (
            select *
            from t_special_clinic_cabinet
            order by clinic_date desc, id desc
            limit 1
        )
        select
            (select count(*) from r) as diagnosis_count,
            (select count(distinct hospital_id) from r) as active_hospital_count,
            (select count(*) from r where ticket_type_used = 'PAID') as paid_diagnosis_count,
            (select coalesce(sum(greatest(paid_delta, 0)), 0) from t where change_type = 'PURCHASE') as paid_ticket_purchased,
            (select coalesce(sum(ingot_cost), 0) from t where change_type = 'PURCHASE') as ingot_cost,
            (select coalesce(sum(temporary_patients), 0) from r) as temporary_patients,
            (select coalesce(sum(ingot_reward), 0) from r) as ingot_reward,
            (select coalesce(sum(money_reward), 0) from r) as money_reward,
            (select coalesce(sum(prestige_reward), 0) from r) as prestige_reward,
            (select coalesce(sum(glory_reward), 0) from r) as glory_reward,
            coalesce((select clinic_date::text from c), '') as latest_clinic_date,
            coalesce((select status from c), '') as cabinet_status,
            coalesce((select initial_total from c), 0) as initial_total,
            coalesce((select remaining_total from c), 0) as remaining_total,
            coalesce((select total_diagnoses from c), 0) as total_diagnoses,
            coalesce((select empty_attempt_count from c), 0) as empty_attempt_count,
            coalesce((select critical_admitted_count from c), 0) as critical_admitted_count
        """,
        (SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID),
    )
    return add_special_clinic_supply_metrics(row)


def add_special_clinic_supply_metrics(row):
    initial_total = int(row.get("initial_total") or 0)
    total_diagnoses = int(row.get("total_diagnoses") or 0)
    supply_total = max(initial_total, total_diagnoses)
    row["supply_total"] = supply_total
    row["consume_rate"] = round(total_diagnoses * 100 / supply_total, 2) if supply_total else 0
    return row


def load_special_clinic_hourly_summary(conn):
    return query_list(
        conn,
        """
        with record_hourly as (
            select date_trunc('hour', create_time at time zone 'UTC' at time zone %s) as hour_bucket,
                   count(*) as diagnosis_count,
                   count(distinct hospital_id) as active_hospital_count,
                   count(*) filter (where ticket_type_used = 'PAID') as paid_diagnosis_count,
                   coalesce(sum((reward_items ->> '1792')::int), 0) as reward_ticket_count,
                   coalesce(sum(temporary_patients), 0) as temporary_patients,
                   coalesce(sum(ingot_reward), 0) as ingot_reward,
                   coalesce(sum(money_reward), 0) as money_reward,
                   coalesce(sum(prestige_reward), 0) as prestige_reward,
                   coalesce(sum(glory_reward), 0) as glory_reward
            from t_special_clinic_patient_record
            where clinic_date >= ((now() at time zone %s)::date - 13)
            group by hour_bucket
        ), ticket_hourly as (
            select date_trunc('hour', create_time at time zone 'UTC' at time zone %s) as hour_bucket,
                   coalesce(sum(abs(appointment_delta)) filter (where appointment_delta < 0), 0) as appointment_ticket_consume,
                   coalesce(sum(abs(gifted_delta)) filter (where gifted_delta < 0), 0) as gifted_ticket_consume,
                   coalesce(sum(abs(paid_delta)) filter (where paid_delta < 0), 0) as paid_ticket_consume,
                   coalesce(sum(paid_delta) filter (where paid_delta > 0 and change_type = 'PURCHASE'), 0) as paid_ticket_purchased,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost
            from t_special_clinic_ticket_log
            where clinic_date >= ((now() at time zone %s)::date - 13)
            group by hour_bucket
        )
        select to_char(coalesce(r.hour_bucket, t.hour_bucket), 'MM-DD HH24:00') as label,
               coalesce(r.diagnosis_count, 0) as diagnosis_count,
               coalesce(r.active_hospital_count, 0) as active_hospital_count,
               coalesce(r.paid_diagnosis_count, 0) as paid_diagnosis_count,
               coalesce(t.appointment_ticket_consume, 0) as appointment_ticket_consume,
               coalesce(t.gifted_ticket_consume, 0) as gifted_ticket_consume,
               coalesce(t.paid_ticket_consume, 0) as paid_ticket_consume,
               coalesce(t.paid_ticket_purchased, 0) as paid_ticket_purchased,
               coalesce(t.ingot_cost, 0) as ingot_cost,
               coalesce(r.reward_ticket_count, 0) as reward_ticket_count,
               coalesce(r.temporary_patients, 0) as temporary_patients,
               coalesce(r.ingot_reward, 0) as ingot_reward,
               coalesce(r.money_reward, 0) as money_reward,
               coalesce(r.prestige_reward, 0) as prestige_reward,
               coalesce(r.glory_reward, 0) as glory_reward
        from record_hourly r
        full outer join ticket_hourly t on t.hour_bucket = r.hour_bucket
        order by coalesce(r.hour_bucket, t.hour_bucket)
        """,
        (SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID),
    )


def load_special_clinic_tier_distribution(conn):
    return query_list(
        conn,
        """
        select coalesce(tier, 'UNKNOWN') as tier,
               count(*) as diagnosis_count,
               count(distinct hospital_id) as hospital_count,
               count(*) filter (where ticket_type_used = 'PAID') as paid_ticket_count
        from t_special_clinic_patient_record
        where clinic_date >= ((now() at time zone %s)::date - 13)
        group by coalesce(tier, 'UNKNOWN')
        order by diagnosis_count desc, tier
        """,
        (SPECIAL_CLINIC_ZONE_ID,),
    )


def load_special_clinic_patient_distribution(conn):
    return query_list(
        conn,
        """
        select coalesce(patient_code, '') as patient_code,
               coalesce(patient_name, '') as patient_name,
               coalesce(tier, 'UNKNOWN') as tier,
               count(*) as diagnosis_count,
               count(distinct hospital_id) as hospital_count,
               count(*) filter (where ticket_type_used = 'PAID') as paid_ticket_count
        from t_special_clinic_patient_record
        where clinic_date >= ((now() at time zone %s)::date - 13)
        group by patient_code, patient_name, tier
        order by diagnosis_count desc, paid_ticket_count desc, patient_name
        limit 40
        """,
        (SPECIAL_CLINIC_ZONE_ID,),
    )


def load_special_clinic_reward_items(conn):
    rows = query_list(
        conn,
        """
        select e.key::bigint as item_id,
               coalesce(sum(e.value::int), 0) as item_count,
               count(*) as record_count,
               count(distinct r.hospital_id) as hospital_count
        from t_special_clinic_patient_record r
        cross join lateral jsonb_each_text(coalesce(r.reward_items, '{}'::jsonb)) e
        where r.clinic_date >= ((now() at time zone %s)::date - 13)
        group by e.key::bigint
        order by item_count desc, record_count desc, item_id
        """,
        (SPECIAL_CLINIC_ZONE_ID,),
    )
    for row in rows:
        row["item_name"] = SPECIAL_CLINIC_ITEM_NAMES.get(int(row["item_id"]), f"道具 {row['item_id']}")
    return rows


def load_special_clinic_resource_rewards(conn):
    return query_list(
        conn,
        """
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s), 'MM-DD HH24:00') as label,
               coalesce(sum(temporary_patients), 0) as temporary_patients,
               coalesce(sum(ingot_reward), 0) as ingot_reward,
               coalesce(sum(money_reward), 0) as money_reward,
               coalesce(sum(prestige_reward), 0) as prestige_reward,
               coalesce(sum(glory_reward), 0) as glory_reward
        from t_special_clinic_patient_record
        where clinic_date >= ((now() at time zone %s)::date - 13)
        group by label
        order by min(create_time)
        """,
        (SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID),
    )


def load_special_clinic_ticket_flows(conn):
    return query_list(
        conn,
        """
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s), 'MM-DD HH24:00') as label,
               coalesce(change_type, 'UNKNOWN') as change_type,
               coalesce(reason, '') as reason,
               coalesce(sum(appointment_delta), 0) as appointment_delta_sum,
               coalesce(sum(gifted_delta), 0) as gifted_delta_sum,
               coalesce(sum(paid_delta), 0) as paid_delta_sum,
               coalesce(sum(ingot_cost), 0) as ingot_cost_sum,
               count(*) as row_count,
               count(distinct hospital_id) as hospital_count
        from t_special_clinic_ticket_log
        where clinic_date >= ((now() at time zone %s)::date - 13)
        group by label, change_type, reason
        order by min(create_time) desc, row_count desc
        limit 60
        """,
        (SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID),
    )


def load_special_clinic_weekly_cabinet(conn):
    depleted_at_select, depleted_at_params = special_clinic_depleted_at_select(
        column_exists(conn, "t_special_clinic_cabinet", "depleted_at")
    )
    cycle_start_expr = "(clinic_date - (((extract(dow from clinic_date)::int + 4) %% 7) * interval '1 day'))::date"
    rows = query_list(
        conn,
        f"""
        with cabinet_recent as (
            select *,
                   {cycle_start_expr} as clinic_week_start
            from t_special_clinic_cabinet
            where clinic_date >= ((now() at time zone %s)::date - 55)
        ), cabinet_ranked as (
            select *,
                   row_number() over (
                       partition by clinic_week_start
                       order by case when clinic_date = clinic_week_start then 0 else 1 end,
                                clinic_date desc,
                                id desc
                   ) as cabinet_rank
            from cabinet_recent
        ), cabinet_weekly as (
            select clinic_week_start,
                   (array_agg(status::text order by clinic_date desc, id desc))[1] as status,
                   coalesce((array_agg(initial_total order by clinic_date desc, id desc))[1], 0) as initial_total,
                   coalesce((array_agg(remaining_total order by clinic_date desc, id desc))[1], 0) as remaining_total,
                   coalesce((array_agg(total_diagnoses order by clinic_date desc, id desc))[1], 0) as total_diagnoses,
                   coalesce((array_agg(paid_ticket_count order by clinic_date desc, id desc))[1], 0) as paid_ticket_count,
                   coalesce((array_agg(empty_attempt_count order by clinic_date desc, id desc))[1], 0) as empty_attempt_count,
                   {depleted_at_select},
                   coalesce((array_agg(critical_admitted_count order by clinic_date desc, id desc))[1], 0) as critical_admitted_count,
                   coalesce((array_agg(consultation_round order by clinic_date desc, id desc))[1], 0) as consultation_round,
                   coalesce((array_agg(consultation_heat order by clinic_date desc, id desc))[1], 0) as consultation_heat,
                   coalesce((array_agg(consultation_threshold order by clinic_date desc, id desc))[1], 0) as consultation_threshold,
                   coalesce((array_agg(remaining_by_tier::text order by clinic_date desc, id desc))[1], '{{}}') as remaining_by_tier
            from cabinet_ranked c
            where cabinet_rank = 1
            group by clinic_week_start
        ), record_weekly as (
            select {cycle_start_expr} as clinic_week_start,
                   count(*) as diagnosis_count_from_record
            from t_special_clinic_patient_record
            where clinic_date >= ((now() at time zone %s)::date - 55)
            group by clinic_week_start
        )
        select c.clinic_week_start::text as clinic_date,
               coalesce(c.status, '') as status,
               coalesce(c.initial_total, 0) as initial_total,
               coalesce(c.remaining_total, 0) as remaining_total,
               coalesce(c.total_diagnoses, 0) as total_diagnoses,
               coalesce(r.diagnosis_count_from_record, 0) as diagnosis_count_from_record,
               coalesce(c.paid_ticket_count, 0) as paid_ticket_count,
               coalesce(c.empty_attempt_count, 0) as empty_attempt_count,
               coalesce(c.depleted_at, '') as depleted_at,
               coalesce(c.critical_admitted_count, 0) as critical_admitted_count,
               coalesce(c.consultation_round, 0) as consultation_round,
               coalesce(c.consultation_heat, 0) as consultation_heat,
               coalesce(c.consultation_threshold, 0) as consultation_threshold,
               coalesce(c.remaining_by_tier, '{{}}') as remaining_by_tier
        from cabinet_weekly c
        left join record_weekly r on r.clinic_week_start = c.clinic_week_start
        order by c.clinic_week_start desc
        limit 8
        """,
        (SPECIAL_CLINIC_ZONE_ID, *depleted_at_params, SPECIAL_CLINIC_ZONE_ID),
    )
    for row in rows:
        add_special_clinic_supply_metrics(row)
    return rows


def load_special_clinic_hospital_daily(conn):
    return query_list(
        conn,
        """
        with reward_item_summary as (
            select r.hospital_id, r.clinic_date, coalesce(sum(e.value::int), 0) as reward_item_count
            from t_special_clinic_patient_record r
            cross join lateral jsonb_each_text(coalesce(r.reward_items, '{}'::jsonb)) e
            group by r.hospital_id, r.clinic_date
        ), ticket_purchase as (
            select hospital_id, clinic_date,
                   coalesce(sum(paid_delta) filter (where paid_delta > 0 and change_type = 'PURCHASE'), 0) as ticket_purchase_count,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost
            from t_special_clinic_ticket_log
            group by hospital_id, clinic_date
        )
        select r.hospital_id,
               coalesce(h.hospital_name, '') as hospital_name,
               coalesce(h.director_name, '') as director_name,
               r.clinic_date::text as clinic_date,
               count(*) as diagnosis_count,
               count(*) filter (where r.ticket_type_used = 'PAID') as paid_diagnosis_count,
               coalesce(max(t.ticket_purchase_count), 0) as ticket_purchase_count,
               coalesce(max(t.ingot_cost), 0) as ingot_cost,
               coalesce(max(i.reward_item_count), 0) as reward_item_count,
               coalesce(sum(r.temporary_patients), 0) as temporary_patients,
               coalesce(sum(r.ingot_reward + r.money_reward + r.prestige_reward + r.glory_reward), 0) as resource_reward_total,
               to_char((min(r.create_time) at time zone 'UTC' at time zone %s), 'MM-DD HH24:MI') as first_diagnosis_time,
               to_char((max(r.create_time) at time zone 'UTC' at time zone %s), 'MM-DD HH24:MI') as last_diagnosis_time
        from t_special_clinic_patient_record r
        left join t_hospitals h on h.id = r.hospital_id
        left join reward_item_summary i on i.hospital_id = r.hospital_id and i.clinic_date = r.clinic_date
        left join ticket_purchase t on t.hospital_id = r.hospital_id and t.clinic_date = r.clinic_date
        where r.clinic_date >= ((now() at time zone %s)::date - 13)
        group by r.hospital_id, h.hospital_name, h.director_name, r.clinic_date
        order by diagnosis_count desc, paid_diagnosis_count desc, ingot_cost desc, r.hospital_id
        limit 30
        """,
        (SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID),
    )


def load_special_clinic_audit_checks(conn):
    row = query_one(
        conn,
        """
        with patient as (
            select count(*) as diagnosis_count,
                   coalesce(sum((reward_items ->> '1792')::int), 0) as reward_ticket_from_records,
                   coalesce(sum(ingot_reward), 0) as ingot_reward_from_records
            from t_special_clinic_patient_record
            where clinic_date >= ((now() at time zone %s)::date - 13)
        ), ticket as (
            select count(*) filter (where change_type = 'CONSUME') as ticket_consume_rows,
                   coalesce(sum(appointment_delta) filter (where reason = '特需诊断药方奖励门诊票'), 0) as reward_ticket_from_logs,
                   coalesce(sum(ingot_cost) filter (where change_type = 'PURCHASE'), 0) as ingot_cost_from_ticket_log
            from t_special_clinic_ticket_log
            where clinic_date >= ((now() at time zone %s)::date - 13)
        ), yuanbao as (
            select coalesce(sum(greatest(old_value - new_value, 0)) filter (where reason like '特需门诊元宝补诊%%'), 0) as ingot_cost_from_yuanbao_log,
                   coalesce(sum(greatest(new_value - old_value, 0)) filter (where reason = '特需门诊确诊奖励'), 0) as ingot_reward_from_yuanbao_log
            from t_log_yuanbao
            where (create_time at time zone 'UTC' at time zone %s)::date >= ((now() at time zone %s)::date - 13)
        ), balance_diff as (
            select count(*) as mismatch_count
            from t_special_clinic_player_state s
            left join t_backpack b on b.hospital_id = s.hospital_id
            where coalesce(s.appointment_ticket_balance, 0) + coalesce(s.gifted_ticket_balance, 0) + coalesce(s.paid_ticket_balance, 0)
                <> coalesce((b.items ->> '1792')::int, 0)
        ), prompt as (
            select
                count(*) filter (where content like '%%特需门诊票不足%%' or content like '%%门诊票不足%%') as no_ticket_count,
                count(*) filter (where content like '%%今日患者已经看完%%' or content like '%%今日门诊已满%%') as empty_count,
                count(*) filter (where content like '%%今日门诊已经结诊%%' or content like '%%今日门诊已结诊%%') as closed_count,
                count(*) filter (where content like '%%今日元宝购票已达10张上限%%') as purchase_limit_count,
                count(*) filter (where content like '%%该补诊包暂未开放购买%%') as unopened_package_count,
                count(*) filter (where content like '%%元宝不足%%') as insufficient_ingot_count
            from t_log_right_bottom
            where (create_time at time zone 'UTC' at time zone %s)::date >= ((now() at time zone %s)::date - 13)
        )
        select *
        from patient, ticket, yuanbao, balance_diff, prompt
        """,
        (SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID, SPECIAL_CLINIC_ZONE_ID),
    )
    checks = [
        {
            "name": "看病数对票消耗",
            "left": row.get("diagnosis_count", 0),
            "right": row.get("ticket_consume_rows", 0),
            "diff": int(row.get("diagnosis_count", 0) or 0) - int(row.get("ticket_consume_rows", 0) or 0),
        },
        {
            "name": "返票记录对流水",
            "left": row.get("reward_ticket_from_records", 0),
            "right": row.get("reward_ticket_from_logs", 0),
            "diff": int(row.get("reward_ticket_from_records", 0) or 0) - int(row.get("reward_ticket_from_logs", 0) or 0),
        },
        {
            "name": "购票元宝成本对日志",
            "left": row.get("ingot_cost_from_ticket_log", 0),
            "right": row.get("ingot_cost_from_yuanbao_log", 0),
            "diff": int(row.get("ingot_cost_from_ticket_log", 0) or 0) - int(row.get("ingot_cost_from_yuanbao_log", 0) or 0),
        },
        {
            "name": "确诊元宝奖励对日志",
            "left": row.get("ingot_reward_from_records", 0),
            "right": row.get("ingot_reward_from_yuanbao_log", 0),
            "diff": int(row.get("ingot_reward_from_records", 0) or 0) - int(row.get("ingot_reward_from_yuanbao_log", 0) or 0),
        },
        {
            "name": "当前票余额不一致医院",
            "left": row.get("mismatch_count", 0),
            "right": 0,
            "diff": int(row.get("mismatch_count", 0) or 0),
        },
    ]
    alerts = [
        {"name": "无票尝试看病", "count": row.get("no_ticket_count", 0)},
        {"name": "库存耗尽提示", "count": row.get("empty_count", 0)},
        {"name": "结诊后尝试", "count": row.get("closed_count", 0)},
        {"name": "购票触达上限", "count": row.get("purchase_limit_count", 0)},
        {"name": "未开放包直调", "count": row.get("unopened_package_count", 0)},
        {"name": "元宝不足", "count": row.get("insufficient_ingot_count", 0)},
    ]
    return {"checks": checks, "alerts": alerts}


def load_stats():
    stats = load_stats_from_prod()
    record_snapshot(stats)
    return merge_snapshot_history(stats)


def load_unavailable_stats(error: Exception):
    summary = {
        "skin_owner_accounts": 0,
        "skin_owner_hospitals": 0,
        "skin_equipped_accounts": 0,
        "green_combo_equipped_accounts": 0,
        "skin_free_accounts": 0,
        "skin_purchase_log_accounts": 0,
        "skin_paid_confirmed_accounts": 0,
        "online_now_accounts": 0,
        "active_today_accounts": 0,
        "registrations_today": 0,
        "recharge_cny_today": 0,
        "recharge_yuanbao_today": 0,
        "recharge_orders_today": 0,
        "stripe_recharge_cny_today": 0,
        "stripe_recharge_yuanbao_today": 0,
        "stripe_recharge_orders_today": 0,
        "steam_recharge_cny_today": 0,
        "steam_recharge_yuanbao_today": 0,
        "steam_recharge_orders_today": 0,
    }
    return {
        "generatedAt": now_in_zone().isoformat(),
        "zoneId": ZONE_ID,
        "note": f"数据源暂不可用：{error}",
        "summary": summary,
        "onlineBuckets": [],
        "dailyRecharge": [],
        "dailyActive": [],
        "dailyRegistrations": [],
        "hourlyYuanbaoSpending": [],
        "itemPurchases": [],
        "itemUsages": [],
    }


def load_unavailable_special_clinic_stats(error: Exception):
    return {
        "generatedAt": datetime.now(ZoneInfo(SPECIAL_CLINIC_ZONE_ID)).isoformat(),
        "zoneId": SPECIAL_CLINIC_ZONE_ID,
        "sourceError": str(error),
        "summary": {},
        "hourlySummary": [],
        "tierDistribution": [],
        "patientDistribution": [],
        "rewardItems": [],
        "resourceRewards": [],
        "ticketFlows": [],
        "weeklyCabinet": [],
        "dailyCabinet": [],
        "hospitalDaily": [],
        "auditChecks": {"checks": [], "alerts": []},
    }


def empty_purchase_insights():
    return {
        "item": {"top": [], "buyers": []},
        "background": {"top": [], "buyers": []},
        "skin": {"top": [], "buyers": []},
    }


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/api/stats")
def stats_api():
    try:
        return jsonify(load_stats())
    except Exception as exc:
        app.logger.warning("stats unavailable: %s", exc)
        return jsonify(load_unavailable_stats(exc))


@app.get("/api/special-clinic-stats")
def special_clinic_stats_api():
    try:
        return jsonify(load_special_clinic_stats_from_prod())
    except Exception as exc:
        app.logger.warning("special clinic stats unavailable: %s", exc)
        return jsonify(load_unavailable_special_clinic_stats(exc))


@app.get("/api/stat-table")
def stat_table_api():
    tab, page, page_size = parse_page_args()
    return jsonify(load_stat_table(tab, page, page_size))


@app.get("/api/item-activity-details")
def item_activity_details():
    item_name = request.args.get("itemName", "").strip()
    activity_type = request.args.get("type", "").strip().lower()
    if not item_name:
        return jsonify([])
    with prod_connection() as conn:
        if activity_type == "purchase":
            rows = query_list(
                conn,
                """
                with parsed as (
                    select l.hospital_id, h.hospital_name, h.director_name,
                           (regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[1] as item_name,
                           ((regexp_match(l.reason, '^商店购买: (.+) x ([0-9]+)$'))[2])::bigint as quantity,
                           greatest(coalesce(l.old_value, 0) - coalesce(l.new_value, 0), 0) as yuanbao_cost,
                           l.create_time
                    from t_log_yuanbao l
                    left join t_hospitals h on h.id = l.hospital_id
                    where l.reason ~ '^商店购买: .+ x [0-9]+$'
                      and (l.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 13
                )
                select hospital_id, coalesce(hospital_name, '') as hospital_name, coalesce(director_name, '') as director_name,
                       count(*) as event_count, coalesce(sum(quantity), 0) as quantity, coalesce(sum(yuanbao_cost), 0) as yuanbao,
                       to_char((max(create_time) at time zone 'UTC' at time zone %s), 'YYYY-MM-DD HH24:MI') as last_time
                from parsed
                where item_name = %s
                group by hospital_id, hospital_name, director_name
                order by quantity desc, event_count desc, max(create_time) desc, hospital_id desc
                limit 200
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID, item_name),
            )
        elif activity_type == "use":
            rows = query_list(conn, item_usage_sql("detail"), (ZONE_ID, ZONE_ID, ZONE_ID, item_name))
        else:
            rows = []
    return jsonify(rows)


def sampler():
    ensure_snapshot_table()
    while True:
        try:
            load_stats()
        except Exception as exc:
            app.logger.warning("snapshot sample failed: %s", exc)
        time.sleep(600)


if os.getenv("OPS_DASHBOARD_DISABLE_SAMPLER", "").strip().lower() not in {"1", "true", "yes"}:
    threading.Thread(target=sampler, daemon=True).start()
