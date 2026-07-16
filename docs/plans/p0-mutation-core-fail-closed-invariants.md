# Fail-closed инварианты mutation core

## Результат и предотвращаемый риск

До следующих P0-рефакторингов mutation core получает отдельную регрессионную
матрицу для уже существующих fail-closed ветвей. Матрица доказывает, что
некорректный plan, недостоверная trust-конфигурация, повреждённые lock metadata и
ошибки canonical path guard останавливают операцию до commit и не создают скрытых
файловых эффектов.

## Класс сложности и затрагиваемые подсистемы

Задача cross-cutting из-за общей transaction/trust/lock границы, но
test-oriented: основной объём изменений принадлежит regression-тестам
`safe_write`, `paths` и `mutation_lock`. Production-код меняется только если
новый тест воспроизводит нарушение уже принятого fail-closed контракта.

## Зависимости и архитектурные enablers

Используются существующие `XmlMutation`, `XmlTransactionResult`,
`MutationFileResult`, `OperationControl`, `UnsafeDatabasePathError` и
`MutationBlockedError`. Новая state machine, DTO, ADR или общая I/O-абстракция
не требуются. Branch-coverage gate остаётся отдельной задачей P2 и не является
зависимостью этой поведенческой приёмки.

## In scope

- Duplicate и missing destination в transaction preflight.
- Одна заданная половина пары `trusted_parent` / `trust_anchor`.
- Несколько trusted mutations с разными trust anchors.
- Отмена пустой транзакции.
- Повреждённый JSON и поля неверных типов в metadata занятого mutation lock.
- Ошибка `lstat`, ошибка `Path.resolve()` и canonical escape в trusted-path guard.
- Для каждого сценария — явные проверки outcome/exception, file state/error,
  отсутствия commit, изменений sentinel-файлов, backup/workspace и temp-файлов.

## Out of scope

- Измерение общего branch coverage и новый CI gate.
- Snapshot/fingerprint guard, crash journal и XML round-trip codec.
- Handle-based hard-link/swap защита mutation lock.
- Изменение публичных CLI/GUI/report/manifest контрактов.
- Рефакторинг transaction state machine или path guard.

## Инварианты

- Ни один preflight/configuration/path/lock-owner отказ из матрицы не достигает
  `_commit_staged_file` и не меняет пользовательский XML либо внешний sentinel.
- До backup/stage не остаются `Backups`, `.launchbox-utils-work` или `*.tmp`.
- Ошибка конкретного destination помечает этот `MutationFileResult` как
  `FAILED` и записывает его error; уже проверенные и ещё не проверенные entries
  остаются `PLANNED`.
- Ошибка конфигурации транзакции до проверки отдельных destinations возвращает
  `FAILED` с transaction-level error; все file results остаются `PLANNED` без
  выдуманной file-level причины.
- Отмена пустой транзакции возвращает `CANCELLED`, не начинает commit и не
  создаёт file results или артефакты.
- Повреждённые либо типизированно неверные lock metadata не мешают блокировке:
  возвращается `mutation_in_progress`, недостоверные structured owner fields
  равны `None`, а исходные bytes lock-файла не переписываются.
- `lstat`/`resolve` failure и canonical escape завершаются fail-closed через
  `UnsafeDatabasePathError`; canonical escape не разрешает запись по внешнему
  пути.

## Фазы, состояния и необратимые границы

Проверяемые transaction-отказы происходят в preflight, до backup и `STAGE`-I/O.
Пустая отменённая транзакция завершается на первом checkpoint. Lock-owner
metadata читаются только после неуспешного захвата OS lock. Path guard выполняет
lexical, metadata и canonical проверки до любого разрешённого file I/O.
Необратимой границей остаётся `OperationControl.begin_commit()`; ни один
сценарий матрицы её не пересекает.

## Этапы реализации

1. Добавить единый acceptance test module с общими assertions отсутствия
   mutation-артефактов и явной матрицей transaction preflight.
2. Закрепить tolerant-but-typed обработку corrupted/invalid lock owner metadata
   без изменения lock bytes.
3. Закрепить структурированные `lstat`/`resolve` ошибки и canonical escape с
   внешним sentinel.
4. Исправить только воспроизведённые нарушения существующего контракта.
5. Выполнить focused/full validation, итоговое review и только после нулевого
   списка замечаний отметить родительскую задачу `[x]` в `ROADMAP.md`.

## Матрица сценариев

| Сценарий | Ожидаемый результат |
| --- | --- |
| Duplicate destination | `FAILED`; первая запись `PLANNED`, повторная `FAILED` с error; ноль backup/stage/commit |
| Missing destination | `FAILED`; отсутствующая запись `FAILED` с error; ноль backup/stage/commit |
| Только `trusted_parent` либо только `trust_anchor` | transaction `FAILED`; entries `PLANNED`, transaction error; ноль file I/O |
| Разные trust anchors | transaction `FAILED`; entries `PLANNED`, transaction error; оба XML неизменны |
| Пустая транзакция отменена | `CANCELLED`; пустой files, commit не начат, артефактов нет |
| Lock owner содержит повреждённый JSON | `mutation_in_progress`; owner fields `None`; lock bytes неизменны |
| Lock owner содержит поля неверных типов | `mutation_in_progress`; invalid owner fields `None`; lock bytes неизменны |
| `lstat` падает на дочернем path | transaction `FAILED`; file `FAILED`/error; `unsafe_path_error.reason="path_metadata_error"`; sentinel неизменён |
| `resolve` падает при canonical check | transaction `FAILED`; file `FAILED`/error; `unsafe_path_error.reason="path_metadata_error"`; sentinel неизменён |
| Lexical path canonicalizes наружу | transaction `FAILED`; file `FAILED`/error; `unsafe_path_error.reason="outside_trusted_directory"`; внешний sentinel неизменён |

## Критерии приёмки и команды проверки

- Все строки матрицы имеют regression-тест и проходят на поддерживаемой локальной
  Windows-среде без зависимости от привилегий symlink/junction.
- Focused: `python -m unittest test.test_fail_closed_invariants -v`.
- Full: `python -m unittest discover -s test -p "test_*.py" -v`.
- Syntax/import: `python -m compileall -q launchbox_tools launchbox_utils.py test`.
- Hygiene: `git diff --check`.
- Итоговое acceptance review не содержит Blocker, Regression или Specification gap.

## Риски и отдельные follow-up задачи

Mocked canonicalization tests доказывают реакцию текущего path guard, но не
устраняют TOCTOU между path-based check и open. Этот риск уже принадлежит
отдельной P0-задаче handle-based hard-link/swap защиты. Общий количественный
coverage gate остаётся в P2.

## Результат приёмки

Приёмка выполнена 2026-07-16 после нескольких review/fix циклов:

- acceptance/lock matrix: 13 тестов успешно;
- полный discovery: 232 теста успешно;
- `compileall` и whitespace/diff hygiene успешно;
- валидный lock owner отдельно проверен на отсутствие регрессии после typed
  filtering;
- итоговый review: 0 Blocker, 0 Regression, 0 Specification gap, 0 новых
  замечаний.
