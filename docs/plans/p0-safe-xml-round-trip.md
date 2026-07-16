# Безопасный XML round-trip

## Результат и предотвращаемый риск

Все XML-чтения LaunchBox проходят через общий preserving codec, а mutation core
сериализует документ вместе с зафиксированным при чтении lexical profile. Apply
для dedupe и replace-paths меняет только разрешённые узлы и больше не удаляет
comments, processing instructions, namespace prefixes либо исходные свойства
документа только потому, что стандартный `ElementTree` их не сохранил.

## Класс сложности и затрагиваемые подсистемы

Задача cross-cutting: меняются shared XML read/write, cancellable parsing и
serialization, обе XML-мутации и их end-to-end тесты. CLI, GUI, manifest и
transaction state machine потребляют прежние operation outcomes и не получают
новых состояний.

## Зависимости и архитектурные enablers

Используются существующие trusted-read guards, `XmlMutation`, transaction
executor, `OperationControl` и byte/event checkpoint limits. Минимальный enabler
задачи — source profile, принадлежащий parsed tree и являющийся единственным
источником истины для declaration/encoding/BOM/EOL и top-level XML nodes.

Задача является enabler для nested/mixed dedupe, snapshot guard, memory/codec
refactor и новых XML-мутаций. Она не зависит от этих результатов.

## In scope

- Один shared parser для cancellable и обычного чтения XML.
- Сохранение порядка, text/tail, unknown elements/attributes, comments и PI.
- Сохранение исходных qualified names, prefix spelling и локальных `xmlns`
  declarations без process-global `ElementTree` namespace registry.
- Сохранение наличия и содержания XML declaration, исходного encoding, BOM и
  стиля строк документа (`LF`, `CRLF` либо `CR`).
- Совместимость сериализатора с обычным `ElementTree`, созданным не codec-слоем.
- End-to-end `read -> one-target mutation -> write -> reread` для dedupe и
  replace-paths, включая namespaced XML и Unicode.
- Cancellable parse/serialize для больших text/attribute/tree/namespaced inputs
  с прежними byte/event bounds.
- Минимальная адаптация XML-consumers, необходимая для безопасного игнорирования
  либо стабильного представления сохранённых comments/PI.

## Out of scope

- Исправление сортировки nested/mixed children в dedupe canonicalization: это
  следующая отдельная P0 поверх готового codec-контракта.
- Snapshot/fingerprint guard, crash journal и handle-relative WinAPI hardening.
- Streaming/peak-memory refactor и удаление DOM из operation results.
- Byte-for-byte сохранение несемантических вариантов: вида кавычек attributes,
  entity spelling, CDATA spelling, empty-element spelling и расположения
  attributes. Codec сохраняет их значения и порядок, но может канонизировать
  перечисленные формы.
- Поддержка DTD/entity declarations. Codec отклоняет DTD fail-closed, потому что
  их потеря нарушила бы обещание round-trip.
- Изменение публичных CLI/GUI/report/manifest форматов.

## Инварианты

- Parser никогда не отбрасывает comment или PI внутри либо снаружи document
  element; неизвестные элементы/атрибуты, их порядок, text и tail сохраняются.
- Namespace semantics не зависят от глобального `ET.register_namespace()`:
  исходные qualified names и декларации остаются локальными документу, включая
  повторное использование prefix в разных scopes и несколько prefixes одного
  URI.
- Source profile следует за тем же tree до transaction serialization; plain
  `ElementTree` сохраняет прежний стандартный UTF-8 serialization contract.
- Dedupe удаляет только выбранный duplicate element. Replace-paths меняет только
  целевой `Folder`/`ApplicationPath`. Нецелевой semantic tree до и после равен,
  а контролируемые lexical properties declaration/BOM/EOL/prefixes неизменны.
- Parse/serialization/validation cancellation происходит до commit, не создаёт
  backup при ранней отмене и не оставляет stage/temp.
- Невозможность закодировать изменённое значение в исходном encoding или
  неподдерживаемая конструкция завершается до backup/commit.
- Existing transaction outcome, rollback, manifest и cleanup contracts не
  меняются.

## Фазы, состояния и необратимые границы

Codec работает в `scan` при чтении и в `stage` при сериализации/валидации.
Source profile создаётся только после успешного parse и остаётся immutable;
domain operation меняет DOM, но не lexical profile. Ошибка codec либо отмена в
`scan`/`stage` не пересекает `OperationControl.begin_commit()`. Необратимой
границей остаётся существующий atomic commit transaction executor.

## Этапы реализации

1. Добавить preserving tree/profile и единый bounded parser с comments/PI,
   raw qualified names, declaration/BOM/encoding/EOL и top-level nodes.
2. Научить общий cancellable serializer потреблять source profile, сохранив
   прежние bytes для plain `ElementTree`; направить bypass helper через него.
3. Адаптировать `local_name` и минимальную canonical representation к появлению
   comments/PI без реализации следующего nested/mixed dedupe item.
4. Добавить codec regression matrix, затем end-to-end fixtures dedupe и
   replace-paths с одним целевым изменением и доказательством сохранности
   остального документа.
5. Обновить архитектурный контракт, выполнить focused/full validation и
   повторять review/fix до нулевого списка замечаний.
6. Только после успешной приёмки отметить родительскую задачу `[x]` в
   `ROADMAP.md` и записать фактические результаты проверок в этот план.

## Матрица сценариев

| Сценарий | Ожидаемый результат |
| --- | --- |
| Comment и PI внутри/до/после root | Узлы присутствуют после parse и serialization в прежнем порядке |
| Unknown nested nodes/attributes и Unicode | Значения, порядок, text/tail и attribute order не меняются |
| Два prefixes одного URI и scoped prefix reuse | Qualified names и локальные `xmlns` declarations не переименованы |
| Declaration + UTF-8 BOM + CRLF | Наличие/содержание declaration, BOM и CRLF сохраняются после apply |
| XML без declaration/BOM с LF | Codec не добавляет declaration/BOM и сохраняет LF |
| DTD | Parse завершается fail-closed до mutation artifacts |
| Dedupe одной полной копии | Удалён только duplicate; остальные semantic/lexical sentinels неизменны |
| Replace одного Folder/ApplicationPath | Изменено только выбранное text value; оба документа сохраняют sentinels |
| Большой namespaced XML | Parse/serialize успешны без потери prefixes |
| Отмена внутри большого text/tree/namespace traversal | `cancelled`, commit не начат, XML исходный, temp отсутствуют |
| Plain `ElementTree` | Cancellable bytes совпадают с прежним `ElementTree.write()` contract |

## Критерии приёмки и команды проверки

- Codec matrix: `python -m unittest test.test_xml_round_trip -v`.
- Existing focused read/write: `python -m unittest test.test_xml_repository test.test_safe_write -v`.
- Operation integration: `python -m unittest test.test_dedupe_additional_apps test.test_path_replacement -v`.
- Full: `python -m unittest discover -s test -p "test_*.py" -v`.
- Syntax/import: `python -m compileall -q launchbox_tools launchbox_utils.py test`.
- Hygiene: `git diff --check`.
- Итоговое acceptance review подтверждает 0 Blocker, 0 Regression,
  0 Specification gap и 0 иных замечаний в границах задачи.

## Риски и отдельные follow-up задачи

Raw qualified names намеренно сохраняют prefix spelling вместо перехода к Clark
names внутри DOM. Все domain lookups поэтому обязаны идти через namespace-safe
`local_name`; regression matrix проверяет это на обеих операциях. Полная
семантика nested/mixed canonicalization остаётся следующей P0 и не маскируется
в codec. Потоковая сериализация и ограничение RSS остаются отдельной P2.

## Результат приёмки

Приёмка выполнена 2026-07-16 после пяти review/fix циклов:

- codec/end-to-end matrix: 15 тестов успешно;
- read/write и обе mutation operations: 107 тестов успешно;
- полный discovery: 247 тестов успешно;
- `compileall` и `git diff --check` успешно;
- regression fixtures подтверждают UTF-8/UTF-16, BOM, `LF`/`CRLF`/`CR`,
  comments/PI до, внутри и после root, scoped/multiple prefixes, namespace
  validation, bounded cancellation и отсутствие artifacts до commit;
- итоговый review: 0 Blocker, 0 Regression, 0 Specification gap и 0 иных
  замечаний в границах задачи.
