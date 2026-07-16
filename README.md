## English

# LaunchBox Utils

> **Disclaimer:** this project is not affiliated with, endorsed by, or sponsored by the LaunchBox developers. Use the utility at your own risk.

> This project is developed with the help of AI assistants

[Launchbox](https://www.launchbox-app.com/) is a great frontend for managing a huge collection of ROM files and emulators! Over time, however, you realize that some features are missing, and floating bugs start getting in the way — bugs that are often hard to notice when you import entire ROM sets at once. Fortunately, LaunchBox databases are convenient XML files for analysis and editing.

This project is a set of Python scripts that extend LaunchBox's built-in functionality. They can be invoked from the command line or through a graphical interface.

## Features

- **Database vs. folder discrepancy audit** — compare LaunchBox databases with files on disk: missing files, extra files in the ROM folder, platform warnings.
- **Additional application deduplication** — LaunchBox used to duplicate additional applications when merging games, and it may still do so. The script lets you bulk-clean such duplicates. To avoid rash actions, an analysis mode is provided that generates per-platform reports.
- **ROM path replacement** — bulk-replace old ROM path prefixes after moving files. Absolute database paths stay absolute; relative database paths are rewritten relative to the LaunchBox folder.



## Requirements

- Windows.
- Python 3.10 or newer (not required for the pre-built exe from [GitHub Releases](https://github.com/DanilaSpevak/launchbox-utils/releases/tag/nightly)).



## Download the pre-built exe

Every night, GitHub Actions builds a Windows release and publishes it as a pre-release [Nightly build](https://github.com/DanilaSpevak/launchbox-utils/releases/tag/nightly).

1. Download `LaunchBoxUtils-nightly-win64.zip`.
2. Extract the `LaunchBoxUtils` folder.
3. Launch the GUI by double-clicking `LaunchBoxUtils.exe` or from the command line:

```powershell
.\LaunchBoxUtils.exe
.\LaunchBoxUtils.exe gui
```

Run CLI commands via `LaunchBoxUtils-cli.exe` — it has a console for output. Without arguments it shows help:

```powershell
.\LaunchBoxUtils-cli.exe audit
.\LaunchBoxUtils-cli.exe dedupe-additional-apps
```



## Configuration

Settings are provided via `launchbox_utils.ini` or command-line parameters.

The repository includes an example `launchbox_utils.example.ini`. Copy it to `launchbox_utils.ini` and set your paths or select them via GUI.

Default configuration file:

```text
launchbox_utils.ini
```

Format:

```ini
[paths]
launchbox_root = D:\Games\LaunchBox
output_dir = AuditReports

[interface]
language = ru
```



### Section `[paths]`


| Parameter        | Description                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------ |
| `launchbox_root` | LaunchBox root folder.                                                                                 |
| `output_dir`     | Folder for reports. A relative path is resolved from `launchbox_root`; an absolute path is used as-is. |




### Section `[interface]`


| Parameter  | Description                                                                                                                                                                                            |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `language` | GUI language: `ru` or `en`. Saved when switching with the `RU` / `EN` buttons. If the parameter is missing or invalid, the language is chosen from the system locale (Russian → `ru`, otherwise `en`). |


The CLI does not use `[interface]` — only the GUI.

### Settings priority

- `--config` — path to the INI file (default: `launchbox_utils.ini` next to the exe, or in the project root when running from source).
- `--root` takes priority over `launchbox_root` from the config.
- `--output` takes priority over `output_dir` from the config.
- If the LaunchBox path or report folder is not set in either parameters or config, the program exits with a configuration error.



## Graphical interface

Launch:

```powershell
python launchbox_utils.py gui
```

With a different configuration file:

```powershell
python launchbox_utils.py --config "D:\Configs\launchbox_utils.ini" gui
```



### Folders and language

- The **LaunchBox folder** and **Output folder** fields are read from `launchbox_utils.ini`.
- Folders can be edited manually, selected via the `...` button, or opened in Explorer with the `↗` button.
- Path changes are automatically saved to `launchbox_utils.ini` (section `[paths]`).
- The interface language is switched with the `RU` and `EN` buttons and saved in `[interface] language`.



### Operation groups

**Audit** — operations that do not modify XML:

- **Full output** and **Only findings** — report writing mode. In "only findings" mode, detailed files are created only for platforms with findings; old reports for "clean" platforms are removed.
- **Find missing files** — ROM audit.
- **Find duplicates in additional apps** — deduplication dry-run.

The report output mode applies to both audit and deduplication dry-run.

**Edit** — operations that modify the database:

- **Remove additional app duplicates** — deduplication apply mode. Before starting, the utility checks that LaunchBox is not running and platform XML files are not locked; if either check fails, an error is shown and the operation does not start. Confirmation is then requested.

Single-platform filtering (`--platform`) is not available in the GUI — CLI only.

### Logs and execution

- Operations run in a background thread; the form is not blocked.
- Results are shown in the log field; the log can be cleared with **Clear logs**.



## ROM file audit

The audit only reads the LaunchBox database and file system. XML and ROM files are not modified.

For each platform in `Data/Platforms.xml`, the utility:

- reads `<Game>` and `<AdditionalApplication>` from `Data/Platforms/<PlatformName>.xml`;
- recursively scans the platform ROM folder (`Folder` in platform metadata);
- compares paths with case-insensitive Windows path normalization;
- collects warnings (empty ROM folder, missing folder, scan errors, entries without `ApplicationPath`, etc.).

All operations treat platform database paths as a security boundary. A platform name
must be one valid Windows filename component, and its XML must remain directly under
`Data/Platforms`. Invalid names, traversal, absolute or UNC paths, DOS reserved names,
and junctions, symbolic links, or other reparse points inside the `Data` chain abort
the complete operation. A junction used only to select the LaunchBox root is allowed.

Run with default settings:

```powershell
python launchbox_utils.py
```

Same, explicitly:

```powershell
python launchbox_utils.py audit
```

With a different LaunchBox path:

```powershell
python launchbox_utils.py --root "D:\LaunchBox" audit
```

With a different report folder:

```powershell
python launchbox_utils.py --output "D:\LaunchBox Reports" audit
```

With a different configuration file:

```powershell
python launchbox_utils.py --config "D:\Configs\launchbox_utils.ini" audit
```

Reports only for platforms with discrepancies or warnings:

```powershell
python launchbox_utils.py audit --only-with-findings
```



### Audit reports

Reports are created in the configured `output_dir` folder (default: `<LaunchBox>\AuditReports`).

```text
<output_dir>\summary.csv
<output_dir>\<PlatformName>\missing_on_disk.txt
<output_dir>\<PlatformName>\not_in_database.txt
<output_dir>\<PlatformName>\warnings.txt
```

- `summary.csv` — platform summary (UTF-8 with BOM encoding, `;` delimiter, first line `sep=;` for Excel).
- `missing_on_disk.txt` — database entries whose file was not found on disk.
- `not_in_database.txt` — files in the ROM folder that are not in the database.
- `warnings.txt` — created in `--only-with-findings` mode when a platform has only warnings and no file discrepancies.

Platform subfolder names are sanitized of characters invalid in Windows file names.

## Additional Apps deduplication

The primary key for `<AdditionalApplication>` entries is:

- `GameID` (case-insensitive);
- file path after resolving relative to the LaunchBox root and normalizing (case-insensitive).

Logical groups connect records with the same primary key or the same complete multisets of normalized direct `GameID` and `ApplicationPath` values. Automatic removal is limited to entries under the same immediate XML parent whose complete namespace-aware XML content is canonically equal inside one logical group. Entries under different parents are preserved as `#parent` ambiguity. Field order, XML 1.0 formatting whitespace (`space`, `tab`, `CR`, `LF`) between immediate fields, known boolean casing, `GameID` casing, and equivalent path spelling are normalized. NBSP and other Unicode whitespace remain significant at mixed-content and parent-tail boundaries; an entry with significant parent tail is preserved and reported as `#parent-content` regardless of its position. Except for documented domain normalization, whitespace inside field and attribute values remains significant. Every other field remains significant, including `Name`, `CommandLine`, autorun flags, emulator settings, attributes, nested data, and unknown future fields.

Groups with the same `GameID` and path but different canonical content are reported as ambiguous and left for manual review. If a group contains `A, A, B`, only the repeated `A` is removable; one `A` and `B` remain. Entries with an empty `GameID` or `ApplicationPath` are skipped and appear in the report warnings.

### Dry-run (default)

```powershell
python launchbox_utils.py dedupe-additional-apps
```

Dry-run for a single platform:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"
```

Reports only for platforms with duplicates, ambiguous groups, or warnings:

```powershell
python launchbox_utils.py dedupe-additional-apps --only-with-findings
```



### Apply

Before `--apply`, the utility automatically checks:

- whether LaunchBox is running (`LaunchBox.exe`, `BigBox.exe`);
- whether platform XML files are locked by another process.

If either condition is true, or Windows cannot complete the process/file diagnostics within the safety timeout, the operation fails closed without changing XML. In the CLI the message goes to stderr and the program exits with code 1; in the GUI a dialog is shown. The checks run again after staging, immediately before commit.

```powershell
python launchbox_utils.py dedupe-additional-apps --apply
```

Example CLI error:

```text
LaunchBox operation aborted: LaunchBox is running. Close LaunchBox before modifying database files.
```

Single platform only:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision" --apply
```

On successful `--apply`, the utility:

- creates XML backups in numbered subdirectories under `<LaunchBox>\Data\Backups\AdditionalAppsDedupe-<timestamp>[-N]`;
- removes only duplicate `<AdditionalApplication>` entries;
- does not remove `<Game>` entries;
- writes XML to a temporary file, validates parsing, and only then replaces the original.

Each platform XML is committed independently. If some files are committed and another fails, the operation reports `partial`; failed files remain unchanged or are restored from backup. CLI exits with code 1 for `partial`, `failed`, and `rolled_back` outcomes.

Every planned XML change has an explicit state: `planned` in dry-run, `prepared` after backup and staged XML validation, `committed` only after atomic replacement, `failed` when a step for that file fails, and `rolled_back` after a committed file is restored. `applied` is not emitted as a separate flag.

Each apply run atomically reserves its timestamped backup directory. Same-second collisions receive `-2`, `-3`, and later numeric suffixes. A final `manifest.json` records the overall outcome, every affected XML file, backup paths, errors, and the state of each duplicate removal. If the XML commit succeeds but the manifest cannot be written, the mutation remains `success`, the manifest error is reported separately, and CLI exits with code 1.

Dry-run and audit do not perform these checks — they only read the database.

### Deduplication reports

```text
<output_dir>\duplicate_additional_apps.csv
<output_dir>\<PlatformName>\duplicate_additional_apps.txt
```

- `duplicate_additional_apps.csv` — summary table with `duplicate` and `ambiguous` finding types plus the mutation `state` (UTF-8 with BOM encoding, `;` delimiter, first line `sep=;` for Excel).
- `duplicate_additional_apps.txt` — per-platform details: which canonical duplicates can be removed, their states, and which ambiguous variants must be kept.

Review dry-run reports before apply.

## ROM path replacement

Use this after moving ROM files from one absolute path to another. The old path does not need to exist. The operation updates `<Game><ApplicationPath>`, `<AdditionalApplication><ApplicationPath>`, and platform ROM folders in `Data/Platforms.xml`.

The match is path-prefix based with a real path boundary, so `D:\ROM` does not match `D:\ROMs`. If the original database value was absolute, the replacement is written as an absolute path. If it was relative, the replacement is written relative to the LaunchBox root.

Dry-run:

```powershell
python launchbox_utils.py replace-paths --old "D:\OldRoms" --new "E:\NewRoms"
```

Apply:

```powershell
python launchbox_utils.py replace-paths --old "D:\OldRoms" --new "E:\NewRoms" --apply
```

Single platform and finding-only reports are also supported:

```powershell
python launchbox_utils.py replace-paths --old "D:\OldRoms" --new "E:\NewRoms" --platform "Watara Supervision"
python launchbox_utils.py replace-paths --old "D:\OldRoms" --new "E:\NewRoms" --only-with-findings
```

Before apply, the same LaunchBox process and XML lock checks are performed as for deduplication. All affected XML files form one transaction: every document is validated, backed up, and staged before commit. Each XML backup is stored in its own numbered subdirectory under `<LaunchBox>\Data\Backups\PathReplacement-<timestamp>[-N]`. If a later replacement fails, already replaced files are restored from the matching backup and the operation reports `rolled_back`; restored replacements have state `rolled_back`, the failing file has state `failed`, and files not yet committed remain `prepared`.

Apply also writes `<LaunchBox>\Data\Backups\PathReplacement-<timestamp>[-N]\manifest.json`, including runs that find no changes.

Reports:

```text
<output_dir>\path_replacements.csv
<output_dir>\<PlatformName>\path_replacements.txt
```

- `path_replacements.csv` — summary table with old/new values, entry type, XML path, mode, mutation state, backup path, manifest details, errors, and warnings.
- `path_replacements.txt` — per-platform details and state for every planned, prepared, committed, failed, or rolled-back replacement.

## Main CLI commands

```powershell
# GUI
python launchbox_utils.py gui

# ROM file audit
python launchbox_utils.py

# Audit, reports only with findings
python launchbox_utils.py audit --only-with-findings

# AdditionalApplication deduplication dry-run
python launchbox_utils.py dedupe-additional-apps

# Dry-run, reports only with findings
python launchbox_utils.py dedupe-additional-apps --only-with-findings

# Dry-run for a single platform
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"

# Remove duplicates
python launchbox_utils.py dedupe-additional-apps --apply

# Remove duplicates for a single platform
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision" --apply

# Path replacement dry-run
python launchbox_utils.py replace-paths --old "D:\OldRoms" --new "E:\NewRoms"

# Apply path replacements
python launchbox_utils.py replace-paths --old "D:\OldRoms" --new "E:\NewRoms" --apply
```



## Tests

Tests are in the `test` folder.

Run:

```powershell
python -m unittest discover -s test -p "test_*.py" -v
```

Tests use a temporary mock LaunchBox structure and do not modify a real database.

## Text encoding

Markdown sources, the roadmap, and user-facing GUI/CLI/documentation text are stored as UTF-8. Edit and commit these files as UTF-8.

CSV reports are the exception: they are written as UTF-8 with BOM for better Excel compatibility, as noted in the report descriptions above.

## License

This project is distributed under the [MIT](LICENSE) license.

---



## Russian



# LaunchBox Utils

> **Disclaimer:** этот проект не связан с LaunchBox, не одобрен и не спонсируется его разработчиками. Используйте утилиту на свой страх и риск.

> Проект разрабатывается с помощью ИИ-ассистентов

[Launchbox](https://www.launchbox-app.com/)- отличная оболочка для управления огромной коллекцией ROM-файлов и эмуляторов! Однако со временем понимаешь, что каких-то функций в нем не хватает, а где-то начинают мешаться плавающие баги, которые зачастую сложно заметить, когда грузишь ромсеты целиком. К счастью, базы Launchbox представляют собой удобные для анализа и редактирования XML-файлы.

Этот проект - набор скриптов на Python, дополняющих штатную функциональность Launchbox. Их можно вызывать из командной строки либо использовать графический интерфейс.

## Возможности

- **Аудит расхождений между базами и папками** — сравнение баз LaunchBox с файлами на диске: отсутствующие файлы, лишние файлы в ROM-папке, предупреждения по платформам.
- **Дедупликация дополнительных приложений** — раньше Launchbox очень любил при объединении игр дублировать дополнительные приложения, а может и сейчас любит. Скрипт позволяет массово очистить такие дубли. Чтобы не рубить сплеча, предусмотрен режим анализа, в котором формируются отчеты по платформам.
- **Массовая замена путей ROM** — обновление старых префиксов путей после переноса файлов. Абсолютные пути остаются абсолютными, относительные пересчитываются от папки LaunchBox.



## Требования

- Windows.
- Python 3.10 или новее (не нужен для готовой сборки exe из [GitHub Releases](https://github.com/DanilaSpevak/launchbox-utils/releases/tag/nightly)).



## Скачать готовый exe

Каждую ночь GitHub Actions собирает Windows-сборку и публикует её в pre-release [Nightly build](https://github.com/DanilaSpevak/launchbox-utils/releases/tag/nightly).

1. Скачайте `LaunchBoxUtils-nightly-win64.zip`.
2. Распакуйте папку `LaunchBoxUtils`.
3. Запустите GUI двойным щелчком по `LaunchBoxUtils.exe` или из командной строки:

```powershell
.\LaunchBoxUtils.exe
.\LaunchBoxUtils.exe gui
```

CLI-команды запускайте через `LaunchBoxUtils-cli.exe` — у него есть консоль для вывода. Без аргументов он показывает справку:

```powershell
.\LaunchBoxUtils-cli.exe audit
.\LaunchBoxUtils-cli.exe dedupe-additional-apps
```



## Конфигурация

Настройки задаются через `launchbox_utils.ini` или параметры командной строки.

В репозитории есть пример `launchbox_utils.example.ini`. Скопируйте его в `launchbox_utils.ini` и укажите свои пути либо выберите их через графический интерфейс.

Файл конфигурации по умолчанию:

```text
launchbox_utils.ini
```

Формат:

```ini
[paths]
launchbox_root = D:\Games\LaunchBox
output_dir = AuditReports

[interface]
language = ru
```



### Секция `[paths]`


| Параметр         | Описание                                                                                              |
| ---------------- | ----------------------------------------------------------------------------------------------------- |
| `launchbox_root` | Корневая папка LaunchBox.                                                                             |
| `output_dir`     | Папка для отчётов. Относительный путь считается от`launchbox_root`; абсолютный используется как есть. |




### Секция `[interface]`


| Параметр   | Описание                                                                                                                                                                                |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `language` | Язык GUI:`ru` или `en`. Сохраняется при переключении кнопками `RU` / `EN`. Если параметр не задан или указан неверно, язык выбирается по системной локали (русская → `ru`, иначе `en`). |


CLI не использует `[interface]` — только GUI.

### Приоритет настроек

- `--config` — путь к INI-файлу (по умолчанию `launchbox_utils.ini` рядом с exe или в корне проекта при запуске из исходников).
- `--root` имеет приоритет над `launchbox_root` из конфига.
- `--output` имеет приоритет над `output_dir` из конфига.
- Если путь к LaunchBox или папка отчётов не заданы ни в параметрах, ни в конфиге, программа завершится с ошибкой конфигурации.



## Графический интерфейс

Запуск:

```powershell
python launchbox_utils.py gui
```

С другим файлом конфигурации:

```powershell
python launchbox_utils.py --config "D:\Configs\launchbox_utils.ini" gui
```



### Папки и язык

- Поля **LaunchBox folder** и **Output folder** читаются из `launchbox_utils.ini`.
- Папки можно редактировать вручную, выбрать через кнопку `...` или открыть в проводнике кнопкой `↗`.
- Изменения путей автоматически сохраняются в `launchbox_utils.ini` (секция `[paths]`).
- Язык интерфейса переключается кнопками `RU` и `EN` и сохраняется в `[interface] language`.



### Группы операций

**Audit / Аудит** — операции без изменения XML:

- **Full output / Полный вывод** и **Only findings / Только расхождения** — режим записи отчётов. В режиме «только расхождения» детальные файлы создаются только для платформ с находками; у «чистых» платформ старые отчёты удаляются.
- **Find missing files / Найти отсутствующие файлы** — аудит ROM.
- **Find duplicates in additional apps / Найти дубли в дополнительных приложениях** — dry-run дедупликации.

Режим вывода отчётов применяется и к аудиту, и к dry-run дедупликации.

**Edit / Редактирование** — операции, изменяющие базу:

- **Remove additional app duplicates / Удалить дубли дополнительных приложений** — apply-режим дедупликации. Перед запуском утилита проверяет, что LaunchBox не запущен и XML-файлы не заблокированы; при нарушении показывается ошибка и операция не начинается. Затем запрашивается подтверждение.

Фильтр по одной платформе (`--platform`) в GUI недоступен — только через CLI.

### Логи и выполнение

- Операции выполняются в фоновом потоке, форма не блокируется.
- Результаты выводятся в поле логов; лог можно очистить кнопкой **Clear logs / Очистить логи**.



## Аудит ROM-файлов

Аудит только читает базу LaunchBox и файловую систему. XML и ROM-файлы не изменяются.

Для каждой платформы из `Data/Platforms.xml` утилита:

- читает `<Game>` и `<AdditionalApplication>` из `Data/Platforms/<PlatformName>.xml`;
- рекурсивно сканирует ROM-папку платформы (`Folder` в метаданных платформы);
- сравнивает пути с учётом регистра и нормализации Windows-путей;
- собирает предупреждения (пустая ROM-папка, отсутствующая папка, ошибки сканирования, записи без `ApplicationPath` и т. п.).

Все операции считают пути платформенных баз границей безопасности. Имя платформы
должно быть одним допустимым компонентом имени файла Windows, а XML должен оставаться
непосредственно внутри `Data/Platforms`. Недопустимые имена, traversal, абсолютные и
UNC-пути, зарезервированные DOS-имена, а также junction, символические ссылки и другие
reparse points внутри цепочки `Data` прерывают всю операцию. Junction, через который
выбран только корень LaunchBox, допускается.

Запуск с настройками по умолчанию:

```powershell
python launchbox_utils.py
```

То же самое явно:

```powershell
python launchbox_utils.py audit
```

С другим путём к LaunchBox:

```powershell
python launchbox_utils.py --root "D:\LaunchBox" audit
```

С другой папкой отчётов:

```powershell
python launchbox_utils.py --output "D:\LaunchBox Reports" audit
```

С другим файлом конфигурации:

```powershell
python launchbox_utils.py --config "D:\Configs\launchbox_utils.ini" audit
```

Отчёты только для платформ с расхождениями или предупреждениями:

```powershell
python launchbox_utils.py audit --only-with-findings
```



### Отчёты аудита

Отчёты создаются в настроенной папке `output_dir` (по умолчанию `<LaunchBox>\AuditReports`).

```text
<output_dir>\summary.csv
<output_dir>\<PlatformName>\missing_on_disk.txt
<output_dir>\<PlatformName>\not_in_database.txt
<output_dir>\<PlatformName>\warnings.txt
```

- `summary.csv` — сводка по платформам (кодировка UTF-8 с BOM, разделитель `;`, первая строка `sep=;` для Excel).
- `missing_on_disk.txt` — записи в базе, файл на диске не найден.
- `not_in_database.txt` — файлы в ROM-папке, которых нет в базе.
- `warnings.txt` — создаётся в режиме `--only-with-findings`, если у платформы есть только предупреждения без расхождений по файлам.

Имена подпапок платформ очищаются от символов, недопустимых в именах файлов Windows.

## Дедупликация Additional Apps

Основной ключ записей `<AdditionalApplication>` состоит из следующих полей:

- `GameID` (без учёта регистра);
- путь к файлу после разрешения относительно корня LaunchBox и нормализации (без учёта регистра).

Логическая группа связывает записи с одинаковым основным ключом либо с одинаковыми полными мультимножествами нормализованных непосредственных значений `GameID` и `ApplicationPath`. Автоматически удаляются только записи под одним непосредственным XML-родителем с канонически одинаковым полным namespace-aware XML-содержимым внутри одной логической группы. Записи под разными родителями сохраняются как ambiguity `#parent`. Нормализуются порядок непосредственных полей, только XML-пробелы `space` / `tab` / `CR` / `LF` между ними, регистр известных булевых значений и `GameID`, а также эквивалентное написание пути. NBSP и другие Unicode-пробелы остаются значимыми в mixed content и parent tail; запись со значимым tail сохраняется и отмечается `#parent-content` независимо от своей позиции. За пределами явно описанной доменной нормализации пробелы внутри значений полей и атрибутов остаются значимыми. Все остальные поля значимы, включая `Name`, `CommandLine`, флаги автозапуска, настройки эмулятора, атрибуты, вложенные данные и неизвестные будущие поля.

Группы с одинаковыми `GameID` и путём, но разным каноническим содержимым отмечаются как ambiguous и остаются для ручного решения. Для группы `A, A, B` удалению подлежит только повторный `A`; по одному варианту `A` и `B` сохраняются. Записи с пустым `GameID` или `ApplicationPath` пропускаются и попадают в предупреждения отчёта.

### Dry-run (по умолчанию)

```powershell
python launchbox_utils.py dedupe-additional-apps
```

Dry-run для одной платформы:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"
```

Отчёты только для платформ с дублями, ambiguous-группами или предупреждениями:

```powershell
python launchbox_utils.py dedupe-additional-apps --only-with-findings
```



### Apply

Перед `--apply` утилита автоматически проверяет:

- не запущен ли LaunchBox (`LaunchBox.exe`, `BigBox.exe`);
- не заблокированы ли XML-файлы платформ другим процессом.

Если хотя бы одно условие выполняется либо Windows не может завершить диагностику процессов/файлов за отведённое время, операция блокируется по принципу fail-closed без изменения XML. В CLI сообщение выводится в stderr и программа завершается с кодом 1; в GUI показывается диалог. После подготовки временных файлов проверки повторяются непосредственно перед commit.

```powershell
python launchbox_utils.py dedupe-additional-apps --apply
```

Пример ошибки в CLI:

```text
LaunchBox operation aborted: LaunchBox is running. Close LaunchBox before modifying database files.
```

Только одна платформа:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision" --apply
```

При успешном `--apply` утилита:

- создаёт резервные копии XML в нумерованных подкаталогах `<LaunchBox>\Data\Backups\AdditionalAppsDedupe-<timestamp>[-N]`;
- удаляет только дублирующие `<AdditionalApplication>`;
- не удаляет `<Game>`;
- записывает XML во временный файл, проверяет парсинг и только после этого заменяет оригинал.

XML каждой платформы фиксируется независимо. Если часть файлов записана, а другой файл завершился ошибкой, операция возвращает `partial`; неуспешные файлы остаются без изменений или восстанавливаются из backup. Для `partial`, `failed` и `rolled_back` CLI завершается с кодом 1.

Каждое изменение XML имеет явное состояние: `planned` в dry-run, `prepared` после создания backup и проверки staged XML, `committed` только после atomic replace, `failed` при ошибке шага и `rolled_back` после успешного восстановления уже записанного файла. Отдельный флаг `applied` больше не используется.

Каждый apply атомарно резервирует timestamp-каталог backup; коллизии в одну секунду получают суффиксы `-2`, `-3` и далее. В каталоге создаётся итоговый `manifest.json` с outcome операции, состояниями XML и отдельных удалений, путями backup и ошибками. Ошибка записи manifest показывается отдельно, не изменяет фактический outcome XML-мутации и приводит к CLI exit code 1.

Dry-run и аудит эти проверки не выполняют — они только читают базу.

### Отчёты по дедупликации

```text
<output_dir>\duplicate_additional_apps.csv
<output_dir>\<PlatformName>\duplicate_additional_apps.txt
```

- `duplicate_additional_apps.csv` — сводная таблица с типами находок `duplicate` и `ambiguous` и состоянием мутации `state` (кодировка UTF-8 с BOM, разделитель `;`, первая строка `sep=;` для Excel).
- `duplicate_additional_apps.txt` — детали по платформе: какие канонические дубли можно удалить, их состояния и какие ambiguous-варианты необходимо оставить.

Перед apply рекомендуется просмотреть отчёты dry-run.

## Основные команды CLI

```powershell
# GUI
python launchbox_utils.py gui

# Аудит ROM-файлов
python launchbox_utils.py

# Аудит, отчёты только с находками
python launchbox_utils.py audit --only-with-findings

# Dry-run дедупликации AdditionalApplication
python launchbox_utils.py dedupe-additional-apps

# Dry-run, отчёты только с находками
python launchbox_utils.py dedupe-additional-apps --only-with-findings

# Dry-run одной платформы
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"

# Удаление дублей
python launchbox_utils.py dedupe-additional-apps --apply

# Удаление дублей одной платформы
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision" --apply
```



## Тесты

Тесты находятся в папке `test`.

Запуск:

```powershell
python -m unittest discover -s test -p "test_*.py" -v
```

Тесты используют временную фиктивную структуру LaunchBox и не меняют реальную базу.

## Кодировка текстов

Markdown-файлы, roadmap и пользовательские тексты GUI/CLI/документации хранятся в UTF-8. Редактируйте и коммитьте эти файлы как UTF-8.

CSV-отчёты являются исключением: они записываются как UTF-8 с BOM для лучшей совместимости с Excel, как указано в описании отчётов выше.

## License

Проект распространяется под лицензией [MIT](LICENSE).
