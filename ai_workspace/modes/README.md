# modes/ — режимы AI-верстака (drop-in)

Процедура добавления режима (спека §2, код движка НЕ правится):

1. Положить `modes/<mode>.yaml` по схеме §1 (пример-фикстура:
   `tests/fixtures/modes/valid_statya.yaml`).
2. `make modes-validate` — схем-валидация (контур (а), Ф3.5a-2): обязательные
   поля, enum kind/contract, совместимость shape x contract из
   `registry/shapes.yaml`, уникальность id узлов, концы edges существуют.
3. Golden run — прогон режима на эталонной задаче (Ф3.6+).

Первый реальный режим — `statya.yaml` — появляется в Ф3.6.
