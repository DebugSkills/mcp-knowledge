# registry/ — реестры режимов (Ф3.5a-1)

Data-only YAML-каталог движка режимов (спека §2): `roles.yaml`, `tools.yaml`,
`gates.yaml`, `model_classes.yaml`, `shapes.yaml` — по одному kind на файл;
загрузчик `Registry` в `__init__.py` (`load()` / `get(kind)` / `reload_if_changed()`).

- **Hot-reload:** mtime каждого файла кэшируется; изменение любого файла —
  перечитка каталога целиком, `reload_if_changed() -> bool`.
- **Fail-closed:** отсутствующий/битый YAML, не-отображение, неизвестный kind —
  `RegistryError` с путём и причиной; без молчаливых дефолтов и частичных состояний.
- Схемную валидацию полей выполняет валидатор (Ф3.5a-2), потребляет движок
  режимов (Ф3.5b); сами данные здесь не интерпретируются.
- Drop-in процедура добавления режимов (`modes/*.yaml` + `make modes-validate`)
  будет закреплена в Ф3.5a-2.
