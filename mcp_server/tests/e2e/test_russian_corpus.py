"""E2E-тесты MCP Knowledge Server на русском корпусе.

Проверяет полный цикл:
- write_knowledge → search_knowledge → get_entry
- reconciliation при рестарте
- мульти-ключи (read/write разделение)
- 9 MCP Tools (v3.0)
"""
