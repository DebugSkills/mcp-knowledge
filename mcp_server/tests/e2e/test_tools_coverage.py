"""S13-S19: E2E-покрытие инструментов через HTTP /mcp против реального Qdrant.

Фаза 13 (v1.0): закрывает gap-анализ — добавляет E2E для:
- S13: delete_entry (HTTP tools/call)
- S14: list_domains / list_subjects / list_projects (HTTP)
- S15: reindex blue_green=False (HTTP)
- S16: import_content (HTTP путь, дополняет S6)
- S17: MCP initialize
- S18: resources/read kb:// (после прода-правки resources.py Ш10a)
- S19: prompts/list + prompts/get

Изоляция: коллекция knowledge_e2e (_patched_collection session fixture).
Все тесты async — httpx.AsyncClient + ASGITransport (один event loop).

Ловушки:
- wait_for_index(kid, timeout=10) перед delete и перед assert после write
- delete_entry — передача wait=True при прямом обращении к Qdrant
- reindex blue_green=False обязателен (иначе разрушит session-scoped коллекцию)
"""

from __future__ import annotations

import json

import pytest

# ═══════════════════════════════════════════════════════════════
# S13: delete_entry через HTTP /mcp tools/call
# ═══════════════════════════════════════════════════════════════

S13_KNOWLEDGE_ID = "e2e-s13-delete"


@pytest.mark.e2e
async def test_s13_delete_entry_via_http(e2e_http_app):
    """S13: write(e2e-s13) → tools/call delete_entry → verify get_entry error + точка отсутствует в Qdrant.

    Паттерн из S9d/S11: wait_for_index ДО delete (иначе drain перезапишет после delete).
    """
    headers_write = {"X-API-Key": "e2e-write-key"}

    # Step 1: Write entry
    write_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# S13 Delete Test\n\nТестовый контент для проверки удаления.\n",
                "domain": "e2e-delete",
                "subject": "test",
                "knowledge_id": S13_KNOWLEDGE_ID,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers_write)
    assert resp.status_code == 200
    write_body = resp.json()
    assert "result" in write_body, f"Write failed: {write_body}"
    write_data = json.loads(write_body["result"]["content"][0]["text"])
    assert write_data["knowledge_id"] == S13_KNOWLEDGE_ID

    # Step 2: Verify entry exists in Qdrant (get_entry)
    get_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "get_entry",
            "arguments": {"knowledge_id": S13_KNOWLEDGE_ID},
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers_write)
    assert resp.status_code == 200
    get_body = resp.json()
    assert "result" in get_body, f"get_entry before delete failed: {get_body}"

    # Step 3: Wait for index (иначе drain перезапишет после delete)
    await e2e_http_app.app.state.pipeline.wait_for_index(S13_KNOWLEDGE_ID, timeout=10.0)

    # Step 4: Delete entry
    delete_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "delete_entry",
            "arguments": {"knowledge_id": S13_KNOWLEDGE_ID},
        },
        "id": 3,
    }
    resp = await e2e_http_app.post("/mcp", json=delete_payload, headers=headers_write)
    assert resp.status_code == 200
    delete_body = resp.json()
    assert "result" in delete_body, f"Delete failed: {delete_body}"
    delete_data = json.loads(delete_body["result"]["content"][0]["text"])
    assert delete_data["deleted"] is True
    assert delete_data["knowledge_id"] == S13_KNOWLEDGE_ID

    # Step 5: Verify get_entry returns error (entry deleted)
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers_write)
    assert resp.status_code == 200
    get_body2 = resp.json()
    assert "result" in get_body2
    get_data2 = json.loads(get_body2["result"]["content"][0]["text"])
    assert "error" in get_data2, f"Expected error after delete, got: {get_data2}"

    # Step 6: Verify point absent from Qdrant
    all_ids = e2e_http_app.app.state.qdrant.get_all_knowledge_ids()
    assert S13_KNOWLEDGE_ID not in all_ids, (
        f"Point {S13_KNOWLEDGE_ID} still in Qdrant: {all_ids}"
    )


# ═══════════════════════════════════════════════════════════════
# S14: list_domains / list_subjects / list_projects через HTTP
# ═══════════════════════════════════════════════════════════════

S14_KNOWLEDGE_A = "e2e-s14-list-a"
S14_KNOWLEDGE_B = "e2e-s14-list-b"
S14_DOMAIN_A = "e2e-list-domain-a"
S14_DOMAIN_B = "e2e-list-domain-b"


@pytest.mark.e2e
async def test_s14_list_domains_subjects_projects_via_http(e2e_http_app):
    """S14: write 2 записи (domain=e2e-list-a/-b) → list_domains → list_subjects → list_projects."""
    headers_write = {"X-API-Key": "e2e-write-key"}

    # Step 1: Write entry A (domain=e2e-list-domain-a, subject=python)
    payload_a = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# S14 Entry A\n\nPython testing content.\n",
                "domain": S14_DOMAIN_A,
                "subject": "python",
                "project": "test-proj-a",
                "knowledge_id": S14_KNOWLEDGE_A,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=payload_a, headers=headers_write)
    assert resp.status_code == 200
    write_a = json.loads(resp.json()["result"]["content"][0]["text"])
    assert write_a["knowledge_id"] == S14_KNOWLEDGE_A

    # Step 2: Write entry B (domain=e2e-list-domain-b, subject=golang)
    payload_b = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# S14 Entry B\n\nGolang testing content.\n",
                "domain": S14_DOMAIN_B,
                "subject": "golang",
                "project": "test-proj-b",
                "knowledge_id": S14_KNOWLEDGE_B,
                "wait_for_index": True,
            },
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=payload_b, headers=headers_write)
    assert resp.status_code == 200
    write_b = json.loads(resp.json()["result"]["content"][0]["text"])
    assert write_b["knowledge_id"] == S14_KNOWLEDGE_B

    # Step 3: list_domains → both domains present
    list_domains_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "list_domains", "arguments": {}},
        "id": 3,
    }
    resp = await e2e_http_app.post("/mcp", json=list_domains_payload, headers=headers_write)
    assert resp.status_code == 200
    domains_data = json.loads(resp.json()["result"]["content"][0]["text"])
    domains = domains_data["results"]
    assert S14_DOMAIN_A in domains, f"Expected {S14_DOMAIN_A} in domains: {domains}"
    assert S14_DOMAIN_B in domains, f"Expected {S14_DOMAIN_B} in domains: {domains}"

    # Step 4: list_subjects(domain=e2e-list-domain-a) → [python]
    list_subjects_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "list_subjects",
            "arguments": {"domain": S14_DOMAIN_A},
        },
        "id": 4,
    }
    resp = await e2e_http_app.post("/mcp", json=list_subjects_payload, headers=headers_write)
    assert resp.status_code == 200
    subjects_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "python" in subjects_data["results"], f"Expected python in subjects: {subjects_data['results']}"
    assert subjects_data["domain"] == S14_DOMAIN_A

    # Step 5: list_projects → both projects present
    list_projects_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "list_projects", "arguments": {}},
        "id": 5,
    }
    resp = await e2e_http_app.post("/mcp", json=list_projects_payload, headers=headers_write)
    assert resp.status_code == 200
    projects_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "test-proj-a" in projects_data["results"], f"Projects: {projects_data['results']}"
    assert "test-proj-b" in projects_data["results"], f"Projects: {projects_data['results']}"

    # Cleanup
    await e2e_http_app.app.state.pipeline.wait_for_index(S14_KNOWLEDGE_A, timeout=10.0)
    await e2e_http_app.app.state.pipeline.wait_for_index(S14_KNOWLEDGE_B, timeout=10.0)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(S14_KNOWLEDGE_A)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(S14_KNOWLEDGE_B)


# ═══════════════════════════════════════════════════════════════
# S15: reindex blue_green=False через HTTP
# ═══════════════════════════════════════════════════════════════

S15_KNOWLEDGE_ID = "e2e-s15-reindex"


@pytest.mark.e2e
async def test_s15_reindex_blue_green_false_via_http(e2e_http_app):
    """S15: write(e2e-s15) → tools/call reindex (blue_green=false) → verify total_docs≥1, failed=0.

    КРИТИЧНО: blue_green=False (иначе blue-green разрушит session-scoped knowledge_e2e).
    """
    headers_write = {"X-API-Key": "e2e-write-key"}

    # Step 1: Write entry (чтобы коллекция не была пустой)
    write_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# S15 Reindex Test\n\nКонтент для проверки reindex.\n",
                "domain": "e2e-reindex",
                "subject": "test",
                "knowledge_id": S15_KNOWLEDGE_ID,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers_write)
    assert resp.status_code == 200
    write_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert write_data["knowledge_id"] == S15_KNOWLEDGE_ID

    # Step 2: reindex с blue_green=false (delete-all + rebuild в той же коллекции)
    reindex_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "reindex",
            "arguments": {"blue_green": False},
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=reindex_payload, headers=headers_write)
    assert resp.status_code == 200
    reindex_body = resp.json()
    assert "result" in reindex_body, f"Reindex failed: {reindex_body}"
    reindex_data = json.loads(reindex_body["result"]["content"][0]["text"])

    # Verify response fields (admin.py:58-67)
    assert reindex_data["total_docs"] >= 1, f"Expected total_docs≥1, got: {reindex_data}"
    assert reindex_data["failed"] == 0, f"Expected failed=0, got: {reindex_data}"
    assert reindex_data["blue_green"] is False, f"Expected blue_green=false, got: {reindex_data}"
    assert "total_chunks" in reindex_data
    assert "index_sections" in reindex_data

    # Step 3: Verify коллекция не разрушена — get_entry still works
    # (после reindex_all коллекция очищается и перестраивается, S15_KNOWLEDGE_ID должен быть перестроен)
    get_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "get_entry",
            "arguments": {"knowledge_id": S15_KNOWLEDGE_ID},
        },
        "id": 3,
    }
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers_write)
    assert resp.status_code == 200

    # Step 4: Verify НЕТ knowledge_v1/v2 коллекций (blue_green=false не должен создавать)
    import httpx
    async with httpx.AsyncClient() as c:
        r = await c.get("http://localhost:6333/collections", timeout=5.0)
        collections_data = r.json()
        collection_names = [col["name"] for col in collections_data.get("result", {}).get("collections", [])]
        # knowledge_e2e — наша тестовая коллекция (OK)
        # knowledge — продакшн коллекция (OK)
        # knowledge_v1 / knowledge_v2 — НЕ должны появиться
        assert "knowledge_v1" not in collection_names, (
            f"knowledge_v1 found after reindex blue_green=False! Collections: {collection_names}"
        )
        assert "knowledge_v2" not in collection_names, (
            f"knowledge_v2 found after reindex blue_green=False! Collections: {collection_names}"
        )


# ═══════════════════════════════════════════════════════════════
# S16: import_content через HTTP /mcp tools/call
# ═══════════════════════════════════════════════════════════════

S16_DOMAIN = "e2e-import-http"

STRUCTURED_BOOK_S16 = """# Python Async Programming

## Chapter 1: Introduction to Asyncio
Asynchronous programming allows concurrent execution of tasks.
The asyncio module provides event loop, coroutines, and futures.

## Chapter 2: Coroutines and Tasks
Coroutines are the core of asyncio. Use async def to define them.
Tasks wrap coroutines and schedule them on the event loop.

## Chapter 3: Event Loop Internals
The event loop is the heart of asyncio. It manages callbacks,
schedules tasks, and handles I/O events efficiently.
"""


@pytest.mark.e2e
async def test_s16_import_content_via_http(e2e_http_app):
    """S16: POST /mcp tools/call import_content (book, 3 главы) → verify imported≥3, failed=0.

    Дополняет существующий S6 (прямой вызов) — здесь HTTP-путь.
    """
    # S6 (test_russian_corpus) вызывает reset() в finally → реестр препроцессоров пуст.
    # Восстанавливаем book-препроцессор локально (паттерн S6 setup) для самодостаточности.
    from mcp_server.content.book_preprocessor import BookPreprocessor
    from mcp_server.content.registry import register, reset

    reset()
    token_counter = type("TokenCounter", (), {
        "count_tokens": lambda self, text: len(text.split()),
        "truncate_to_tokens": lambda self, text, max_t: " ".join(text.split()[:max_t]),
    })()
    register(BookPreprocessor(embedder=e2e_http_app.app.state.embedder, token_counter=token_counter))

    headers_write = {"X-API-Key": "e2e-write-key"}

    import_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "import_content",
            "arguments": {
                "content": STRUCTURED_BOOK_S16,
                "content_type": "book",
                "domain": S16_DOMAIN,
                "subject": "python",
                "title": "Python Async HTTP",
                "tags": ["async", "python", "e2e"],
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=import_payload, headers=headers_write)
    assert resp.status_code == 200
    import_body = resp.json()
    assert "result" in import_body, f"import_content HTTP failed: {import_body}"
    import_data = json.loads(import_body["result"]["content"][0]["text"])

    # import_content может вернуть error при взаимодействии с другими тестами
    if "error" in import_data:
        pytest.fail(f"import_content returned error: {import_data}")

    assert import_data.get("imported", 0) >= 3, f"Expected imported≥3, got: {import_data}"
    assert import_data.get("failed", 0) == 0, f"Expected failed=0, got: {import_data}"
    assert import_data.get("partial_success", True) is False
    assert import_data.get("collection_id", "").endswith("-collection"), (
        f"collection_id should end with '-collection': {import_data.get('collection_id')}"
    )

    # Cleanup: удаляем коллекцию и её children
    collection_id = import_data["collection_id"]
    await e2e_http_app.app.state.pipeline.wait_for_index(collection_id, timeout=10.0)
    # Удаляем коллекцию из Qdrant
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(collection_id)
    # Удаляем children
    root = await e2e_http_app.app.state.store.read(collection_id)
    if root and root.frontmatter.children:
        for child_ref in root.frontmatter.children:
            cid = child_ref["knowledge_id"]
            e2e_http_app.app.state.qdrant.delete_by_knowledge_id(cid)


# ═══════════════════════════════════════════════════════════════
# S17: MCP initialize через HTTP POST /mcp
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
async def test_s17_initialize_via_http(e2e_http_app):
    """S17: POST /mcp {method:"initialize"} → verify protocolVersion, capabilities."""
    headers = {"X-API-Key": "e2e-read-key"}

    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "e2e-test-client", "version": "0.1.0"},
            "capabilities": {},
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["jsonrpc"] == "2.0"
    assert "result" in body, f"Initialize failed: {body}"
    result = body["result"]

    # protocolVersion
    assert result["protocolVersion"] == "2024-11-05", f"protocolVersion: {result['protocolVersion']}"

    # serverInfo
    assert result["serverInfo"]["name"] == "mcp-knowledge-server"
    assert "version" in result["serverInfo"]

    # capabilities
    caps = result["capabilities"]
    assert "tools" in caps, f"Missing tools capability: {caps}"
    assert "resources" in caps, f"Missing resources capability: {caps}"
    assert "prompts" in caps, f"Missing prompts capability: {caps}"


# ═══════════════════════════════════════════════════════════════
# S18: resources/read kb:// через HTTP POST /mcp
# ═══════════════════════════════════════════════════════════════

S18_KNOWLEDGE_ID = "e2e-s18-resources"
S18_DOMAIN = "e2e-resources-read"
S18_SUBJECT = "networking"


@pytest.mark.e2e
async def test_s18_resources_read_kb_uri_via_http(e2e_http_app):
    """S18: write(e2e-s18) → resources/read kb:// → kb://{domain} → kb://{domain}/{subject}.

    Требует Ш10a: resources.py fix (COLLECTION_NAME import) + conftest patch.
    """
    headers_write = {"X-API-Key": "e2e-write-key"}
    headers_read = {"X-API-Key": "e2e-read-key"}

    # Step 1: Write entry
    write_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# S18 Resources Test\n\nNetworking knowledge entry.\n",
                "domain": S18_DOMAIN,
                "subject": S18_SUBJECT,
                "knowledge_id": S18_KNOWLEDGE_ID,
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers_write)
    assert resp.status_code == 200

    # Step 2: resources/read kb:// → список доменов
    # Формат ответа: result.contents[0] = {uri, text: json_str, mimeType}
    # Где text содержит JSON с полем contents (список доменов)
    kb_root_payload = {
        "jsonrpc": "2.0",
        "method": "resources/read",
        "params": {"uri": "kb://"},
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=kb_root_payload, headers=headers_read)
    assert resp.status_code == 200
    root_body = resp.json()
    assert "result" in root_body, f"resources/read kb:// failed: {root_body}"
    root_result = root_body["result"]

    # Парсим вложенный JSON из text
    assert len(root_result.get("contents", [])) >= 1, f"No contents in: {root_result}"
    inner_json = json.loads(root_result["contents"][0]["text"])
    domain_names = [c["name"] for c in inner_json.get("contents", [])]
    assert S18_DOMAIN in domain_names, f"Expected {S18_DOMAIN} in kb:// contents: {domain_names}"

    # Step 3: resources/read kb://{domain} → список subjects
    kb_domain_payload = {
        "jsonrpc": "2.0",
        "method": "resources/read",
        "params": {"uri": f"kb://{S18_DOMAIN}"},
        "id": 3,
    }
    resp = await e2e_http_app.post("/mcp", json=kb_domain_payload, headers=headers_read)
    assert resp.status_code == 200
    domain_body = resp.json()
    assert "result" in domain_body, f"resources/read kb://{S18_DOMAIN} failed: {domain_body}"
    domain_result = domain_body["result"]

    assert len(domain_result.get("contents", [])) >= 1
    domain_inner = json.loads(domain_result["contents"][0]["text"])
    subject_names = [c["name"] for c in domain_inner.get("contents", [])]
    assert S18_SUBJECT in subject_names, f"Expected {S18_SUBJECT} in subjects: {subject_names}"

    # Step 4: resources/read kb://{domain}/{subject} → список knowledge_ids
    kb_subject_payload = {
        "jsonrpc": "2.0",
        "method": "resources/read",
        "params": {"uri": f"kb://{S18_DOMAIN}/{S18_SUBJECT}"},
        "id": 4,
    }
    resp = await e2e_http_app.post("/mcp", json=kb_subject_payload, headers=headers_read)
    assert resp.status_code == 200
    subject_body = resp.json()
    assert "result" in subject_body, f"resources/read kb://{S18_DOMAIN}/{S18_SUBJECT} failed: {subject_body}"
    subject_result = subject_body["result"]

    assert len(subject_result.get("contents", [])) >= 1
    subject_inner = json.loads(subject_result["contents"][0]["text"])
    kid_names = [c["name"] for c in subject_inner.get("contents", [])]
    assert S18_KNOWLEDGE_ID in kid_names, f"Expected {S18_KNOWLEDGE_ID} in knowledge_ids: {kid_names}"

    # Cleanup
    await e2e_http_app.app.state.pipeline.wait_for_index(S18_KNOWLEDGE_ID, timeout=10.0)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(S18_KNOWLEDGE_ID)


# ═══════════════════════════════════════════════════════════════
# S19: prompts/list + prompts/get через HTTP POST /mcp
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
async def test_s19_prompts_list_and_get_via_http(e2e_http_app):
    """S19: prompts/list → verify 3 промпта → prompts/get("best-practice-write") → messages непустой."""
    headers = {"X-API-Key": "e2e-read-key"}

    # Step 1: prompts/list
    list_payload = {
        "jsonrpc": "2.0",
        "method": "prompts/list",
        "params": {},
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=list_payload, headers=headers)
    assert resp.status_code == 200
    list_body = resp.json()
    assert "result" in list_body, f"prompts/list failed: {list_body}"
    prompts = list_body["result"].get("prompts", [])
    prompt_names = [p["name"] for p in prompts]

    # Verify 3 prompts exist
    assert "how-to-structure-knowledge" in prompt_names, f"Missing how-to-structure-knowledge: {prompt_names}"
    assert "best-practice-write" in prompt_names, f"Missing best-practice-write: {prompt_names}"
    assert "periodic_quality_cleanup" in prompt_names, f"Missing periodic_quality_cleanup: {prompt_names}"
    assert len(prompts) >= 3

    # Step 2: prompts/get("best-practice-write")
    get_payload = {
        "jsonrpc": "2.0",
        "method": "prompts/get",
        "params": {"name": "best-practice-write"},
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers)
    assert resp.status_code == 200
    get_body = resp.json()
    assert "result" in get_body, f"prompts/get failed: {get_body}"
    prompt_result = get_body["result"]

    # messages непустой
    messages = prompt_result.get("messages", [])
    assert len(messages) > 0, f"Expected non-empty messages, got: {prompt_result}"
    assert messages[0]["role"] == "user"
    assert messages[0]["content"]["type"] == "text"
    assert len(messages[0]["content"]["text"]) > 100, (
        f"Expected substantial prompt text, got {len(messages[0]['content']['text'])} chars"
    )

    # Step 3: prompts/get("how-to-structure-knowledge") — тоже непустой
    get_payload2 = {
        "jsonrpc": "2.0",
        "method": "prompts/get",
        "params": {"name": "how-to-structure-knowledge"},
        "id": 3,
    }
    resp = await e2e_http_app.post("/mcp", json=get_payload2, headers=headers)
    assert resp.status_code == 200
    get_body2 = resp.json()
    assert "result" in get_body2
    messages2 = get_body2["result"]["messages"]
    assert len(messages2) > 0


# ═══════════════════════════════════════════════════════════════
# S20: list_collections — новый tool (Variant A: Surface & Enrich)
# ═══════════════════════════════════════════════════════════════

S20_DOMAIN = "e2e-collections"
S20_SUBJECT = "list-test"


@pytest.mark.e2e
async def test_s20_list_collections_via_http(e2e_http_app):
    """S20: import_content (collection) → list_collections → get_entry(TOC).

    Flow:
      1. import_content(e2e-collections) → collection_id
      2. list_collections(domain=e2e-collections) → collection in results
      3. get_entry(collection_id) → TOC with children
    """
    headers_write = {"X-API-Key": "e2e-write-key"}
    headers_read = {"X-API-Key": "e2e-read-key"}

    # Step 1: Import a small book
    import_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "import_content",
            "arguments": {
                "content": "# Глава 1\n\nКонтент первой главы.\n\n## Раздел 1.1\n\nПодраздел.\n\n# Глава 2\n\nКонтент второй главы.",
                "content_type": "book",
                "domain": S20_DOMAIN,
                "subject": S20_SUBJECT,
                "title": "E2E Collections Test Book",
                "tags": ["e2e", "collections-test"],
                "wait_for_index": True,
            },
        },
        "id": 1,
    }
    resp = await e2e_http_app.post("/mcp", json=import_payload, headers=headers_write)
    assert resp.status_code == 200
    import_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in import_result, f"import failed: {import_result}"
    assert import_result["imported"] >= 2, f"Expected >=2 sections, got {import_result.get('imported')}"
    collection_id = import_result["collection_id"]
    assert collection_id.endswith("-collection")

    # Step 2: list_collections — find the collection
    list_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "list_collections",
            "arguments": {"domain": S20_DOMAIN},
        },
        "id": 2,
    }
    resp = await e2e_http_app.post("/mcp", json=list_payload, headers=headers_read)
    assert resp.status_code == 200
    list_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in list_result, f"list_collections failed: {list_result}"
    assert "results" in list_result
    assert len(list_result["results"]) >= 1
    # Find our collection
    our = [c for c in list_result["results"] if c["collection_id"] == collection_id]
    assert len(our) == 1, f"Collection {collection_id} not found in list_collections results"
    assert our[0]["domain"] == S20_DOMAIN
    assert our[0]["section_count"] >= 2
    assert "title" in our[0]

    # Step 3: get_entry on collection → TOC with children
    get_payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "get_entry",
            "arguments": {"knowledge_id": collection_id},
        },
        "id": 3,
    }
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers_read)
    assert resp.status_code == 200
    get_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in get_result, f"get_entry failed: {get_result}"
    assert get_result["content_type"] == "collection"
    children = get_result.get("children", [])
    assert len(children) >= 2, f"Expected >=2 children, got {len(children)}: {get_result}"
    assert children[0]["sequence_number"] == 1
    assert "title" in children[0]
    assert "knowledge_id" in children[0]


# ═══════════════════════════════════════════════════════════════
# S21: fragment_lifecycle_via_http — add→find→update→conflict→delete
# ═══════════════════════════════════════════════════════════════

S21_DOMAIN = "e2e-s21"
S21_SUBJECT = "frag"


@pytest.mark.e2e
async def test_s21_fragment_lifecycle_via_http(e2e_http_app):
    """S21: import → add×2 → get_entry → find → update v1→v2 → conflict → delete → cleanup.

    Паттерн S16: локальный reset()+register(BookPreprocessor), e2e-write-key.
    """
    from mcp_server.content.book_preprocessor import BookPreprocessor
    from mcp_server.content.registry import register, reset

    reset()
    token_counter = type("TokenCounter", (), {
        "count_tokens": lambda self, text: len(text.split()),
        "truncate_to_tokens": lambda self, text, max_t: " ".join(text.split()[:max_t]),
    })()
    register(BookPreprocessor(embedder=e2e_http_app.app.state.embedder, token_counter=token_counter))

    headers_write = {"X-API-Key": "e2e-write-key"}
    headers_read = {"X-API-Key": "e2e-read-key"}

    # Step 1: import_content — создать книгу с 2 главами
    import_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "import_content",
            "arguments": {
                "content": "# Глава 1\n\nКонтент первой главы про Docker.\n\n# Глава 2\n\nКонтент второй главы про Kubernetes.",
                "content_type": "book",
                "domain": S21_DOMAIN,
                "subject": S21_SUBJECT,
                "title": "e2e-s21-book",
                "tags": ["e2e", "fragment-lifecycle"],
                "wait_for_index": True,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=import_payload, headers=headers_write)
    assert resp.status_code == 200
    import_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in import_result, f"import failed: {import_result}"
    collection_id = import_result["collection_id"]
    initial_imported = import_result["imported"]  # может быть 2 (chapters) или 3 (preamble+2)

    # Секции импорта индексируются АСИНХРОННО (wait_for_index=False в _batch_write_sections),
    # а add_fragment считает sequence=max+1 по TOC из Qdrant → ждём индексацию детей
    # напрямую через pipeline.wait_for_index (паттерн S16:393-399). Poll через get_entry
    # НЕ годится: первый запрос кэширует пустой TOC на 30s (data_version не меняется
    # при фоновой индексации).
    import asyncio
    root_entry = await e2e_http_app.app.state.store.read(collection_id)
    child_ids = [c["knowledge_id"] for c in (root_entry.frontmatter.children or [])]
    for child_id in child_ids:
        await e2e_http_app.app.state.pipeline.wait_for_index(child_id, timeout=15.0)

    # Step 2: add_fragment ×2
    frag1_payload = {
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {
            "name": "add_fragment",
            "arguments": {
                "collection_id": collection_id,
                "title": "Совет по Docker Compose",
                "content": "Используйте docker compose ps для мониторинга состояния сервисов в реальном времени.",
                "tags": ["docker", "compose"],
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=frag1_payload, headers=headers_write)
    assert resp.status_code == 200
    frag1_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in frag1_result, f"add_fragment 1 failed: {frag1_result}"
    frag1_id = frag1_result["fragment_id"]
    assert frag1_result["sequence_number"] == initial_imported + 1
    assert frag1_result["indexed"] is True

    frag2_payload = {
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {
            "name": "add_fragment",
            "arguments": {
                "collection_id": collection_id,
                "title": "Kubernetes handler tips",
                "content": "Используйте readiness probes для проверки готовности подов перед отправкой трафика.",
                "tags": ["k8s"],
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=frag2_payload, headers=headers_write)
    assert resp.status_code == 200
    frag2_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in frag2_result, f"add_fragment 2 failed: {frag2_result}"
    frag2_id = frag2_result["fragment_id"]
    assert frag2_result["sequence_number"] == initial_imported + 2
    assert frag2_result["indexed"] is True

    # Step 3: get_entry → проверяем children
    get_payload = {
        "jsonrpc": "2.0", "id": 4,
        "method": "tools/call",
        "params": {"name": "get_entry", "arguments": {"knowledge_id": collection_id}},
    }
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers_read)
    assert resp.status_code == 200
    get_result = json.loads(resp.json()["result"]["content"][0]["text"])
    children = get_result.get("children", [])
    expected_count = initial_imported + 2
    assert len(children) == expected_count, (
        f"Expected {expected_count} children (original {initial_imported} + 2 fragments), got {len(children)}: {[(c['knowledge_id'], c.get('title','')) for c in children]}"
    )
    child_ids = {c["knowledge_id"] for c in children}
    assert frag1_id in child_ids, f"frag1 ({frag1_id}) not in children"
    assert frag2_id in child_ids, f"frag2 ({frag2_id}) not in children"
    # Проверяем sequence order: 1..expected_count
    sequences = [c["sequence_number"] for c in children]
    assert sequences == list(range(1, expected_count + 1)), f"Sequence not 1..{expected_count}: {sequences}"

    # Step 4: find_fragment — поиск по ключевому слову из frag1
    find_payload = {
        "jsonrpc": "2.0", "id": 5,
        "method": "tools/call",
        "params": {
            "name": "find_fragment",
            "arguments": {"collection_id": collection_id, "query": "Docker Compose мониторинг"},
        },
    }
    resp = await e2e_http_app.post("/mcp", json=find_payload, headers=headers_read)
    assert resp.status_code == 200
    find_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert find_result["total"] >= 1, f"find_fragment should find at least 1 result: {find_result}"
    found_ids = [f["fragment_id"] for f in find_result["fragments"]]
    assert frag1_id in found_ids, f"frag1 ({frag1_id}) should be in find results: {found_ids}"

    # Step 5: update_fragment — обновить frag1 (v1 → v2)
    update_payload = {
        "jsonrpc": "2.0", "id": 6,
        "method": "tools/call",
        "params": {
            "name": "update_fragment",
            "arguments": {
                "fragment_id": frag1_id,
                "content": "Обновлённый контент: используйте docker compose ps --format json для машинной обработки.",
                "version": 1,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=update_payload, headers=headers_write)
    assert resp.status_code == 200
    update_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in update_result, f"update_fragment failed: {update_result}"
    assert update_result["version"] == 2
    assert update_result["indexed"] is True

    # Step 6: повторный update_fragment(version=1) → conflict
    resp = await e2e_http_app.post("/mcp", json=update_payload, headers=headers_write)
    assert resp.status_code == 200
    conflict_body = resp.json()
    # VersionConflictError → JSON-RPC error -32005 (не result: conflict:true —
    # update_fragment пробрасывает исключение в handler, в отличие от update_entry)
    assert "error" in conflict_body, f"Expected JSON-RPC error for version conflict, got: {conflict_body}"
    assert conflict_body["error"]["code"] == -32005, f"Expected -32005, got: {conflict_body}"
    assert conflict_body["error"]["data"]["current_version"] == 2

    # Step 7: delete_fragment — удалить frag1 (пауза: write-ключ burst=5, refill 20/мин)
    await asyncio.sleep(3.5)
    delete_payload = {
        "jsonrpc": "2.0", "id": 7,
        "method": "tools/call",
        "params": {
            "name": "delete_fragment",
            "arguments": {"fragment_id": frag1_id},
        },
    }
    resp = await e2e_http_app.post("/mcp", json=delete_payload, headers=headers_write)
    assert resp.status_code == 200
    delete_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert delete_result["deleted"] is True
    assert delete_result["fragment_id"] == frag1_id

    # Step 8: get_entry → frag1 отсутствует, frag2 остаётся
    resp = await e2e_http_app.post("/mcp", json=get_payload, headers=headers_read)
    assert resp.status_code == 200
    get_result2 = json.loads(resp.json()["result"]["content"][0]["text"])
    children2 = get_result2.get("children", [])
    child_ids2 = {c["knowledge_id"] for c in children2}
    assert frag1_id not in child_ids2, f"frag1 ({frag1_id}) should be deleted, still in children: {child_ids2}"
    assert frag2_id in child_ids2, f"frag2 ({frag2_id}) should still be present"

    # Step 9: cleanup — delete_entry(cascade=True) removes collection + remaining sections
    await e2e_http_app.app.state.pipeline.wait_for_index(collection_id, timeout=10.0)
    await asyncio.sleep(3.5)  # refill write-токенов (delete выше потратил последний burst)
    cascade_payload = {
        "jsonrpc": "2.0", "id": 8,
        "method": "tools/call",
        "params": {
            "name": "delete_entry",
            "arguments": {"knowledge_id": collection_id, "cascade": True},
        },
    }
    resp = await e2e_http_app.post("/mcp", json=cascade_payload, headers=headers_write)
    assert resp.status_code == 200
    cascade_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert cascade_result["deleted"] is True

    # Verify Qdrant cleanup: collection + fragments removed
    all_ids = e2e_http_app.app.state.qdrant.get_all_knowledge_ids()
    assert collection_id not in all_ids, f"Collection {collection_id} still in Qdrant after cascade delete"
    assert frag2_id not in all_ids, f"frag2 ({frag2_id}) still in Qdrant after cascade delete"


# ═══════════════════════════════════════════════════════════════
# S22: fragment edge cases — deprecated/empty/missing guards
# ═══════════════════════════════════════════════════════════════

S22_DOMAIN = "e2e-s22"
S22_SUBJECT = "edge"
S22_STANDALONE_ID = "e2e-s22-standalone"


@pytest.mark.e2e
async def test_s22_fragment_edge_cases_via_http(e2e_http_app):
    """S22: add_fragment на несуществующую коллекцию → error,
    на standalone запись → error, с пустым content → error,
    в deprecated-книгу → error.
    """
    from mcp_server.content.book_preprocessor import BookPreprocessor
    from mcp_server.content.registry import register, reset

    reset()
    token_counter = type("TokenCounter", (), {
        "count_tokens": lambda self, text: len(text.split()),
        "truncate_to_tokens": lambda self, text, max_t: " ".join(text.split()[:max_t]),
    })()
    register(BookPreprocessor(embedder=e2e_http_app.app.state.embedder, token_counter=token_counter))

    headers_write = {"X-API-Key": "e2e-write-key"}

    # ── Case 1: add_fragment на несуществующую коллекцию ──
    missing_payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "add_fragment",
            "arguments": {
                "collection_id": "e2e-nonexistent-collection",
                "title": "Test",
                "content": "Should fail.",
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=missing_payload, headers=headers_write)
    assert resp.status_code == 200
    missing_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" in missing_result, f"Expected error for missing collection, got: {missing_result}"
    assert "not found" in missing_result["error"].lower()

    # ── Case 2: add_fragment на standalone запись (не коллекцию) ──
    # Создаём standalone-запись через write_knowledge
    write_payload = {
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {
            "name": "write_knowledge",
            "arguments": {
                "content": "# Standalone Entry\n\nЭто одиночная запись, которая не является книжной коллекцией и не содержит секций.",
                "domain": S22_DOMAIN,
                "subject": S22_SUBJECT,
                "knowledge_id": S22_STANDALONE_ID,
                "wait_for_index": True,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=write_payload, headers=headers_write)
    assert resp.status_code == 200
    write_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in write_result, f"write_knowledge failed: {write_result}"

    standalone_payload = {
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {
            "name": "add_fragment",
            "arguments": {
                "collection_id": S22_STANDALONE_ID,
                "title": "Test",
                "content": "Should fail on non-collection.",
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=standalone_payload, headers=headers_write)
    assert resp.status_code == 200
    standalone_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" in standalone_result, f"Expected error for non-collection, got: {standalone_result}"
    assert "not a book collection" in standalone_result["error"].lower()

    # ── Case 3: add_fragment с пустым content ──
    # Создаём книгу-коллекцию для этого теста
    import_payload = {
        "jsonrpc": "2.0", "id": 4,
        "method": "tools/call",
        "params": {
            "name": "import_content",
            "arguments": {
                "content": "# Chapter 1\n\nEmpty content guard test: добавление фрагмента с пустым содержимым должно отклоняться сервером.",
                "content_type": "book",
                "domain": S22_DOMAIN,
                "subject": S22_SUBJECT,
                "title": "e2e-s22-edge-book",
                "tags": ["e2e", "edge-case"],
                "wait_for_index": True,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=import_payload, headers=headers_write)
    assert resp.status_code == 200
    import_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in import_result, f"import failed: {import_result}"
    s22_coll_id = import_result["collection_id"]

    empty_content_payload = {
        "jsonrpc": "2.0", "id": 5,
        "method": "tools/call",
        "params": {
            "name": "add_fragment",
            "arguments": {
                "collection_id": s22_coll_id,
                "title": "Empty content test",
                "content": "",
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=empty_content_payload, headers=headers_write)
    assert resp.status_code == 200
    empty_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" in empty_result, f"Expected error for empty content, got: {empty_result}"
    assert "must not be empty" in empty_result["error"].lower()

    # ── Case 4: add_fragment в deprecated-книгу ──
    # Создаём книгу → deprecate (через resolve_quality_issue) → add_fragment → error
    # (паузы: write-ключ burst=5 исчерпан на Case 3; refill 20/мин = 1 токен/3с)
    import asyncio
    await asyncio.sleep(3.5)
    dep_import_payload = {
        "jsonrpc": "2.0", "id": 6,
        "method": "tools/call",
        "params": {
            "name": "import_content",
            "arguments": {
                "content": "# Deprecated Book\n\nЭта книга будет деприкейтнута для проверки защиты от добавления фрагментов.",
                "content_type": "book",
                "domain": S22_DOMAIN,
                "subject": S22_SUBJECT,
                "title": "e2e-s22-dep-book",
                "tags": ["e2e", "deprecated"],
                "wait_for_index": True,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=dep_import_payload, headers=headers_write)
    assert resp.status_code == 200
    dep_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in dep_result, f"import deprecated book failed: {dep_result}"
    dep_coll_id = dep_result["collection_id"]

    # Deprecate through resolve_quality_issue
    await asyncio.sleep(3.5)
    deprecate_payload = {
        "jsonrpc": "2.0", "id": 7,
        "method": "tools/call",
        "params": {
            "name": "resolve_quality_issue",
            "arguments": {
                "knowledge_id": dep_coll_id,
                "action": "deprecate",
                "reason": "E2E test: deprecated guard verification",
                "cascade": True,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=deprecate_payload, headers=headers_write)
    assert resp.status_code == 200
    deprecate_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in deprecate_data, f"deprecate failed: {deprecate_data}"

    # Try add_fragment to deprecated book
    await asyncio.sleep(3.5)
    dep_add_payload = {
        "jsonrpc": "2.0", "id": 8,
        "method": "tools/call",
        "params": {
            "name": "add_fragment",
            "arguments": {
                "collection_id": dep_coll_id,
                "title": "Should fail",
                "content": "Cannot add to deprecated book.",
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=dep_add_payload, headers=headers_write)
    assert resp.status_code == 200
    dep_add_result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" in dep_add_result, f"Expected error for deprecated collection, got: {dep_add_result}"
    assert "deprecated" in dep_add_result["error"].lower()

    # ── Cleanup ──
    # Restore + delete deprecated book
    await asyncio.sleep(3.5)
    restore_payload = {
        "jsonrpc": "2.0", "id": 9,
        "method": "tools/call",
        "params": {
            "name": "resolve_quality_issue",
            "arguments": {
                "knowledge_id": dep_coll_id,
                "action": "restore",
                "reason": "E2E cleanup",
                "cascade": True,
            },
        },
    }
    resp = await e2e_http_app.post("/mcp", json=restore_payload, headers=headers_write)
    assert resp.status_code == 200
    restore_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert "error" not in restore_data, f"restore failed: {restore_data}"

    # Delete both collections cascade (пауза перед каждым delete: refill write-токенов)
    await asyncio.sleep(3.5)
    for cid in [s22_coll_id, dep_coll_id]:
        await e2e_http_app.app.state.pipeline.wait_for_index(cid, timeout=10.0)
        del_payload = {
            "jsonrpc": "2.0", "id": 10,
            "method": "tools/call",
            "params": {
                "name": "delete_entry",
                "arguments": {"knowledge_id": cid, "cascade": True},
            },
        }
        resp = await e2e_http_app.post("/mcp", json=del_payload, headers=headers_write)
        assert resp.status_code == 200
        await asyncio.sleep(3.5)

    # Cleanup standalone
    await e2e_http_app.app.state.pipeline.wait_for_index(S22_STANDALONE_ID, timeout=10.0)
    e2e_http_app.app.state.qdrant.delete_by_knowledge_id(S22_STANDALONE_ID)
