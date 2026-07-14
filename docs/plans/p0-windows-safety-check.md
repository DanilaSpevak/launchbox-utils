# Fail-closed Windows safety-check

## Результат и предотвращаемый риск

Apply-операция изменяет XML только после успешной диагностики процессов
`LaunchBox.exe` и `BigBox.exe` и получения эксклюзивного handle каждого целевого
XML. Ошибка, timeout или неразбираемый результат диагностики не трактуются как
«LaunchBox не запущен» и блокируют мутацию до записи пользовательских данных.

## Класс сложности и затрагиваемые подсистемы

Задача cross-cutting: Windows process/file diagnostics, общий transaction
executor, CLI/GUI-ошибки, документация и Windows smoke-проверка. Обязателен
design gate для P0 mutation safety.

## Зависимости и архитектурные enablers

Используются существующие `MutationBlockedError`, `ensure_safe_to_mutate()`,
`OperationControl.begin_commit()` и `execute_xml_transaction()`. Новый ADR и
внешние зависимости не требуются.

## In scope

- Канонические process names `LaunchBox.exe` и `BigBox.exe` без учета регистра.
- Один структурированный CSV-snapshot `tasklist` на process-check без анализа
  локализованных диагностических фраз.
- Ограниченный timeout запуска `tasklist` и fail-closed обработка launch error,
  timeout, ненулевого exit code, ошибки декодирования и malformed/empty CSV.
- `WinDLL("kernel32", use_last_error=True)` и явные `argtypes`/`restype` для
  `CreateFileW` и `CloseHandle`.
- Различение ожидаемых lock/access-denied ошибок и прочих ошибок WinAPI; любые
  неопределенные результаты блокируют apply.
- Повторный общий safety-check после stage и непосредственно перед
  `begin_commit()` без дополнительного cancellable checkpoint между ними.
- Понятный fail-closed outcome в CLI и локализованное сообщение GUI.
- Unit-регрессии и focused Windows smoke текущей реализации.

## Out of scope

- Реальные отдельные процессы с именами `LaunchBox.exe` / `BigBox.exe` в тестах.
- Эксклюзивный WinAPI handle из отдельного test process и управляемый
  access-denied fixture.
- Устранение TOCTOU между последней диагностикой и `os.replace` через
  handle-relative WinAPI.
- Замена `tasklist` на собственный WinAPI process enumerator.
- Изменение mutation lock, manifest, backup или rollback-контрактов.

Эти process-level сценарии принадлежат следующей самостоятельной P0-задаче о
реальных Windows integration-тестах.

## Инварианты

- Успешный пустой process snapshot, сбой команды и timeout не означают
  «LaunchBox закрыт».
- Сравнение process names точное и регистронезависимое; похожие имена не
  блокируют apply.
- `ERROR_ACCESS_DENIED`, `ERROR_SHARING_VIOLATION` и `ERROR_LOCK_VIOLATION`
  означают занятый XML. Неизвестная ошибка WinAPI становится диагностическим
  блокером, а не ложным `unlocked`.
- Windows file probe вызывается и для исчезнувшего target: `ERROR_FILE_NOT_FOUND`
  считается неопределенной диагностикой, поэтому commit не создаёт XML заново.
- Успешно открытый handle всегда закрывается; ошибка `CloseHandle` также
  блокирует apply.
- Последний safety-check выполняется после подготовки всех stage-файлов и до
  необратимой границы `begin_commit()`.
- При сбое последней проверки destination остается неизменным, файл сохраняет
  состояние `prepared`, stage очищается, commit и rollback не запускаются.
- Причина и технические детали позднего `safety_check_failed` сохраняются через
  transaction/domain result, чтобы GUI локализовал итог так же, как preflight.
- Dry-run не получает новых process/file probes; все записи по-прежнему идут
  через `safe_write.py`.

## Фазы, состояния и необратимые границы

Предварительная диагностика выполняется до backup. После backup и stage общий
safety-check повторяется. При его успехе `begin_commit()` атомарно закрывает
возможность отмены и начинается commit; при ошибке операция завершается как
`failed` без XML-записи. Необратимой границей остается `begin_commit()`.

## Этапы реализации

1. Ввести строгий parser process snapshot, timeout и диагностическую ошибку.
2. Исправить имена процессов и WinAPI-сигнатуры file-lock probe.
3. Преобразовать неопределенную диагностику в `MutationBlockedError` с отдельной
   причиной и локализованным GUI-сообщением.
4. Сузить последний cancellable участок перед `begin_commit()` и закрепить
   порядок вызовов тестом.
5. Синхронизировать README, `ARCHITECTURE.md` и roadmap wording.
6. Выполнить unit, полный regression, Windows smoke и итоговое независимое
   acceptance review.

## Матрица сценариев

| Сценарий | Ожидаемый результат |
| --- | --- |
| Snapshot содержит `LaunchBox.exe` | `launchbox_running`, apply запрещен |
| Snapshot содержит `bigbox.EXE` | `launchbox_running`, apply запрещен |
| Snapshot содержит только похожее имя | process-check успешен, проверка продолжается |
| Локализованные значения остальных CSV-полей | parser использует image name/PID, проверка продолжается |
| Timeout, launch error или non-zero exit | `safety_check_failed`, apply запрещен |
| Empty/malformed CSV | `safety_check_failed`, apply запрещен |
| `CreateFileW` возвращает sharing/access error | `files_locked`, apply запрещен |
| `CreateFileW`/`CloseHandle` возвращает неизвестную ошибку | `safety_check_failed`, apply запрещен |
| Target исчез до последнего `CreateFileW` | `prepared`, commit не воссоздаёт XML |
| Последняя проверка после stage завершается ошибкой | destination не изменен, состояние `prepared` |
| Проверки успешны | существующий commit/rollback flow не меняется |

## Критерии приёмки и команды проверки

- `python -m unittest -v test.test_runtime_checks`
- focused pre-commit regression из `test.test_safe_write`
- focused GUI-message regression из `test.test_gui`
- `python -m unittest discover -s test -p "test_*.py" -v`
- `python -m compileall -q launchbox_tools launchbox_utils.py test`
- `git diff --check`
- Windows smoke реального process snapshot и незаблокированного временного XML.
- Независимый review подтверждает отсутствие Blocker/Regression; затем roadmap
  item получает `[x]`.

## Риски и отдельные follow-up задачи

- `tasklist` остается внешней системной командой; fail-closed ограничивает риск
  ложного разрешения, но не устраняет стоимость запуска и TOCTOU.
- Реальные process/file race, access denied и отдельный exclusive-handle process
  проверяются следующей P0-задачей Windows integration-тестов.
- Полное устранение race потребует отдельного handle-based transaction design и
  не входит в текущий scope.
