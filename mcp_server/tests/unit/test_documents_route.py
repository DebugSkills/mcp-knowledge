"""Ф4a: HTTP-выдача blob — GET/HEAD /documents/{sha256} (план §3.4, TestClient).

Unit-уровень негативной e2e-матрицы §3.4:194-209:
- 400 malformed id (короткий/не-hex/upper) — ДО auth, без раскрытия существования;
- 401 без ключа (+WWW-Authenticate) / с невалидным ключом;
- 404-семантика (никогда 403): валидный неизвестный sha; blob отсутствует;
  blob без Source-refs; зона/status/license не проходят (subscriber+private;
  public license=unknown/restricted — fail-closed; public_allowed=False;
  deprecated); detail одинаков для всех причин (no-oracle);
- 200: read+private; subscriber+public licensed; write(env)+private;
  least-strict (private+public refs: read по private, subscriber по public);
- заголовки: nosniff, Accept-Ranges, Content-Length, Content-Disposition
  (санитайз враждебного имени, fallback <sha256><ext>), Content-Type whitelist
  (html → octet-stream, параметры срезаются, None → дефолт);
- Range → 206 + Content-Range + длина; невалидный/за пределами → 416
  (+Content-Range: bytes */len, multi-range/не-bytes → 416); HEAD → те же
  заголовки без тела (вкл. Range-ветку).

Фикстуры: реальный AuthMiddleware на main.app (без lifespan — state
проставляется тестами и снимается), DocumentStore в tmp_path, SourceRefIndex,
TokenStore с subscriber/read ключами (паттерн test_auth_subscriber.py);
write-ключ — env из tests/conftest.py.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp_server.main import app
from mcp_server.storage import DocumentStore
from mcp_server.token_store import TokenRecord, TokenStore, _hash_key
from mcp_server.tools.source_ref_index import SourceRef, SourceRefIndex

# ── Константы контура ─────────────────────────────────────────

PAYLOAD = b"%PDF-1.4\n" + b"0123456789abcdef" * 128  # 9 + 2048 = 2057 байт
SIZE = len(PAYLOAD)
UNKNOWN_SHA = "e" * 64  # валидный формат, нигде не существует
MISSING_BLOB_SHA = "c" * 64  # на него есть ref, но blob не положен

SUB_KEY = "mcp_sa_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
READ_KEY = "mcp_rx_" + "b1a2d3c4f5e6b7a8c9d0e1f2a3b4c5d6"
ENV_WRITE_KEY = "test-write-key"  # tests/conftest.py → MCP_WRITE_KEYS

SUB_HEADERS = {"X-API-Key": SUB_KEY}
READ_HEADERS = {"X-API-Key": READ_KEY}
WRITE_HEADERS = {"X-API-Key": ENV_WRITE_KEY}


def _token_record(key: str, *, level: str, zone: str, token_id: str) -> TokenRecord:
    return TokenRecord(
        id=token_id,
        key_hash=_hash_key(key),
        level=level,
        zone=zone,
        scope=None,
        active=True,
        expires_at=None,
        source="manual",
    )


# ── Фикстуры ──────────────────────────────────────────────────


@pytest.fixture()
def kb(tmp_path):
    """Изолированный контур: store + index + token_store на app.state (без lifespan)."""
    document_store = DocumentStore(tmp_path / "documents", max_gb=1)
    index = SourceRefIndex()
    token_store = TokenStore(tokens_dir=str(tmp_path / "tokens"))
    token_store.save(
        [
            _token_record(SUB_KEY, level="subscriber", zone="public", token_id="tok_sub"),
            _token_record(READ_KEY, level="read", zone="both", token_id="tok_read"),
        ]
    )
    app.state.document_store = document_store
    app.state.source_ref_index = index
    app.state.token_store = token_store
    yield SimpleNamespace(document_store=document_store, index=index)
    app.state.document_store = None
    app.state.source_ref_index = None
    app.state.token_store = None


@pytest.fixture()
def client(kb) -> TestClient:
    return TestClient(app)


def _put(kb, data=None, *, mime="application/pdf", filename="paper.pdf") -> str:
    """Положить blob в стор; вернуть sha256."""
    data = PAYLOAD if data is None else data
    return kb.document_store.put(data, mime=mime, filename=filename).sha256


def _ref(
    kb,
    sha: str,
    *,
    zone: str = "private",
    status: str = "published",
    license_value: str | None = None,
    public_allowed: bool | None = None,
    source_id: str | None = None,
) -> None:
    """Добавить Source-ref в индекс (SSOT-снимок зоны/статуса/license)."""
    kb.index.add(
        SourceRef(
            source_id=source_id or f"src-{sha[:16]}",
            zone=zone,
            status=status,
            license=license_value,
            public_allowed=public_allowed,
            shas=(sha,),
        )
    )


# ── 400: синтаксис id (до auth, без раскрытия существования) ──


class TestValidation400:
    def test_short_id_rejected(self, client):
        resp = client.get(f"/documents/{'a' * 63}", headers=READ_HEADERS)
        assert resp.status_code == 400

    def test_nonhex_id_rejected(self, client):
        resp = client.get(f"/documents/{'z' * 64}", headers=READ_HEADERS)
        assert resp.status_code == 400

    def test_uppercase_id_rejected(self, client):
        sha = hashlib.sha256(b"upper").hexdigest().upper()
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 400

    def test_malformed_precedes_auth(self, client):
        """Синтаксическая ошибка публична → 400 даже без ключа (не 401)."""
        resp = client.get(f"/documents/{'a' * 63}")
        assert resp.status_code == 400

    def test_head_malformed_rejected(self, client):
        resp = client.head(f"/documents/{'a' * 63}", headers=READ_HEADERS)
        assert resp.status_code == 400


# ── 401: аутентификация ───────────────────────────────────────


class TestAuth401:
    def test_no_key_401_with_www_authenticate(self, client):
        resp = client.get(f"/documents/{UNKNOWN_SHA}")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").startswith("ApiKey")

    def test_invalid_key_401(self, client):
        resp = client.get(f"/documents/{UNKNOWN_SHA}", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_head_no_key_401(self, client):
        resp = client.head(f"/documents/{UNKNOWN_SHA}")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").startswith("ApiKey")


# ── 404-семантика (никогда 403 — не раскрываем существование) ──


class TestNotFound404:
    def test_unknown_valid_sha_404(self, client):
        resp = client.get(f"/documents/{UNKNOWN_SHA}", headers=READ_HEADERS)
        assert resp.status_code == 404

    def test_blob_missing_404(self, client, kb):
        """Ref есть, blob физически отсутствует → 404."""
        _ref(kb, MISSING_BLOB_SHA)
        resp = client.get(f"/documents/{MISSING_BLOB_SHA}", headers=READ_HEADERS)
        assert resp.status_code == 404

    def test_blob_without_refs_404(self, client, kb):
        """Blob есть, ∄ Source-ref → least-strict отказ → 404."""
        sha = _put(kb)
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 404

    def test_subscriber_private_404(self, client, kb):
        sha = _put(kb)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 404

    def test_subscriber_private_original_blob_404(self, client, kb):
        """Вторая точка zone-чека: original-blob private-Source, subscriber."""
        sha = _put(kb, filename="original.pdf")
        _ref(kb, sha, zone="private", source_id="src-original-1")
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 404

    def test_subscriber_mixed_no_public_ref_404(self, client, kb):
        """Private + public-restricted refs: subscriber не проходит ни по одному."""
        sha = _put(kb)
        _ref(kb, sha, zone="private", source_id="src-priv-mixed")
        _ref(kb, sha, zone="public", license_value="restricted",
             source_id="src-pub-mixed")
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 404

    def test_subscriber_public_unknown_license_404(self, client, kb):
        """license=unknown → fail-closed (О-3): метаданные есть, viewer нет."""
        sha = _put(kb)
        _ref(kb, sha, zone="public", license_value="unknown")
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 404

    def test_subscriber_public_restricted_license_404(self, client, kb):
        sha = _put(kb)
        _ref(kb, sha, zone="public", license_value="restricted")
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 404

    def test_subscriber_public_not_allowed_404(self, client, kb):
        sha = _put(kb)
        _ref(kb, sha, zone="public", license_value="cc-by-4.0", public_allowed=False)
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 404

    def test_read_deprecated_404(self, client, kb):
        """status=deprecated (SSOT) → недоступен даже полным ключом."""
        sha = _put(kb)
        _ref(kb, sha, zone="private", status="deprecated")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 404

    def test_read_public_only_restricted_404(self, client, kb):
        """License-гейт public-refs не зависит от уровня ключа (§3.4:169)."""
        sha = _put(kb)
        _ref(kb, sha, zone="public", license_value="restricted")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 404

    def test_detail_is_generic_no_oracle(self, client, kb):
        """404-detail одинаков для «не существует» и «нет доступа»."""
        unknown = client.get(f"/documents/{UNKNOWN_SHA}", headers=READ_HEADERS)
        sha = _put(kb)
        _ref(kb, sha, zone="private")
        denied = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert unknown.status_code == denied.status_code == 404
        assert unknown.json()["detail"] == denied.json()["detail"]

    def test_head_unknown_404(self, client):
        resp = client.head(f"/documents/{UNKNOWN_SHA}", headers=READ_HEADERS)
        assert resp.status_code == 404


# ── 200: доступные выдачи ─────────────────────────────────────


class TestAccess200:
    def test_read_private_200(self, client, kb):
        sha = _put(kb)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        assert resp.content == PAYLOAD

    def test_subscriber_public_licensed_200(self, client, kb):
        sha = _put(kb)
        _ref(kb, sha, zone="public", license_value="cc-by-4.0", public_allowed=True)
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 200
        assert resp.content == PAYLOAD

    def test_write_env_key_private_200(self, client, kb):
        """Env write-ключ (conftest) — полная зона."""
        sha = _put(kb)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=WRITE_HEADERS)
        assert resp.status_code == 200
        assert resp.content == PAYLOAD

    def test_least_strict_read_via_private_ref(self, client, kb):
        """Blob с private+public(restricted) refs: read проходит по private."""
        sha = _put(kb)
        _ref(kb, sha, zone="private", source_id="src-ls-priv")
        _ref(kb, sha, zone="public", license_value="restricted", source_id="src-ls-pub")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        # subscriber по тем же refs — только public(restricted) → 404
        resp_sub = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp_sub.status_code == 404

    def test_subscriber_via_public_licensed_ref(self, client, kb):
        """Blob с public(licensed)-ref доступен subscriber независимо от private."""
        sha = _put(kb)
        _ref(kb, sha, zone="public", license_value="cc-by-4.0", source_id="src-ls-pub2")
        resp = client.get(f"/documents/{sha}", headers=SUB_HEADERS)
        assert resp.status_code == 200


# ── Заголовки ответа ──────────────────────────────────────────


class TestResponseHeaders:
    def test_security_and_range_headers(self, client, kb):
        sha = _put(kb)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["accept-ranges"] == "bytes"
        assert resp.headers["content-length"] == str(SIZE)
        assert resp.headers["content-type"] == "application/pdf"

    def test_content_disposition_sanitized(self, client, kb):
        """Враждебное имя (CRLF/кавычки) не попадает в заголовок."""
        hostile = 'report";\r\nSet-Cookie: pwn=1; filename="x.pdf'
        sha = _put(kb, filename=hostile)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        cd = resp.headers["content-disposition"]
        assert "\r" not in cd and "\n" not in cd
        # Санитайз УДАЛЯЕТ control/кавычки из имени, а не полагается на
        # percent-кодировку quote(): в filename* их не должно быть даже
        # в закодированном виде (%0D/%0A/%22) — мутация M4 детектируема.
        cd_lower = cd.lower()
        assert "%0d" not in cd_lower and "%0a" not in cd_lower and "%22" not in cd_lower
        assert "filename*=UTF-8''" in cd
        assert "Set-Cookie" in cd  # текст выжил, CRLF/кавычки — вырезаны

    def test_content_disposition_fallback_sha(self, client, kb):
        sha = _put(kb, filename=None)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        cd = resp.headers["content-disposition"]
        assert f"{sha}.pdf" in cd  # ASCII-fallback <sha256><ext>
        assert "filename*=UTF-8''" in cd

    def test_content_type_html_downgraded(self, client, kb):
        """text/html вне whitelist → octet-stream (нет inline-исполнения)."""
        sha = _put(kb, mime="text/html", filename="page.html")
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"

    def test_content_type_params_stripped(self, client, kb):
        sha = _put(kb, mime="application/pdf; charset=utf-8")
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.headers["content-type"] == "application/pdf"

    def test_content_type_missing_default(self, client, kb):
        sha = _put(kb, mime=None)
        _ref(kb, sha, zone="private")
        resp = client.get(f"/documents/{sha}", headers=READ_HEADERS)
        assert resp.headers["content-type"] == "application/octet-stream"


# ── Range-запросы ─────────────────────────────────────────────


class TestRangeRequests:
    @pytest.fixture(autouse=True)
    def _blob(self, kb):
        self.sha = _put(kb)
        _ref(kb, self.sha, zone="private")

    def test_range_prefix_206(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=0-4"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-4/{SIZE}"
        assert resp.headers["content-length"] == "5"
        assert resp.content == PAYLOAD[:5]

    def test_range_open_end_206(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=100-"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 100-{SIZE - 1}/{SIZE}"
        assert resp.content == PAYLOAD[100:]

    def test_range_suffix_206(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=-16"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes {SIZE - 16}-{SIZE - 1}/{SIZE}"
        assert resp.content == PAYLOAD[-16:]

    def test_range_end_clamped_206(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=0-99999"}
        )
        assert resp.status_code == 206
        assert resp.headers["content-range"] == f"bytes 0-{SIZE - 1}/{SIZE}"
        assert resp.content == PAYLOAD

    def test_range_start_at_size_416(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": f"bytes={SIZE}-"}
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"] == f"bytes */{SIZE}"

    def test_range_beyond_size_416(self, client):
        resp = client.get(
            f"/documents/{self.sha}",
            headers={**READ_HEADERS, "Range": "bytes=99999-99999"},
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"] == f"bytes */{SIZE}"

    def test_range_malformed_416(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=zz-yy"}
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"] == f"bytes */{SIZE}"

    def test_range_start_gt_end_416(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=5-2"}
        )
        assert resp.status_code == 416

    def test_range_multi_416(self, client):
        """Multi-range не поддерживается → невалидный → 416 (не частичная ложь)."""
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=0-1,3-4"}
        )
        assert resp.status_code == 416

    def test_range_non_bytes_unit_416(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "items=0-1"}
        )
        assert resp.status_code == 416

    def test_range_suffix_zero_416(self, client):
        resp = client.get(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=-0"}
        )
        assert resp.status_code == 416


# ── HEAD ──────────────────────────────────────────────────────


class TestHeadRequests:
    @pytest.fixture(autouse=True)
    def _blob(self, kb):
        self.sha = _put(kb)
        _ref(kb, self.sha, zone="private")

    def test_head_200_headers_no_body(self, client):
        resp = client.head(f"/documents/{self.sha}", headers=READ_HEADERS)
        assert resp.status_code == 200
        assert resp.content == b""
        assert resp.headers["content-length"] == str(SIZE)
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["accept-ranges"] == "bytes"
        assert "filename*=UTF-8''" in resp.headers["content-disposition"]

    def test_head_range_206(self, client):
        resp = client.head(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": "bytes=0-4"}
        )
        assert resp.status_code == 206
        assert resp.content == b""
        assert resp.headers["content-length"] == "5"
        assert resp.headers["content-range"] == f"bytes 0-4/{SIZE}"

    def test_head_range_416(self, client):
        resp = client.head(
            f"/documents/{self.sha}", headers={**READ_HEADERS, "Range": f"bytes={SIZE}-"}
        )
        assert resp.status_code == 416
        assert resp.headers["content-range"] == f"bytes */{SIZE}"
