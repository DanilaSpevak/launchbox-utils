# Ограничение платформенных XML доверенным каталогом

## Результат и предотвращаемый риск

`Data/Platforms.xml` и `Data/Platforms/<PlatformName>.xml` читаются и изменяются
только внутри канонической структуры выбранной установки LaunchBox. Небезопасное
имя, выход за доверенный каталог либо reparse point/junction останавливает всю
операцию до записи пользовательских данных.

## Класс сложности и затрагиваемые подсистемы

Задача cross-cutting: общий path/file I/O, XML repository, transaction executor,
dedupe, replace-paths, CLI, GUI, документация и реальные Windows-тесты.

## Зависимости и архитектурные enablers

Используются существующие mutation lock, `XmlMutation`, transaction flow,
`MutationState`, `MutationOutcome` и граница `begin_commit()`. Новые зависимости
и ADR не требуются.

## In scope

- Строгая проверка имени платформы как одного допустимого Windows-компонента.
- Учет superscript-вариантов `COM¹`–`COM³` и `LPT¹`–`LPT³` как DOS device names.
- Lexical и canonical containment платформенных XML.
- Запрет reparse points, junctions и symlink от канонического корня через `Data`
  до конечного XML.
- Повторная проверка перед backup, stage, каждым commit и rollback.
- Единые fail-closed ошибки в audit, dry-run, apply, CLI и GUI.

## Out of scope

- ROM-пути из `Folder` и `ApplicationPath`.
- Output, reports и backup retention policy.
- Новый WinAPI process safety-check из следующей P0-задачи.
- Полное устранение TOCTOU через handle-relative WinAPI.

## Инварианты

- Небезопасная платформа не пропускается молча: вся операция прекращается.
- Dry-run и apply используют одинаковый набор доверенных XML.
- `Platforms.xml` читается один раз в snapshot; список платформ и изменяемое дерево
  получены из одного набора байтов.
- Платформенный XML проверяется до existence probe, перед parse и после parse.
- Канонический XML является непосредственным потомком ожидаемого каталога.
- После canonical resolve корня компоненты `Data`, `Platforms` и XML не являются
  reparse points; alias/junction на сам выбранный корень допустим.
- Ошибка до запуска не создаёт backup/manifest и остаётся доменным исключением.
  После создания backup операция возвращает `FAILED` либо `PARTIAL`, не разрешает
  новый commit и отражает фактический результат в manifest, CLI, GUI и отчётах.
- Существующие состояния, outcome и cancellation semantics не меняются.

## Фазы, состояния и необратимые границы

Путь проверяется до и после каждого repository read, в начале transaction, перед backup и stage,
непосредственно перед каждым `os.replace` в commit и перед restore в rollback.
Необратимой границей остаётся `begin_commit()`.

## Этапы реализации

1. Добавить валидатор имени, trusted-path guard и доменную ошибку.
2. Перевести repository и операции на безопасные построители путей.
3. Передать trust boundary в `XmlMutation` и повторять проверки в transaction.
4. Добавить локализованные CLI/GUI ошибки без traceback.
5. Обновить документацию, unit, Windows integration и Tk smoke тесты.

## Матрица сценариев

| Сценарий | Результат |
| --- | --- |
| Обычное Unicode-имя | XML остаётся непосредственным потомком `Data/Platforms` |
| Traversal, absolute/UNC, DOS reserved, invalid или слишком длинное имя | Операция прекращается до чтения внешнего файла |
| `COM¹`–`COM³`, `LPT¹`–`LPT³` и варианты с расширением | Операция прекращается как для соответствующего ASCII DOS device name |
| Reparse/junction в `Data`, `Platforms` или XML | Audit, dry-run и apply прекращаются fail-closed |
| Junction появляется после catalog load, но до platform parse | Повторный read guard блокирует внешний XML до backup |
| Junction появляется в dedupe после commit предыдущей платформы | Новые платформы не обрабатываются; операция возвращает `PARTIAL`, а manifest, CLI, GUI и отчёты показывают committed и failed файлы |
| Подмена пути после stage | Commit блокируется; подготовленный apply получает `FAILED` manifest |
| Ошибка GUI до confirmation | Confirmation и worker не запускаются; traceback не показывается |

## Критерии приёмки и команды проверки

- Табличные unit-тесты всех классов имён и canonical containment.
- Sentinel-тесты подтверждают отсутствие чтения и записи вне LaunchBox root.
- Реальные Windows junction-тесты initial/late race и тест повторной pre-commit проверки.
- Тесты dedupe, replace-paths, CLI и GUI error paths.
- Реальный Tk smoke для ошибки до confirmation.
- `python -m unittest discover -s test -p "test_*.py" -v`
- `python -m compileall -q launchbox_tools launchbox_utils.py`
- `git diff --check`
- Отдельное acceptance review без Blocker, Regression и Specification gap.

## Риски и follow-up

Строгая политика блокирует установки, намеренно переносящие `Data` либо
`Data/Platforms` через junction. Если это станет обязательным сценарием, нужен
отдельный контракт явно подтверждённого физического `Data`, а не автоматическое
доверие любому junction.

Pre/post-read guards сужают окно подмены, но не устраняют микрогонку между
последней проверкой и `open`. Полное устранение требует handle-relative WinAPI и
остаётся отдельным follow-up вне этой задачи.
