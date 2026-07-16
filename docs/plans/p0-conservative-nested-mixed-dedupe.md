# Консервативная дедупликация вложенного и mixed XML

## Результат и предотвращаемый риск

Дедупликация `AdditionalApplication` автоматически удаляет только записи,
эквивалентные по полному XML-контракту. Перестановка вложенных узлов, изменение
mixed content, `text`/`tail`, comments либо processing instructions создаёт
отдельный вариант и переводит группу в ambiguous вместо удаления данных.

## Класс сложности и затрагиваемые подсистемы

Задача является P0 design-gate изменением XML-mutation поведения. После
повторного открытия она стала ограниченно cross-cutting: кроме canonicalization
в `operations/dedupe_additional_apps.py`, существующий preserving codec хранит
несериализуемую expanded namespace identity. Меняются также dedupe/codec tests и
документация; transaction executor, serializer, CLI/GUI, reports и manifest
продолжают потреблять прежние модели результата.

## Зависимости и архитектурные enablers

Обязательный enabler — принятый preserving XML round-trip контракт. Он владеет
сохранением порядка, `text`/`tail`, unknown nodes, comments и PI. Существующий
parser расширен только несериализуемой expanded namespace metadata; новый
parser/serializer pipeline не создаётся, lexical output contract не меняется.

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
- Никогда не удалять экземпляр, чей собственный `AdditionalApplication.tail`
  содержит значимый parent mixed content; такой экземпляр остаётся ambiguity
  `#parent-content` независимо от first/kept/duplicate роли.
- Автоматически сопоставлять duplicate только под одним непосредственным
  XML-родителем; одинаковые элементы под разными parents сохранять как
  ambiguity `#parent`.
- Сопоставлять доказанно одинаковые full canonical signatures независимо от
  порядка повторяющихся `GameID` / `ApplicationPath`, даже если их primary keys
  различаются.
- Диагностировать различие root tag/namespace как `#root`.
- Исправить test fixture builder, чтобы каждый переданный параметр попадал в
  сгенерированный XML.
- Проверить dry-run и apply на mixed content, повторяющихся полях, nested order,
  comments/PI и отсутствии удаления ambiguous-вариантов.

## Out of scope

- Изменения preserving serializer и lexical round-trip профиля сверх
  несериализуемой namespace metadata существующего parser.
- Полный redesign primary grouping key за пределами принятой connected-component
  связи по primary key и multiset key fields.
- Snapshot/fingerprint guard, hard-link/handle hardening и crash recovery.
- Новые outcomes, поля manifest/report либо пользовательские тексты CLI/GUI.
- Byte-for-byte сравнение XML или изменение принятой семантики attribute order.

## Инварианты

- Автоматическое удаление возможно только при полном совпадении contextual
  canonical signature, включая непосредственный parent, внутри одной logical
  dedupe-группы.
- Непосредственные обычные поля являются multiset: их порядок незначим, но
  количество и полные signatures каждого повторения значимы.
- Вложенные children всегда являются sequence; сортировка ниже корня
  `AdditionalApplication` запрещена.
- У вложенного/mixed XML точные `text` и `tail`, comments и PI значимы.
- Нормализация доменных leaf-полей не применяется к полю с children: malformed
  либо future mixed content сравнивается консервативно и буквально.
- Formatting-only root whitespace не создаёт ложную ambiguous-группу.
- Значимый tail любого `AdditionalApplication` является parent content и
  запрещает автоматическое удаление этого экземпляра даже при равной element
  signature; проверка не зависит от порядка экземпляров.
- Полная равная canonical signature остаётся достаточным доказательством
  duplicate при перестановке повторяющихся key fields только внутри одного
  непосредственного XML-parent.
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
| Duplicate имеет значимый `AdditionalApplication.tail` | Экземпляр сохранён как `#parent-content` ambiguity |
| Первый representative имеет значимый `AdditionalApplication.tail` | Safe later duplicate удалим; representative участвует в `#parent-content` ambiguity |
| Одинаковые элементы находятся под разными parents | Ноль удалений; ambiguity `#parent` |
| Повторяющиеся key fields переставлены | Exact canonical duplicate найден независимо от primary key |
| Root tag/namespace различается при равных fields/attributes | Ambiguous с `#root` diagnostic |
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

## Повторное независимое ревью 2026-07-16

После исходной приёмки отдельное независимое ревью нашло три пропущенные
границы. Значимый tail самого удаляемого `AdditionalApplication` терялся вместе
с элементом; одинаковые signatures с переставленными повторяющимися key fields
не встречались из-за разных primary keys; различие root namespace могло дать
пустой field diagnostic.

Исправления прошли red/green regression-цикл:

- protected-tail экземпляр сохраняется и входит в ambiguity
  `#parent-content`, тогда как соседний безопасный duplicate всё ещё удаляется;
- full canonical signature сопоставляется между primary-key группами, поэтому
  перестановка повторяющихся `GameID` / `ApplicationPath` не скрывает exact
  duplicate;
- `_differing_fields()` возвращает `#root` для различающихся root tags;
- три изолированных regression-теста сначала воспроизвели все дефекты, затем
  прошли после исправлений;
- focused dedupe suite: 29 тестов успешно;
- dedupe + preserving codec + mutation regression suite: 116 тестов успешно;
- полный discovery, включая реальные Windows, Tk и process-level проверки:
  254 теста успешно;
- комбинированные probes подтвердили сохранение двух значимых tails, безопасное
  удаление whitespace-tail duplicate рядом с сохранённым significant-tail
  representative и `#parent-content` для reordered-key protected duplicate;
- финальный полный discovery после повторного review: 254 теста успешно; один
  sandbox-прогон был отброшен из-за массового `%TEMP%` ACL failure и успешно
  повторён вне sandbox;
- `compileall` и `git diff --check` успешно;
- итоговое повторное review: 0 Blocker, 0 Regression, 0 Specification gap и
  0 новых замечаний.

## Повторное открытие после adversarial review исправления `4d493f7`

Предыдущее заключение о нулевом списке замечаний признано недействительным.
Adversarial-проверка самого исправления обнаружила три дефекта:

1. Глобальный `representatives_by_signature` сопоставляет записи из разных
   primary-key групп. Preserving parser хранит literal QName, но прежний
   signature не учитывает унаследованный namespace scope, поэтому один и тот же
   prefix с разными URI мог дать ложный duplicate и потерю данных.
2. Проверки root/parent mixed content используют Python `str.strip()`. NBSP и
   другие Unicode whitespace не являются formatting whitespace по XML 1.0,
   однако ошибочно отбрасывались вместе с удаляемым элементом.
3. Cross-key exact duplicate не добавляется в свою logical group. Поэтому
   следующий отличный вариант в этой группе не формирует ambiguity, хотя до
   fallback она диагностировалась.

### Исправленный design и инварианты

- Preserving codec сохраняет для обычного element и каждого атрибута пару
  namespace URI/local name наряду с неизменным lexical QName. Serializer
  продолжает писать исходные `tag`/`attrib`; metadata не попадает в XML.
- Canonical name включает и lexical QName, и expanded namespace identity. Это
  сохраняет прежнюю консервативность по prefix и различает одинаковый prefix в
  разных inherited scopes.
- Logical dedupe group является связной компонентой: записи связывает либо
  прежний primary key, либо одинаковое мультимножество нормализованных значений
  всех прямых `GameID` и `ApplicationPath`. Это сохраняет диагностику вариантов с
  общим primary key и связывает переставленные repeated key fields без
  глобального signature fallback.
- Внутри logical group одинаковый полный contextual signature под одним
  непосредственным parent является duplicate, а разные element/parent
  signatures формируют ambiguity. Удаляемый exact duplicate не может исключить
  ambiguity между своим surviving representative и третьим вариантом.
- Formatting whitespace проверяется только по набору XML 1.0 `" \t\r\n"`.
  NBSP и любой другой символ сохраняет root/parent content значимым.
- Сканирование больших text/tail выполняет cancellation checkpoint между
  ограниченными блоками; существующая граница по XML-узлам остаётся в силе.

### Baseline/candidate матрица

| Сценарий | `4d493f7` | Кандидат |
| --- | --- | --- |
| Одинаковый prefix, разные inherited namespace URI, reordered key fields | Ложный duplicate | 0 duplicates; namespace ambiguity; обе записи сохранены |
| NBSP в tail удаляемого `AdditionalApplication` | NBSP потерян | `#parent-content`; NBSP сохранён |
| NBSP во внутреннем root mixed content | Ложный duplicate | Ambiguity `#content`; оба варианта сохранены |
| Exact reordered-key variant плюс отличный вариант той же logical group | Duplicate скрывает ambiguity | Exact duplicate найден; ambiguity сохранена |
| Только XML formatting whitespace | Canonical duplicate | Canonical duplicate |

### Этапы повторного исправления

1. Добавить namespace identity в preserving DOM и отдельные codec-тесты на
   унаследованный scope без изменения lexical serialization.
2. Перевести canonical signatures и root/attribute diagnostics на namespace-aware
   identities.
3. Заменить global signature fallback на connected-component grouping по
   primary key либо полному multiset key fields.
4. Ввести cancellable XML-whitespace predicate и применить его ко всем
   formatting/mixed-content границам dedupe.
5. Добавить end-to-end dry-run/apply regression-тесты для трёх дефектов и
   граничный положительный XML-whitespace сценарий.
6. Выполнить focused, codec, mutation, full discovery, `compileall` и
   `git diff --check`; затем передать кандидат отдельному reviewer.

Статус: реализация повторно открыта. Этот раздел не является приёмкой, а
ROADMAP остаётся `[ ]` до независимого acceptance review.

## Candidate validation 2026-07-16

Реализация и авторский adversarial-проход завершены, но это не независимая
приёмка:

- focused dedupe + codec suite: 54 теста успешно;
- полный discovery, включая реальные Windows, Tk и process-level проверки:
  261 тест успешно в заключительном прогоне;
- новые end-to-end тесты подтверждают: inherited namespace scopes не дают
  ложного duplicate и диагностируются как ambiguity; NBSP сохраняется во
  внутреннем и parent mixed content; reordered exact duplicate не скрывает
  третий отличный вариант;
- отдельные operational tests подтверждают cancellation внутри многомегабайтного
  whitespace и внутри одного элемента с сотнями атрибутов;
- adversarial review candidate diff дополнительно выявил и устранил потерю
  namespace metadata при shallow/deep copy и удержание отдельного полного
  canonical signature для каждого exact duplicate;
- `compileall` и `git diff --check` успешно.

Статус остаётся `[ ]`: следующий шаг — независимый reviewer, который не является
автором этого candidate diff.

## Независимое review candidate `6c7adcb` и повторное исправление

Независимое acceptance review подтвердило red/green для трёх дефектов baseline
`4d493f7`, но отклонило candidate по двум дополнительным границам:

1. Значимый tail проверялся только у последующего duplicate. Если protected
   экземпляр был первым representative, safe later duplicate удалялся, но
   обязательная ambiguity `#parent-content` не создавалась.
2. Полный element signature не включал parent context. Два одинаковых элемента
   под `FirstScope` и `SecondScope` считались duplicate, и apply удалял запись из
   второго parent.

Исправление вводит contextual signature `(immediate parent identity, canonical
element)`. Logical connected component остаётся общей для диагностики, но
автоматическое удаление разрешено только внутри одного parent; разные parents
дают ambiguity `#parent`. Проверка significant tail выполняется до выбора
representative, поэтому защищённый экземпляр участвует в `#parent-content`
finding в любой позиции. Ambiguity variants содержат только реально сохраняемые
записи; safe duplicate, удаляемый рядом с protected representative, остаётся
только в duplicate result и не маркируется отчётом как `Keep variant`.

### Validation исправления 2026-07-16

- два exact regression-теста воспроизводят оба дефекта на `6c7adcb` и проходят
  на текущем working tree;
- четыре новых end-to-end сценария покрывают protected-first, композицию
  `protected A / safe A / B`, разные parents и safe duplicate внутри одного из
  двух parent contexts;
- dedupe + codec + mutation regression suite: 127 тестов успешно;
- полный discovery, включая Windows, Tk и process-level проверки: 265 тестов
  успешно;
- `compileall`, `git diff --check` и проверка изменённых Markdown-файлов на
  mojibake успешно;
- English/Russian README, ARCHITECTURE, ROADMAP и верхние scope-границы этого
  плана синхронизированы с фактическим contextual contract.

Статус остаётся `[ ]`: это implementation/self-validation evidence, а не новое
независимое acceptance review.
