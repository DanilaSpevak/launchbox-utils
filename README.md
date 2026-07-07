# LaunchBox Utils

Набор утилит на Python для аудита ROM-файлов LaunchBox и обслуживания записей `AdditionalApplication`. Доступны командная строка и графический интерфейс на Tkinter.

> **Disclaimer:** этот проект не связан с LaunchBox, не одобрен и не спонсируется его разработчиками. Используйте утилиту на свой риск.

> Проект разрабатывается с помощью ИИ-ассистента.

Перед операциями, изменяющими XML-базы LaunchBox:

- проверяйте отчёты dry-run и наличие резервных копий;
- учитывайте, что apply автоматически прерывается, если LaunchBox запущен или файлы баз заблокированы другим процессом.



## Возможности

- **Аудит ROM** — сравнение базы LaunchBox с файлами на диске: отсутствующие файлы, лишние файлы в ROM-папке, предупреждения по платформам.
- **Дедупликация дополнительных приложений** — поиск и удаление дублирующих `<AdditionalApplication>` с dry-run по умолчанию, резервным копированием перед apply и автоматической проверкой, что LaunchBox закрыт и XML-файлы не заблокированы.
- **CLI и GUI** — одна и та же логика; GUI сохраняет настройки в `launchbox_utils.ini`.



## Требования

- Windows.
- Python 3.10 или новее **или** готовый nightly exe из [GitHub Releases](https://github.com/DanilaSpevak/launchbox-utils/releases/tag/nightly).
- LaunchBox должен быть закрыт перед apply: утилита проверяет это автоматически и прерывает операцию, если запущены `LaunchBox.exe` / `LaunchBox Big Box.exe` или XML-файлы баз заблокированы другим процессом.



## Скачать готовый exe

Каждую ночь GitHub Actions собирает Windows-сборку и публикует её в pre-release [Nightly build](https://github.com/DanilaSpevak/launchbox-utils/releases/tag/nightly).

1. Скачайте `LaunchBoxUtils-nightly-win64.zip`.
2. Распакуйте папку `LaunchBoxUtils`.
3. Скопируйте `launchbox_utils.example.ini` в `launchbox_utils.ini` рядом с `LaunchBoxUtils.exe` и укажите свои пути.
4. Запустите GUI:

```powershell
.\LaunchBoxUtils.exe gui
```

CLI-команды работают так же, как у Python-версии:

```powershell
.\LaunchBoxUtils.exe audit
.\LaunchBoxUtils.exe dedupe-additional-apps
```

Python на машине не нужен.



## Конфигурация

Настройки задаются через `launchbox_utils.ini` или параметры командной строки.

В репозитории есть пример `launchbox_utils.example.ini`. Скопируйте его в `launchbox_utils.ini` и укажите свои пути.

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


| Параметр         | Описание                                                                                               |
| ---------------- | ------------------------------------------------------------------------------------------------------ |
| `launchbox_root` | Корневая папка LaunchBox.                                                                              |
| `output_dir`     | Папка для отчётов. Относительный путь считается от `launchbox_root`; абсолютный используется как есть. |




### Секция `[interface]`


| Параметр   | Описание                                                                                                                                                                                 |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `language` | Язык GUI: `ru` или `en`. Сохраняется при переключении кнопками `RU` / `EN`. Если параметр не задан или указан неверно, язык выбирается по системной локали (русская → `ru`, иначе `en`). |


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
- Папки можно редактировать вручную, выбрать через кнопку с иконкой папки или открыть в проводнике кнопкой `↗`.
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

Дублями считаются записи `<AdditionalApplication>`, у которых совпадают:

- `GameID` (без учёта регистра);
- путь к файлу после разрешения относительно корня LaunchBox и нормализации (без учёта регистра).

При совпадении сохраняется первая найденная запись, остальные считаются дублями. Записи с пустым `GameID` или `ApplicationPath` пропускаются и попадают в предупреждения отчёта.

### Dry-run (по умолчанию)

```powershell
python launchbox_utils.py dedupe-additional-apps
```

Dry-run для одной платформы:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"
```

Отчёты только для платформ с дублями или предупреждениями:

```powershell
python launchbox_utils.py dedupe-additional-apps --only-with-findings
```



### Apply

Перед `--apply` утилита автоматически проверяет:

- не запущен ли LaunchBox (`LaunchBox.exe`, `LaunchBox Big Box.exe`);
- не заблокированы ли XML-файлы платформ другим процессом.

Если хотя бы одно условие выполняется, операция прерывается с ошибкой. В CLI сообщение выводится в stderr и программа завершается с кодом 1; в GUI показывается диалог.

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

- создаёт резервные копии XML в `<LaunchBox>\Data\Backups\AdditionalAppsDedupe-<timestamp>`;
- удаляет только дублирующие `<AdditionalApplication>`;
- не удаляет `<Game>`;
- записывает XML во временный файл, проверяет парсинг и только после этого заменяет оригинал.

Dry-run и аудит эти проверки не выполняют — они только читают базу.

### Отчёты по дедупликации

```text
<output_dir>\duplicate_additional_apps.csv
<output_dir>\<PlatformName>\duplicate_additional_apps.txt
```

- `duplicate_additional_apps.csv` — сводная таблица (кодировка UTF-8 с BOM, разделитель `;`, первая строка `sep=;` для Excel).
- `duplicate_additional_apps.txt` — детали по платформе: какие записи удалить и какие оставить.

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

## License

Проект распространяется под лицензией [MIT](LICENSE).