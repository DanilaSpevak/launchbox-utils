# Консервативная дедупликация вложенного и mixed XML

## Результат и предотвращаемый риск

Дедупликация `AdditionalApplication` автоматически удаляет только записи,
эквивалентные по полному XML-контракту. Перестановка вложенных узлов, изменение
mixed content, `text`/`tail`, comments либо processing instructions создаёт
отдельный вариант и переводит группу в ambiguous вместо удаления данных.

## Класс сложности и затрагиваемые подсистемы

Задача является P0 design-gate изменением XML-mutation поведения, но не
cross-cutting рефакторингом. Меняются только canonicalization в
`operations/dedupe_additional_apps.py`, dedupe fixtures/tests, архитектурное
описание контракта и статус roadmap. Transaction executor, preserving codec,
CLI/GUI, reports и manifest продолжают потреблять прежние модели результата.

## Зависимости и архитектурные enablers

Обязательный enabler — принятый preserving XML round-trip контракт. Он уже
владеет сохранением порядка, `text`/`tail`, unknown nodes, comments и PI.
Текущая задача только определяет, какие сохранённые DOM-различия считаются
эквивалентными при dedupe; новый parser или serializer не создаётся.

## In scope

- Игнорировать порядок только у непосредственных обычных element-полей
  `AdditionalApplication`.
- Сохранять порядок children, точные `text` и `tail` внутри каждого поля и на
  всех более глубоких уровнях.
- Считать comments и PI значимыми узлами с сохранением их последовательности и
  payload; на корневом уровне их наличие делает последовательность содержимого
  order-sensitive.
- Сохранять прежнюю leaf-нормализацию `GameID`, `ApplicationPath` и известных
  boolean-полей, включая каждое повторяющееся поле отдельно.
- Игнорировать только formatting-only whitespace между непосредственными
  полями `AdditionalApplication`.
- Исправить test fixture builder, чтобы каждый переданный параметр попадал в
  сгенерированный XML.
- Проверить dry-run и apply на mixed content, повторяющихся полях, nested order,
  comments/PI и отсутствии удаления ambiguous-вариантов.

## Out of scope

- Изменения preserving parser/serializer и lexical round-trip профиля.
- Изменения группировочного ключа `GameID + normalized ApplicationPath`.
- Snapshot/fingerprint guard, hard-link/handle hardening и crash recovery.
- Новые outcomes, поля manifest/report либо пользовательские тексты CLI/GUI.
- Byte-for-byte сравнение XML или изменение принятой семантики attribute order.

## Инварианты

- Автоматическое удаление возможно только при полном совпадении canonical
  signature внутри одной существующей dedupe-группы.
- Непосредственные обычные поля являются multiset: их порядок незначим, но
  количество и полные signatures каждого повторения значимы.
- Вложенные children всегда являются sequence; сортировка ниже корня
  `AdditionalApplication` запрещена.
- У вложенного/mixed XML точные `text` и `tail`, comments и PI значимы.
- Нормализация доменных leaf-полей не применяется к полю с children: malformed
  либо future mixed content сравнивается консервативно и буквально.
- Formatting-only root whitespace не создаёт ложную ambiguous-группу.
- Dry-run не меняет XML; apply удаляет только найденные canonical duplicates и
  сохраняет существующие transaction, backup, manifest и cancellation правила.
- Canonical traversal сохраняет существующий checkpoint bound не реже одного
  checkpoint на 256 анализируемых узлов.

## Фазы, состояния и необратимые границы

Изменение работает только в существующей фазе `scan`, до backup/stage/commit.
Состояния `planned`, `prepared`, `committed`, `failed` и `rolled_back` не
меняются. Необратимой границей остаётся `OperationControl.begin_commit()` в
transaction executor. Cancellation во время canonical traversal по-прежнему
останавливает scan до любых mutation artifacts.

## Этапы реализации

1. Разделить canonicalization корня `AdditionalApplication` и рекурсивного
   subtree: сортировать только непосредственные обычные поля, рекурсию оставить
   упорядоченной.
2. Ограничить доменную нормализацию leaf-полями и сохранить точные nested
   `text`/`tail`, comments/PI и order-sensitive root content.
3. Согласовать ambiguity diagnostics с новым signature, чтобы различие nested
   поля называло это поле, а root comments/PI не давали пустой diagnostic.
4. Исправить fixture builder и его вызовы; добавить прямую проверку применения
   обоих title-параметров.
5. Расширить conservative fixture переставленными nested repeated nodes и
   comments/PI; проверить dry-run и apply, включая точное число сохранённых
   вариантов.
6. Обновить архитектурный контракт, выполнить focused/full validation и
   повторять review/fix до нулевого списка замечаний.
7. Только после успешной приёмки отметить родительскую задачу `[x]` и записать
   фактические результаты проверок в этот план.

## Матрица сценариев

| Сценарий | Ожидаемый результат |
| --- | --- |
| Те же leaf-поля в другом порядке | Canonical duplicate; одно повторение удалимо |
| Переставленные nested repeated children | Ambiguous; оба варианта сохранены |
| Различный nested `text` или `tail` | Ambiguous; оба варианта сохранены |
| Переставленные nested comment/PI/element | Ambiguous; оба варианта сохранены |
| Одинаковое nested содержимое и разное root indentation | Canonical duplicate |
| Повторяющиеся `ApplicationPath`, отличающиеся только slash style | Каждое повторение нормализовано независимо |
| Одно повторяющееся поле имеет другое значение | Ambiguous; отличается имя этого поля |
| Builder получает разные first/second title | Оба значения присутствуют в fixture XML |
| Dry-run на ambiguous fixture | Ноль XML-записей и ноль удалений |
| Apply на mixed fixture | Удалены только доказанные duplicates; все ambiguous-варианты сохранены |
| Cancellation во время большого nested traversal | `cancelled` до backup/stage/commit |

## Критерии приёмки и команды проверки

- Focused: `python -m unittest test.test_dedupe_additional_apps -v`.
- Codec regression: `python -m unittest test.test_xml_round_trip -v`.
- Mutation regression: `python -m unittest test.test_safe_write test.test_path_replacement -v`.
- Full: `python -m unittest discover -s test -p "test_*.py" -v`.
- Syntax/import: `python -m compileall -q launchbox_tools launchbox_utils.py test`.
- Hygiene: `git diff --check`.
- Итоговое acceptance review подтверждает 0 Blocker, 0 Regression,
  0 Specification gap и 0 иных замечаний в границах задачи.

## Риски и отдельные follow-up задачи

Консервативный fallback делает root content order-sensitive при наличии
comments, PI либо значимого mixed text. Это может оставить больше записей для
ручной проверки, но исключает ложноположительное удаление и не является потерей
данных. Snapshot guard, handle-based I/O и crash journal остаются следующими
отдельными P0 и не расширяют текущий scope.

## Результат приёмки

Приёмка выполнена 2026-07-16 после трёх review/fix циклов: добавлены недостающие
root comments/PI, положительный nested-equivalence и nested cancellation
сценарии, затем устранена неоднозначность чтения критической ветки `tail`.

- focused dedupe suite: 26 тестов успешно;
- dedupe + preserving codec + mutation regression suite: 113 тестов успешно;
- полный discovery, включая реальные Windows, Tk и process-level проверки:
  251 тест успешно в двух финальных прогонах;
- `compileall` и `git diff --check` успешно;
- dry-run сохранил исходный fixture без записи, apply удалил только четыре
  доказанных canonical duplicates и сохранил все 15 ambiguous-групп;
- переставленные nested repeated children, mixed `tail`, nested/root comments и
  PI остались ambiguous; одинаковый nested subtree с другим порядком и
  форматированием непосредственных полей остался canonical duplicate;
- fixture builder применяет оба title-параметра, а все тесты, которым нужны
  настоящие duplicates, теперь передают одинаковые значения явно;
- итоговое acceptance review: 0 Blocker, 0 Regression, 0 Specification gap и
  0 иных замечаний в границах задачи.
