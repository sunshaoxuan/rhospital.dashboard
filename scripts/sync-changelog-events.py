import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.changelog_events import parse_gameplay_release_events


def main():
    parser = argparse.ArgumentParser(description="从游戏 CHANGELOG.md 生成看板版本事件快照")
    parser.add_argument("source", type=Path, help="游戏 CHANGELOG.md 路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT_DIR / "app" / "data" / "gameplay_release_events.json",
        help="输出 JSON 路径",
    )
    args = parser.parse_args()
    events = parse_gameplay_release_events(args.source.read_text(encoding="utf-8"))
    payload = {
        "source": "hospital-backend/CHANGELOG.md",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(events)} events to {args.output}")


if __name__ == "__main__":
    main()
