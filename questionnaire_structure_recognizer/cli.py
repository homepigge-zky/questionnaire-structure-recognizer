from __future__ import annotations

import argparse
from pathlib import Path

from .engine import recognize, save_json
from .reports import save_html


def main():
    parser = argparse.ArgumentParser(description="本地问卷结构识别器")
    parser.add_argument("file")
    parser.add_argument("--output", default="result.json")
    parser.add_argument("--html")
    parser.add_argument("--cache-dir", default=".qsr-cache")
    parser.add_argument("--no-model", action="store_true")
    args = parser.parse_args()
    result = recognize(args.file, args.cache_dir, use_model=not args.no_model, progress=lambda stage, detail, done, total: print(f"[{stage}] {detail}"))
    save_json(result, args.output)
    if args.html:
        save_html(result, args.html)
    print(f"状态={result['extraction_status']} 问题={len(result['questions'])} 待检查={len(result['unresolved_items'])}")


if __name__ == "__main__":
    main()
