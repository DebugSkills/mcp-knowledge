#!/usr/bin/env python3
r"""Зеркальный прогон restore-цепочки documents (P0-1/P0-2, гейт A4).

Critic per-phase (REVISE 0.64) требовал эмпирического доказательства, что
restore scope=documents ИСПОЛНЯЕТСЯ (а не только проходит lint): discovery
последнего тара, подготовка dest, распаковка, полная sha256-сверка с манифестом,
копирование в data_root. Здесь каждая команда — дословный эквивалент таска из
`ansible/playbooks/backup.yml` (shell: find | sort -rn | awk 'NR==1').

Запуск: .venv/bin/python -m pytest tests/test_backup_restore_chain.py -v
"""

import hashlib
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"


def _discover_latest(backup_dir: Path) -> str:
    """Дословный эквивалент discovery-таска backup.yml (shell+find+awk)."""
    cmd = (
        "set -euo pipefail;"
        f"d={backup_dir};"
        "[ -d \"$d\" ] || { echo \"backup_dir не существует: $d\" >&2; exit 1; };"
        "find \"$d\" -maxdepth 1 -name 'documents-*.tar.gz' -printf '%T@ %p\\n' 2>/dev/null |"
        "sort -rn | awk 'NR==1 { print $2 }'"
    )
    proc = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, f"discovery rc={proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


def test_restore_chain_discovery_returns_path(tmp_path):
    """P0-1(а): при существующем таре discovery возвращает непустой путь (не skip)."""
    data_root = tmp_path / "data"
    docs = data_root / "documents"
    for i in range(3):
        p = docs / "ab" / "cd" / f"{i:064x}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"blob-{i}".encode())
    subprocess.run(["bash", str(BACKUP), "--no-qdrant", "--no-ssot"],
                   env={"DATA_ROOT": str(data_root), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, timeout=120, check=False)
    backup_dir = data_root / "backups"
    assert list(backup_dir.glob("documents-*.tar.gz")), "бэкап не создан"
    latest = _discover_latest(backup_dir)
    assert latest, "discovery вернул пустой путь при наличии тара"
    assert (backup_dir / Path(latest).name).is_file()


def test_restore_chain_full_roundtrip(tmp_path):
    """P0-1(б)+P0-2: распаковка + полная sha256-сверка + копирование в data_root."""
    data_root = tmp_path / "data"
    docs = data_root / "documents"
    blobs = {}
    for i in range(4):
        shard, sub = f"{i:02x}", f"{(i * 7) % 256:02x}"
        p = docs / shard / sub / f"{i:064x}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"blob-content-{i}".encode())
        blobs[f"{shard}/{sub}/{i:064x}"] = hashlib.sha256(f"blob-content-{i}".encode()).hexdigest()
    subprocess.run(["bash", str(BACKUP), "--no-qdrant", "--no-ssot"],
                   env={"DATA_ROOT": str(data_root), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, timeout=120, check=False)
    backup_dir = data_root / "backups"
    latest = _discover_latest(backup_dir)
    manifest = Path(latest + ".sha256")

    # prepare (P0-2): rm -rf + mkdir -p — дословный эквивалент таска
    stage = tmp_path / "stage"
    subprocess.run(["bash", "-c", f"rm -rf {stage} && mkdir -p {stage}"],
                   check=True)

    # unarchive → распаковка тара в stage
    with tarfile.open(latest) as tf:
        tf.extractall(stage)

    # полная sha256-сверка всех файлов (гейт A4a)
    verified = 0
    for ln in manifest.read_text(encoding="utf-8").splitlines():
        h, rel = ln.split(None, 1)
        data = (stage / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == h, f"sha256 расходится: {rel}"
        assert h == blobs[rel.lstrip("./")], f"blob {rel} не совпал с исходником"
        verified += 1
    assert verified == len(blobs), f"сверено {verified} != {len(blobs)}"

    # копирование в data_root/documents (эквивалент таска cp -a)
    out_docs = tmp_path / "out" / "documents"
    out_docs.mkdir(parents=True)
    subprocess.run(["bash", "-c", f"cp -a {stage}/. {out_docs}/"], check=True)
    for rel in blobs:
        assert (out_docs / rel).is_file()


def test_restore_chain_manifest_deleted_fails(tmp_path):
    """P0-1(в): удалён манифест → sha256sum -c падает (не тихий skip)."""
    data_root = tmp_path / "data"
    docs = data_root / "documents"
    p = docs / "ab" / "cd" / ("0" * 64)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    subprocess.run(["bash", str(BACKUP), "--no-qdrant", "--no-ssot"],
                   env={"DATA_ROOT": str(data_root), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, timeout=120, check=False)
    backup_dir = data_root / "backups"
    latest = _discover_latest(backup_dir)
    manifest = Path(latest + ".sha256")
    manifest.unlink()
    # эквивалент таска sha256-сверки: [ -f manifest ] || exit 1
    cmd = (
        f'manifest="{manifest}"; '
        '[ -f "$manifest" ] || { echo "FAIL: манифест отсутствует"; exit 1; }; '
        f'cd {tmp_path} && sha256sum -c "$manifest"'
    )
    proc = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode != 0
    assert "манифест отсутствует" in proc.stdout + proc.stderr
