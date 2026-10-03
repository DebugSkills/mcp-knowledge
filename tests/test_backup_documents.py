#!/usr/bin/env python3
r"""Тесты Фазы 0 (code-2026-10-02-bibliography): documents blob-store в backup.sh.

Проверяем интеграцию каталога data/documents в жизненный цикл бэкапа:
  - backup_documents() создаёт tar + детерминированный sha256-манифест;
  - пустой / отсутствующий каталог → no-op (гейт A6, бэкап не падает);
  - verify_documents_drill() (--verify) сверяет выборочные sha256 из манифеста;
  - --no-documents пропускает и бэкап, и drill.

Изоляция: DATA_ROOT → tmp_path; Qdrant/SSOT отключены флагами (--no-qdrant
--no-ssot) — тест не зависит от живого стека. Живой .env репо НЕ читается
(backup_secrets тарит его в temp/backups — безвредно, tmp_path очищается).

Запуск: .venv/bin/python -m pytest tests/test_backup_documents.py -v
"""

import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "scripts" / "backup.sh"


def _make_docs_tree(docs_dir: Path, n: int = 5) -> list[str]:
    """Sharded-дерево ab/cd/<sha256-64> с детерминированным содержимым → пути blobs."""
    rels = []
    for i in range(n):
        shard = f"{i:02x}"[:2] or "00"          # ab / cd — два hex-слоя
        sub = f"{(i * 7) % 256:02x}"
        name = f"{i:064x}"                       # 64-hex имя (полный sha256 по форме)
        p = docs_dir / shard / sub / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"blob-{i}\n".encode())
        rels.append(f"{shard}/{sub}/{name}")
    return rels


def _run_backup(tmp_path: Path, *args: str) -> str:
    """backup.sh в изолированном DATA_ROOT; возвращает stdout+stderr."""
    data_root = tmp_path / "data"
    proc = subprocess.run(
        ["bash", str(BACKUP), *args],
        env={
            "DATA_ROOT": str(data_root),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        capture_output=True, text=True, timeout=120, check=False,
    )
    return data_root, proc.returncode, proc.stdout + proc.stderr


def test_backup_documents_creates_tar_and_manifest(tmp_path):
    """Непустой documents/ → tar + манифест с числом строк == числу файлов."""
    data_root = tmp_path / "data"
    rels = _make_docs_tree(data_root / "documents")
    _, rc, out = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0, f"backup.sh exit={rc}\n{out}"
    tars = sorted((data_root / "backups").glob("documents-*.tar.gz"))
    assert tars, "documents-тар не создан"
    manifest = Path(str(tars[-1]) + ".sha256")
    assert manifest.is_file(), "манифест не создан"
    lines = manifest.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(rels), f"манифест {len(lines)} строк != {len(rels)} файлов"
    # манифест содержит все blob-пути (относительные, с префиксом ./)
    for rel in rels:
        assert any(rel in ln for ln in lines), f"в манифесте нет {rel}"


def test_backup_documents_roundtrip_sha256_matches_manifest(tmp_path):
    """Распаковка тара → sha256 каждого файла == манифест (гейт A4 на стороне бэкапа)."""
    import hashlib

    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    tar_path = max((data_root / "backups").glob("documents-*.tar.gz"))
    manifest = Path(str(tar_path) + ".sha256")
    expected = {}
    for ln in manifest.read_text(encoding="utf-8").splitlines():
        h, rel = ln.split(None, 1)
        expected[rel] = h
    extract = tmp_path / "extract"
    with tarfile.open(tar_path) as tf:
        tf.extractall(extract)
    for rel, h in expected.items():
        data = (extract / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == h, f"sha256 расходится: {rel}"


def test_backup_documents_empty_dir_noop(tmp_path):
    """Пустой documents/ → no-op: тар не создаётся, exit 0 (гейт A6)."""
    data_root = tmp_path / "data"
    (data_root / "documents").mkdir(parents=True)
    _, rc, out = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    assert not list((data_root / "backups").glob("documents-*.tar.gz")), \
        "пустой documents/ не должен создавать тар"
    assert "документов нет" not in out and "пуст" in out


def test_backup_documents_missing_dir_noop(tmp_path):
    """Отсутствующий documents/ → no-op без падения (exit 0)."""
    data_root = tmp_path / "data"
    _, rc, out = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    assert not list((data_root / "backups").glob("documents-*.tar.gz"))
    assert "отсутствует" in out


def test_backup_no_documents_flag_skips(tmp_path):
    """--no-documents: каталог есть, но тар не создаётся."""
    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot", "--no-documents")
    assert rc == 0
    assert not list((data_root / "backups").glob("documents-*.tar.gz"))


def test_verify_documents_drill_pass(tmp_path):
    """--verify: documents-drill зелёный (выборочная сверка)."""
    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    _, vrc, vout = _run_backup(tmp_path, "--verify", "--no-qdrant")
    assert vrc == 0, f"--verify exit={vrc}\n{vout}"
    assert "documents drill" in vout
    assert "RESTORE TEST PASSED" in vout


def test_verify_documents_drill_missing_manifest_fails(tmp_path):
    """Удалён манифест → documents-drill FAIL (не тихий skip)."""
    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    tar_path = max((data_root / "backups").glob("documents-*.tar.gz"))
    Path(str(tar_path) + ".sha256").unlink()
    _, vrc, vout = _run_backup(tmp_path, "--verify", "--no-qdrant")
    assert vrc != 0
    assert "манифест отсутствует" in vout
    assert "RESTORE TEST FAILED" in vout


def test_verify_documents_drill_corrupt_manifest_hash_fails(tmp_path):
    """Испорченный hash в манифесте (M1) → drill FAIL: сверка не снимается безнаказанно."""
    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    tar_path = max((data_root / "backups").glob("documents-*.tar.gz"))
    manifest = Path(str(tar_path) + ".sha256")
    # портим hash ПЕРВОЙ строки (drill сверяет первые 3 строки → ловит)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    lines[0] = "0" * 64 + lines[0][64:]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _, vrc, vout = _run_backup(tmp_path, "--verify", "--no-qdrant")
    assert vrc != 0
    assert "sha256 mismatch" in vout
    assert "RESTORE TEST FAILED" in vout


def test_verify_documents_drill_corrupt_tar_fails(tmp_path):
    """Битый tar (M2) → drill FAIL: `tar -tzf` не снимается безнаказанно."""
    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    tar_path = max((data_root / "backups").glob("documents-*.tar.gz"))
    tar_path.write_bytes(b"not a gzip tar archive")
    _, vrc, vout = _run_backup(tmp_path, "--verify", "--no-qdrant")
    assert vrc != 0
    assert "tar -tzf FAILED" in vout
    assert "RESTORE TEST FAILED" in vout


def test_verify_no_documents_flag_skips_drill(tmp_path):
    """--verify --no-documents → documents-drill пропущен (не FAIL, не PASS)."""
    data_root = tmp_path / "data"
    _make_docs_tree(data_root / "documents")
    _, rc, _ = _run_backup(tmp_path, "--no-qdrant", "--no-ssot")
    assert rc == 0
    _, _, vout = _run_backup(tmp_path, "--verify", "--no-qdrant", "--no-documents")
    assert "Documents drill: --no-documents" in vout
    assert "Documents drill (Фаза 0)" not in vout
