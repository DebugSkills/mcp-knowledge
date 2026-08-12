#!/usr/bin/env python3
"""
seed_knowledge.py — Загрузка начального корпуса знаний (русский).

Использование:
    python3 seed_knowledge.py --dir knowledge/engineering/python/backend/
    python3 seed_knowledge.py --single knowledge/engineering/python/backend/async-patterns.md

Предполагает, что mcp-server запущен и доступен на localhost:8000.
"""

import argparse
import os
from pathlib import Path

import httpx
import yaml

MCP_URL = "http://localhost:8000/mcp"
API_KEY = os.environ.get("MCP_WRITE_KEY", "dev-write-key-001")


def parse_markdown_file(filepath: Path) -> dict:
    """Парсинг Markdown с YAML frontmatter."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.startswith("---"):
        print(f"WARN: {filepath} — no frontmatter, skipping")
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        print(f"WARN: {filepath} — invalid frontmatter, skipping")
        return None

    frontmatter = yaml.safe_load(parts[1])
    body = parts[2].strip()

    return {
        "content": body,
        "domain": frontmatter.get("domain", "engineering"),
        "subject": frontmatter.get("subject", "general"),
        "project": frontmatter.get("project"),
        "tags": frontmatter.get("tags", []),
        "cross_subjects": frontmatter.get("cross_subjects", []),
    }


def write_knowledge(client: httpx.Client, entry: dict) -> bool:
    """Запись одной записи через MCP write_knowledge."""
    payload = {
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": entry,
        },
    }
    try:
        resp = client.post(MCP_URL, json=payload, headers={"X-API-Key": API_KEY})
        resp.raise_for_status()
        result = resp.json()
        knowledge_id = result.get("knowledge_id", "unknown")
        print(f"  ✅ {knowledge_id}")
        return True
    except Exception as e:  # noqa: BLE001 — seed-скрипт: логируем и продолжаем
        print(f"  ❌ {entry.get('domain', '?')}/{entry.get('subject', '?')}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Seed MCP Knowledge Server")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", help="Directory of .md files to import")
    group.add_argument("--single", help="Single .md file to import")
    args = parser.parse_args()

    client = httpx.Client(timeout=30.0)

    if args.single:
        filepath = Path(args.single)
        entry = parse_markdown_file(filepath)
        if entry:
            write_knowledge(client, entry)
    elif args.dir:
        dirpath = Path(args.dir)
        md_files = sorted(dirpath.rglob("*.md"))
        total = len(md_files)
        success = 0
        print(f"Seeding {total} files from {dirpath}...")
        for i, filepath in enumerate(md_files, 1):
            print(f"[{i}/{total}] {filepath.name}")
            entry = parse_markdown_file(filepath)
            if entry and write_knowledge(client, entry):
                success += 1
        print(f"\nDone: {success}/{total} imported successfully.")

    client.close()


if __name__ == "__main__":
    main()
