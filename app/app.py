import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from flask import Flask, jsonify, render_template, request


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("OPS_DASHBOARD_DATA_DIR", "/data"))
SQLITE_PATH = DATA_DIR / "ops_dashboard.sqlite3"
ZONE_ID = os.getenv("OPS_DASHBOARD_TIME_ZONE", "Asia/Tokyo")
QUERY_TIMEOUT_SECONDS = int(os.getenv("OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS", "10"))
STAT_TABLE_PAGE_SIZES = {20, 50, 100}
STAT_TABLE_TABS = {"items", "money", "yuanbao", "prestige", "guild", "registrants"}

app = Flask(__name__, template_folder=str(APP_DIR / "templates"))


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
            "generatedAt": datetime.now().astimezone().isoformat(),
            "zoneId": ZONE_ID,
            "note": "在线历史来自医院最后心跳时间；最近7日日活来自进入游戏日志。",
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
                  and (r.create_time at time zone 'UTC' at time zone %s)::date >= (now() at time zone %s)::date - 6
                group by day
                order by day
                """,
                (ZONE_ID, ZONE_ID, ZONE_ID),
            ),
            "dailyRecharge": load_daily_recharge(conn),
            "dailySkin": load_hourly_skin(conn),
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


def load_hourly_skin(conn):
    return query_list(
        conn,
        """
        select to_char(date_trunc('hour', create_time at time zone 'UTC' at time zone %s),
                       'MM-DD HH24:00') as day,
               sum(case when content = '成功领取1个【荣耀绿茵(期间限定)】' then 1 else 0 end) as free_count,
               sum(case when content = '成功购买1个【荣耀绿茵(期间限定)】' then 1 else 0 end) as purchase_count
        from t_log_right_bottom
        where content in ('成功领取1个【荣耀绿茵(期间限定)】', '成功购买1个【荣耀绿茵(期间限定)】')
        group by day
        order by min(create_time)
        """,
        (ZONE_ID,),
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
    day = datetime.fromisoformat(stats["generatedAt"]).date().isoformat()
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
    with sqlite_connection() as conn:
        rows = conn.execute("select * from daily_snapshot order by day").fetchall()
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
        "generatedAt": datetime.now().astimezone().isoformat(),
        "zoneId": ZONE_ID,
        "note": f"数据源暂不可用：{error}",
        "summary": summary,
        "onlineBuckets": [],
        "dailyRecharge": [],
        "dailyActive": [],
        "dailyRegistrations": [],
        "dailySkin": [],
        "itemPurchases": [],
        "itemUsages": [],
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
