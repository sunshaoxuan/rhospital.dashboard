import json
import re
from pathlib import Path


DATE_HEADING_PATTERN = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s+更新日志\s*$")
NUMBERED_ITEM_PATTERN = re.compile(r"^\s*\d+[.、]\s*(.+?)\s*$")
LAUNCH_MARKER_PATTERN = re.compile(r"^[^，。；]{0,18}(?:新增|上线|开放|推出)")
LEGACY_FEATURE_PATTERN = re.compile(r"^(?:增加|实现).{0,48}(?:功能|玩法)")
PLAIN_FEATURE_PATTERN = re.compile(r"^[^，。；]{1,40}功能(?:[，。；]|$)")
NON_PLAYER_MARKERS = (
    "管理员",
    "管理页",
    "后台",
    "部署配置",
    "联调测试",
    "回归测试",
    "数据库迁移",
    "静态资源目录",
    "资源目录",
    "更新日志统一",
    "prod66",
    "Compose",
    "新增病人",
)


def is_gameplay_release_entry(text):
    normalized = str(text or "").strip()
    if not normalized or any(marker in normalized[:80] for marker in NON_PLAYER_MARKERS):
        return False
    return bool(
        LAUNCH_MARKER_PATTERN.search(normalized)
        or LEGACY_FEATURE_PATTERN.search(normalized)
        or PLAIN_FEATURE_PATTERN.search(normalized)
    )


def summarize_release_entry(text, max_length=86):
    normalized = re.sub(r"\s+", "", str(text or "").strip())
    if len(normalized) <= max_length:
        return normalized
    cut = normalized[:max_length]
    punctuation = max(cut.rfind("；"), cut.rfind("。"), cut.rfind("，"))
    if punctuation >= max_length // 2:
        cut = cut[:punctuation]
    return f"{cut.rstrip('，。；')}…"


def parse_gameplay_release_events(changelog_text):
    current_date = None
    events = []
    seen = set()
    for line in str(changelog_text or "").splitlines():
        heading = DATE_HEADING_PATTERN.match(line)
        if heading:
            current_date = heading.group(1)
            continue
        item = NUMBERED_ITEM_PATTERN.match(line)
        if not current_date or not item:
            continue
        full_text = item.group(1).strip()
        if not is_gameplay_release_entry(full_text):
            continue
        title = summarize_release_entry(full_text)
        key = title
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "date": current_date,
            "month": current_date[:7],
            "title": title,
        })
    return events


def load_release_event_snapshot(path):
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return []
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = payload.get("events", []) if isinstance(payload, dict) else payload
    return [
        {
            "date": str(row.get("date") or ""),
            "month": str(row.get("month") or str(row.get("date") or "")[:7]),
            "title": str(row.get("title") or "").strip(),
        }
        for row in rows
        if isinstance(row, dict) and row.get("date") and row.get("title")
    ]
