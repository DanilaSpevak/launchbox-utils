# LaunchBox Audit

Python-скрипт для проверки ROM-файлов LaunchBox и обслуживания записей `AdditionalApplication`.

Настройки путей задаются через файл `launchbox_utils.ini` или параметры командной строки.

Пример файла конфигурации:

```ini
[paths]
launchbox_root = D:\Games\LaunchBox
output_dir = AuditReports
```

В репозитории есть пример `launchbox_utils.example.ini`. Скопируйте его в `launchbox_utils.ini` и укажите свои пути. Реальный `launchbox_utils.ini` добавлен в `.gitignore`, чтобы локальные пути не попадали в GitHub.

## Требования

- Windows.
- Python 3.10 или новее.
- Закрытый LaunchBox перед командами, которые меняют XML.
- Внешние Python-библиотеки не нужны.

## Аудит ROM-файлов

Запуск с настройками по умолчанию:

```powershell
python launchbox_utils.py
```

То же самое явно:

```powershell
python launchbox_utils.py audit
```

С другим путем к LaunchBox:

```powershell
python launchbox_utils.py --root "D:\LaunchBox" audit
```

С другой папкой отчетов:

```powershell
python launchbox_utils.py --output "D:\LaunchBox Reports" audit
```

С другим файлом конфигурации:

```powershell
python launchbox_utils.py --config "D:\Configs\launchbox_utils.ini" audit
```

Создать детальные папки и файлы только для платформ с расхождениями или предупреждениями:

```powershell
python launchbox_utils.py audit --only-with-findings
```

Аудит только читает базу LaunchBox и файловую систему. Он не меняет XML и ROM-файлы.

## Конфигурация

Файл конфигурации по умолчанию:

```text
launchbox_utils.ini
```

Формат:

```ini
[paths]
launchbox_root = D:\Games\LaunchBox
output_dir = AuditReports
```

Правила приоритета:

- `--root` имеет приоритет над `launchbox_root` из конфига.
- Если `--root` не указан, используется `launchbox_root` из `launchbox_utils.ini`.
- `--output` имеет приоритет над `output_dir` из конфига.
- Если `--output` не указан, используется `output_dir` из `launchbox_utils.ini`.
- Относительный `output_dir` считается относительно `launchbox_root`.
- Абсолютный `output_dir` используется как есть.
- Если путь к LaunchBox или output-папка не заданы ни в параметрах, ни в конфиге, скрипт завершится с ошибкой конфигурации.

### Отчеты аудита

Отчеты создаются в папке:

```text
<LaunchBox>\AuditReports
```

Для каждой платформы создается отдельная подпапка:

```text
AuditReports\<PlatformName>\missing_on_disk.txt
AuditReports\<PlatformName>\not_in_database.txt
```

- `missing_on_disk.txt` — записи, которые есть в базе LaunchBox, но файл не найден на диске.
- `not_in_database.txt` — файлы в ROM-папке платформы, которых нет в базе LaunchBox.
- `summary.csv` — сводка по платформам. Файл записывается в формате, удобном для открытия в Excel.

## Поиск дублей Additional Apps

Дублями считаются записи `<AdditionalApplication>`, у которых совпадают:

- `GameID`;
- `ApplicationPath` после нормализации пути.

Dry-run, без изменения XML:

```powershell
python launchbox_utils.py dedupe-additional-apps
```

Dry-run для одной платформы:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"
```

Создать отчеты только для платформ, где найдены дубли или предупреждения:

```powershell
python launchbox_utils.py dedupe-additional-apps --only-with-findings
```

### Отчеты по дублям

После dry-run создаются:

```text
AuditReports\duplicate_additional_apps.csv
AuditReports\<PlatformName>\duplicate_additional_apps.txt
```

Перед удалением дублей рекомендуется открыть эти отчеты и проверить, какие записи будут удалены.

## Удаление дублей Additional Apps

Перед запуском обязательно закройте LaunchBox.

Удалить дубли во всех платформах:

```powershell
python launchbox_utils.py dedupe-additional-apps --apply
```

Удалить дубли только в одной платформе:

```powershell
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision" --apply
```

При `--apply` скрипт:

- создает backup XML-файлов в:

```text
<LaunchBox>\Data\Backups\AdditionalAppsDedupe-<timestamp>
```

- удаляет только дублирующие `<AdditionalApplication>`;
- не удаляет `<Game>`;
- сначала сохраняет XML во временный файл;
- проверяет, что временный XML корректно парсится;
- только после этого заменяет оригинальный XML.

## Тесты

Тесты находятся в папке `test`.

Запуск:

```powershell
python -m unittest -v test\test_launchbox_utils.py
```

Тесты используют временную фиктивную структуру LaunchBox и не меняют реальную базу.

## Структура проекта

`launchbox_utils.py` — короткая точка входа для запуска из командной строки.

Основная логика находится в пакете `launchbox_tools`:

```text
launchbox_tools\
  cli.py                         # команды CLI
  config.py                      # настройки по умолчанию
  models.py                      # dataclass-модели
  paths.py                       # нормализация путей и имен папок
  xml_repository.py              # чтение XML LaunchBox
  safe_write.py                  # backup и безопасная запись XML
  operations\
    audit.py                     # аудит ROM-файлов
    dedupe_additional_apps.py    # поиск и удаление дублей AdditionalApplication
  reports\
    audit_reports.py             # отчеты аудита
    dedupe_reports.py            # отчеты по дублям
```

Новые операции лучше добавлять в `launchbox_tools\operations`, а генерацию отчетов для них — в `launchbox_tools\reports`. CLI и будущий GUI должны только вызывать эти операции, не дублируя бизнес-логику.

## Основные команды

```powershell
# Аудит ROM-файлов
python launchbox_utils.py

# Аудит ROM-файлов, отчеты только с находками
python launchbox_utils.py audit --only-with-findings

# Dry-run поиска дублей AdditionalApplication
python launchbox_utils.py dedupe-additional-apps

# Dry-run поиска дублей, отчеты только с находками
python launchbox_utils.py dedupe-additional-apps --only-with-findings

# Dry-run одной платформы
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision"

# Реальное удаление дублей
python launchbox_utils.py dedupe-additional-apps --apply

# Реальное удаление дублей одной платформы
python launchbox_utils.py dedupe-additional-apps --platform "Watara Supervision" --apply
```
