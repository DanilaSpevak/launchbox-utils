# Правила планирования и приёмки roadmap

Этот документ определяет, как задачи попадают в [`ROADMAP.md`](../ROADMAP.md),
как крупные задачи разбиваются перед реализацией и когда их можно считать
выполненными. Цель процесса — заранее обнаруживать скрытые архитектурные
зависимости и не превращать приёмочное ревью в последовательное уточнение
неоговорённых требований.

## Роли документов

- ADR фиксирует существенное архитектурное или продуктовое решение, его
  альтернативы и последствия. Направление со статусом `Proposed` не является
  обязательством по реализации.
- `ROADMAP.md` содержит только принятые результаты, расположенные по убыванию
  риска с учётом графа зависимостей. Roadmap не должен заменять технический
  план реализации.
- Для cross-cutting задачи до начала реализации составляется execution plan в
  описании PR либо в отдельном Markdown-файле внутри `docs/`. План должен быть
  доступен для ревью вместе с изменениями проекта.

Если задача требует нового архитектурного решения, сначала создаётся ADR. В
roadmap она добавляется только после перехода ADR в статус `Accepted`.

## Приоритизация и граф зависимостей

Приоритет отражает не только ущерб и вероятность риска, но и порядок, в котором
результаты можно реализовать без временных обходов и последующей переделки.
Перед добавлением или перестановкой задач для них строится направленный граф:
ребро `A → B` означает, что результат A является обязательным enabler для B.

- Задача не может иметь обязательный enabler в более низком приоритете или ниже
  себя внутри одного приоритета. Такой enabler поднимается перед зависимой
  задачей.
- Если нижняя задача нужна не целиком, из неё выделяется минимальный enabler с
  собственным результатом и критериями приёмки. Остальной scope остаётся на
  исходном уровне.
- Нельзя считать будущую общую абстракцию неявной зависимостью и одновременно
  реализовывать её временную копию внутри более приоритетной задачи. Владелец
  контракта и слой, который его только потребляет, фиксируются явно.
- Инфраструктурный quality gate более низкого приоритета не должен блокировать
  поведенческую приёмку более приоритетной задачи. Необходимые regression-тесты
  включаются в приоритетную задачу, а измерение coverage или общий CI gate могут
  оставаться отдельным последующим результатом.

Граф активных задач должен быть ациклическим. Если A требует B, а B требует A,
задачи декомпозируются до уровня, на котором общий контракт, state machine или
низкоуровневый примитив становится отдельным enabler C, а зависимости принимают
вид `C → A` и `C → B`. Задача не готова к реализации, пока цикл не устранён и
для каждого общего результата не определён единственный владеющий слой.

## Готовность задачи к реализации

До начала работы у задачи должны быть определены:

1. ожидаемый пользовательский или системный результат;
2. причина приоритета и предотвращаемый риск;
3. границы `in scope` и `out of scope`;
4. зависимости и необходимые архитектурные enablers;
5. наблюдаемые критерии приёмки;
6. требуемые уровни проверки: unit, integration, реальный GUI/Windows smoke;
7. класс сложности: обычная или cross-cutting.

Если любой из этих пунктов существенно влияет на архитектуру, безопасность или
объём работы и пока неизвестен, задача ещё не готова к реализации. Сначала
проводится ограниченный анализ или spike; его результат уточняет план, а не
скрыто расширяет реализацию.

## Обязательный design gate

Задача считается cross-cutting и требует design review до написания основного
кода, если затрагивает два или более следующих признака:

- изменение LaunchBox XML или других пользовательских данных;
- потоки, процессы либо межпроцессную синхронизацию;
- cancellation, commit, rollback или восстановление;
- lifecycle GUI или завершение процесса;
- общий XML/file I/O либо другой низкоуровневый shared code;
- несколько поверхностей: CLI, GUI, отчёты, manifest;
- несколько операций с дублирующейся orchestration-логикой;
- реальные Windows, process-level или Tk integration-тесты.

Для любой P0-задачи с мутациями, concurrency или lifecycle этот gate обязателен
независимо от количества признаков.

Design review должен зафиксировать:

- инварианты безопасности и единственный источник истины для состояния;
- состояния или фазы и допустимые переходы между ними;
- необратимую границу операции;
- семантику отмены, включая гранулярность checkpoints;
- cleanup, recovery и поведение при частичном сбое;
- итоговые outcome и ошибки на всех пользовательских поверхностях;
- владельца фоновых workers, callbacks и временных ресурсов;
- матрицу критических сценариев и способ их проверки.

Проверка зависимостей является частью gate. Если функция опирается на общую
абстракцию, запланированную позже, сначала реализуется минимальный общий enabler
либо явно принимается ограниченный временный дизайн. Нельзя молча дублировать
будущую архитектуру в нескольких операциях.

## Разбиение крупных задач

Roadmap сохраняет один родительский результат, но execution plan разбивает его
на независимо проверяемые этапы. Рекомендуемый порядок для системных изменений:

1. общий контракт, state machine или другая минимальная основа;
2. низкоуровневые примитивы и границы безопасности;
3. интеграция с доменными операциями и моделями результатов;
4. интеграция с CLI/GUI и пользовательскими сообщениями;
5. integration-тесты, документация и приёмка.

Каждый этап должен сохранять уже принятые инварианты и иметь сфокусированную
проверку. Если этап нельзя проверить отдельно или он одновременно меняет
несвязанные подсистемы, его следует разделить ещё раз.

## Обязательный adversarial acceptance gate

Исправление заранее известного дефекта и приёмка задачи — разные утверждения.
Повторение исходных repro доказывает только то, что конкретные сценарии
исправлены. Оно не доказывает, что новый diff не добавил Regression, не ослабил
соседний инвариант и не изменил результат в непроверенной комбинации условий.
Полный regression suite также не заменяет эту проверку: он подтверждает только
сценарии, уже представленные в suite.

Gate обязателен для любой P0-задачи, а также для исправления Blocker или
Regression в mutation, concurrency, lifecycle, recovery либо shared I/O. Для
другой cross-cutting задачи необходимость gate фиксируется в execution plan.

До передачи результата независимому reviewer автор выполняет тот же gate как
pre-review. Цель — перенести поиск граничных случаев до итогового review, чтобы
reviewer проверял уже сформированное доказательство, а не по очереди открывал
очевидные классы входов. Reviewer при этом не принимает тесты и вывод автора как
достаточное доказательство и начинает с точного diff проверяемых commit.

Self-review автора не заменяет независимую P0-приёмку. Итоговый reviewer не
должен быть автором проверяемого поведения. Если в ходе review он меняет code
или tests, candidate становится новым результатом и требует повторного
независимого verdict после полного релевантного gate.

### Обязательная последовательность

1. **Зафиксировать baseline и candidate.** Указать defect baseline и точные
   проверяемые commit. Один и тот же regression-test либо внешний harness должен
   воспроизводить дефект на baseline и проходить на candidate. Новый test можно
   временно применить к baseline отдельно от product fix. Тест, зелёный на обоих
   состояниях, сам по себе не доказывает исправление.
2. **Построить карту изменений diff.** Для каждой новой ветки, fallback,
   нормализации, state transition и нового источника истины записать допущение,
   на котором держится безопасность изменения.
3. **Построить adversarial matrix.** Проверить не только ожидаемый пример, но и
   границы equivalence, порядок и роль участников, сочетания с соседними
   вариантами, ошибками и cancellation.
4. **Выполнить differential proof.** Один и тот же детерминированный либо
   сгенерированный корпус запускается на baseline и candidate. Каждое изменение
   outcome, state, diagnostics, mutated data и side effects должно быть
   перечислено и объяснено принятым контрактом.
5. **Проверить conservation.** Для мутации пользовательских данных результат
   может отличаться от входа только заранее разрешёнными target-изменениями.
   Остальные узлы, значения, metadata, lexical profile и внешние файлы
   сохраняются согласно контракту. Необъяснённое отличие является Blocker.
6. **Проверить эксплуатационные границы.** Cancellation, cleanup, повторный
   запуск, размер входа, platform-specific поведение и стоимость новых обходов
   проверяются там, где diff может на них влиять.
7. **Запустить focused и полный regression suite.** Этот шаг выполняется после
   adversarial и differential проверок, а не вместо них.
8. **Повторить gate после review-fix.** Любое изменение candidate после
   найденного замечания аннулирует предыдущий acceptance verdict. Повторяется
   вся релевантная матрица, а не только последний repro.

Минимальные измерения adversarial matrix выбираются по diff и контракту:

| Измерение | Что обязательно проверить |
| --- | --- |
| Граница equivalence | Значения по обе стороны каждой нормализации: отсутствующее/пустое, lexical/semantic identity, platform и Unicode boundary |
| Порядок и роль | Все значимые перестановки first/last, kept/removed, winner/loser, cancel/commit |
| Композиция | Исправленный случай рядом с другим variant, ambiguity, failure, retry или partial result |
| Контекст | Родитель, namespace/path scope, trust boundary и другие данные вне локального объекта |
| Conservation | Точный перечень разрешённых изменений и доказательство сохранности всего остального |
| Масштаб | Большой единичный объект, много объектов, checkpoint/cancellation и дополнительная память/обходы |

Матрица не считается выполненной, если reviewer просто повторил тесты автора или
ограничился исходными замечаниями. Неприменимое измерение помечается явно с
обоснованием; молчаливое отсутствие проверки считается непроверенной областью.

### Требования к acceptance evidence и формулировке вердикта

Итоговый отчёт должен содержать:

- baseline и candidate commit;
- исходные repro с доказательством `red on baseline → green on candidate`;
- карту новых веток и допущений diff;
- adversarial matrix и результаты differential/conservation проверок;
- выполненные команды focused/full/integration validation;
- непроверенные области и причины, по которым они не блокируют результат;
- отдельные списки Blocker, Regression, Specification gap, Hardening и Refactor;
- явный итоговый verdict.

Формулировки должны отражать силу доказательства:

- «исходные замечания исправлены» означает только успешный повтор известных
  repro и не является acceptance verdict;
- «в проверенной матрице новых дефектов не найдено» допустимо только вместе с
  перечислением этой матрицы и непроверенных областей;
- без завершённого gate нельзя утверждать «новых дефектов нет», «P0 закрыта» или
  переводить задачу в `[x]`;
- обнаруженный Blocker или Regression оставляет задачу `[ ]`, даже если полный
  suite зелёный.

Минимальный шаблон итогового acceptance report:

```markdown
# Acceptance review: <результат>

## Baseline и candidate
## Исходные repro: red → green
## Карта новых веток и допущений diff
## Adversarial matrix
## Differential proof
## Conservation и non-target side effects
## Focused, full и integration validation
## Непроверенные области и пределы verdict
## Blocker / Regression / Specification gap / Hardening / Refactor
## Итоговый verdict
```

## Статусы и критерий `[x]`

- `[ ]` означает, что результат ещё не принят. Реализация при этом может быть в
  работе, завершена технически или находиться на приёмочном ревью.
- `[x]` означает `Accepted`: критерии выполнены, обязательная проверка пройдена,
  блокирующих замечаний нет, документация соответствует фактическому поведению.
- Задача не отмечается `[x]` в первом implementation commit только потому, что
  добавлен основной код или прошли сфокусированные unit-тесты.

Перед `[x]` для изменения поведения необходимо:

1. проверить все заранее согласованные сценарии;
2. завершить обязательный adversarial acceptance gate, если он применим;
3. выполнить полный `unittest` discovery;
4. выполнить релевантные GUI, Windows или process-level проверки;
5. проверить отсутствие регрессий в outcome, manifest, cleanup и отчётах;
6. синхронизировать `ARCHITECTURE.md`, README и пользовательские тексты там,
   где изменился их контракт;
7. провести одно итоговое acceptance review с evidence и явным вердиктом.

## Классификация замечаний ревью

Каждое новое замечание относится к одной категории:

- **Blocker** — нарушен заранее согласованный инвариант или критерий приёмки;
  исправляется в текущей задаче.
- **Regression** — реализация сломала ранее поддерживаемое поведение;
  исправляется в текущей задаче.
- **Specification gap** — обнаружено важное, но не согласованное требование;
  план и scope пересматриваются до продолжения реализации.
- **Hardening** — дополнительная защита, не требуемая принятым контрактом;
  оформляется отдельной задачей. Сделать её блокирующей для текущего P0 можно
  только через `Specification gap` и явное решение владельца проекта об
  изменении контракта.
- **Refactor** — улучшение структуры без нарушения текущего контракта;
  оформляется отдельной задачей с собственной критичностью.

Новый инвариант нельзя незаметно добавлять под видом обычного исправления. Если
он необходим для исходной цели безопасности, задача официально возвращается на
design gate; иначе замечание переносится в отдельный roadmap item.

## Автономное исполнение roadmap

Этот раздел применяется, когда владелец проекта явно запустил автономный цикл
для одной задачи, группы приоритетов или всего готового roadmap. Сам факт запуска
цикла разрешает локальный анализ, изменения, проверки и commits в его границах,
но не разрешает push, merge, release, изменение внешних систем или мутацию
реальных пользовательских данных без отдельно выданного полномочия.

### Роли и полномочия

- **Владелец проекта** принимает решения, которые меняют результат, scope,
  архитектурные инварианты, критерии приёмки или допустимый риск. Его участие не
  требуется для обычных исправлений, уже однозначно следующих из контракта.
- **Orchestrator** выбирает готовую задачу, фиксирует её состояние, назначает
  implementer и reviewer, проверяет обязательные gates и продолжает цикл, пока
  не достигнут `accepted` либо `decision_required`. Orchestrator применяет
  принятый контракт, но не расширяет его.
- **Implementer** является единственным write-capable автором candidate в
  текущей итерации: реализует результат, добавляет tests, выполняет self-review,
  запускает проверки и создаёт commits.
- **Recorder** используется только для audit work item и может byte-identical
  сохранять report/audit evidence, но не менять findings, product code или
  приоритет roadmap.
- **Reviewer** не является автором проверяемого поведения, начинает с точных
  baseline/candidate commits и работает read-only относительно candidate. Он
  выдаёт findings, evidence и verdict, но не исправляет code или tests.

Один агент не совмещает роли implementer и итогового reviewer одного candidate.
Каждую итерацию после review-fix проверяет новый reviewer, не участвовавший в
создании code или tests этого candidate. Reviewer может предоставить отдельный
repro или patch как evidence, но implementer переносит его в candidate и несёт
ответственность за resulting test.

### Work item и состояния

До захвата задачи execution plan получает стабильный `work_item_id` и следующий
машиночитаемый блок либо эквивалентные явно именованные поля:

```yaml
work_item_id: <stable-id>
work_item_type: implementation
status: unclaimed
depends_on: []
base_sha: <commit>
candidate_sha: null
accepted_code_sha: null
branch: <task-branch>
review_round: 0
claimed_by: null
implementer: null
recorder: null
design_reviewer: null
design_plan_sha: null
design_verdict_id: null
design_verdict_sha256: null
review_history: []
auditor: null
decision_required: null
resume_state: null
verdict_id: null
verdict_sha256: null
```

Допустимые состояния:

- `unclaimed` — задача ещё не закреплена authoritative remote claim;
- `preparing` — orchestrator формализует execution plan и проводит независимый
  design review, не меняя принятый roadmap-контракт;
- `ready` — контракт достаточен, зависимости приняты, обязательный design gate
  завершён;
- `implementing` — implementer изменяет candidate;
- `self_review` — реализация зафиксирована, автор выполняет pre-review и gates;
- `auditing` — независимый auditor read-only выполняет audit matrix;
- `recording` — recorder byte-identical сохраняет audit report и proposals;
- `reviewing` — candidate неизменяем и передан независимому reviewer;
- `fixing` — Blocker или Regression возвращены implementer;
- `decision_required` — продолжение требует решения владельца проекта;
- `accepted` — независимый verdict получен и blocking findings отсутствуют;
- `awaiting_merge` — metadata-only closeout опубликован, но ещё не интегрирован;
- `suspended` — audit work item ждёт интеграции принятых remediation findings;
- `integrated` — принятый результат присутствует в canonical default branch;
- `cancelled` — владелец явно прекратил work item; состояние терминальное.

Только orchestrator меняет состояние work item. Implementer и reviewer сообщают
результат своей роли, но не объявляют задачу принятой в обход state transition.

Разрешённые переходы исчерпывающие:

| Из | В |
| --- | --- |
| `unclaimed` | `preparing` или `decision_required` |
| `preparing` | `ready`, `auditing` или `decision_required` |
| `ready` | `implementing` или `decision_required` |
| `implementing` | `self_review` или `decision_required` |
| `self_review` | `implementing`, `fixing`, `reviewing` или `decision_required` |
| `auditing` | `recording` или `decision_required` |
| `recording` | `reviewing` или `decision_required` |
| `reviewing` | `fixing`, `recording`, `suspended`, `accepted` или `decision_required` |
| `fixing` | `self_review` или `decision_required` |
| `accepted` | `awaiting_merge`, `preparing` или `decision_required` |
| `awaiting_merge` | `integrated`, `preparing` или `decision_required` |
| `suspended` | `auditing`, `cancelled` или `decision_required` |
| `decision_required` | точный `resume_state` либо `cancelled`, записанный решением владельца |

Провал self-review возвращает задачу в `implementing`, а провал pre-review с
уже зафиксированным finding — в `fixing`. Ответ владельца записывает точный
`resume_state`; продолжение разрешено только переходом
`decision_required → resume_state`, после фиксации решения в плане или ADR.

### Выбор и захват следующей задачи

Если верхняя незавершённая задача ещё не `ready` только из-за отсутствующего
execution plan или незавершённого design gate, orchestrator сначала выполняет
authoritative remote claim и переводит её `unclaimed → preparing`. Он
формализует уже принятые результат, границы,
инварианты, матрицу и проверки, после чего передаёт план независимому design
reviewer. При положительном verdict задача переходит в `ready`. Любая
необходимость выбрать новый контракт, расширить scope или принять риск является
`Specification gap` и переводит задачу в `decision_required`; orchestrator не
  принимает такое решение самостоятельно.

Design reviewer проверяет точный plan commit. State block хранит его SHA как
`design_plan_sha`, внешний ID verdict и SHA-256 точных UTF-8 байтов verdict.
Перед переходом из `preparing` orchestrator повторно читает внешний verdict и
сверяет hash. Любое изменение plan после review аннулирует design verdict:
пока задача остаётся в `preparing`, изменённый plan проходит новый независимый
review; после перехода в `ready` изменение контракта является
`Specification gap` и требует `decision_required`.

Кандидат для `preparing` выбирается по тем же правилам приоритета и зависимостей,
что и готовая задача. Отсутствие execution plan само по себе не разрешает
пропустить верхнюю задачу и взять более удобную нижнюю.

После preparatory claim задача является `ready` к реализации, только если
одновременно:

1. она находится в активных разделах `P0`–`P3` и имеет `[ ]`;
2. все её обязательные enablers имеют `[x]`;
3. выполнены требования раздела «Готовность задачи к реализации»;
4. применимый design gate завершён и не содержит открытого решения;
5. задача не захвачена другим orchestrator; разрешена только её собственная
   preparatory branch с совпадающими `work_item_id`, `base_sha` и `claimed_by`;
6. доступна среда для обязательных проверок либо в плане заранее согласовано,
   почему конкретная проверка не входит в blocking gate.

Из готовых задач выбирается задача наиболее высокого приоритета, а внутри него
— первая по порядку roadmap. Нельзя пропускать готовую более приоритетную задачу
ради удобства реализации. Audit и release-gate задачи становятся готовыми только
после выполнения всех перечисленных в них prerequisites. Если готовых задач нет,
orchestrator не придумывает новую работу и переходит в `decision_required` с
перечнем причин.

Захват фиксирует `base_sha`, task branch и `claimed_by` до первого изменения.
В одном worktree одновременно разрешён только один write-capable implementer.
Параллельные задачи используют отдельные branches и worktrees; reviewer не
изменяет файлы в worktree проверяемого candidate.

В гибридном PR-контуре authoritative claim выполняется create-only созданием
remote branch `agent/<work_item_id>` от точного `base_sha` до первого plan
commit: существующая branch означает конфликт claim и запрещает продолжение.
Непосредственно перед create-only операцией orchestrator читает HEAD canonical
default branch из remote API и использует именно этот SHA, а не локальную
tracking reference.
После первого plan commit создаётся draft PR, а его машинный state block
становится authoritative store
для переходов, SHA и `review_round`; обновление выполняется только после проверки
предыдущего состояния. Если authoritative remote branch/PR недоступны, гибридный
цикл не переходит из `unclaimed` и создаёт `decision_required`; local-only claim
этим контрактом не поддерживается.

Remote claim не считается просроченным автоматически. Закрытый без merge PR,
удалённая не orchestrator'ом branch, несовпадающий `base_sha` или claim без
наблюдаемого владельца переводят задачу в `decision_required`; только владелец
проекта может освободить или переназначить такой claim. Это запрещает двум
orchestrator одновременно считать себя владельцами одной задачи.

Каждый запуск сначала выполняет idempotent remote probe по всему семейству:
`agent/<work_item_id>`, implementation `-refresh-<N>` и audit
`-audit-refresh-<N>` branches, а также всем PR с такими head. Полное отсутствие
branches и PR означает новый claim. Ровно один открытый draft PR в canonical
default branch означает resume только при существующей head branch, совпадении
`work_item_id`, `branch`, `base_sha`, `claimed_by`, валидной history и закрытой
superseded-цепочке всех прежних PR, явно ведущей к активному PR. Любая другая
комбинация, включая orphan branch/PR, несколько активных PR, non-draft active PR,
unsuperseded closed PR, разрыв цепочки или расхождение state, даёт
`decision_required`. Resume выполняется с записанного status и не повторяет уже
зафиксированные preparation, implementation, review либо closeout transitions.

Для каждой refresh-семьи `N` равен единице плюс максимальный числовой suffix,
наблюдаемый во всех её remote branches и PR. Некорректный suffix даёт
`decision_required`, а create-only остаётся окончательной защитой от коллизии.
Пустое семейство имеет максимум `0`; первый suffix равен `1`. Допустимая запись
`N` — decimal `[1-9][0-9]*` без ведущих нулей.

### Отдельный путь независимого audit work item

Для roadmap-задачи, результатом которой является независимый аудит, используется
`work_item_type: audit`; обычный implementer не создаёт проверяемое поведение.
Orchestrator фиксирует canonical `base_sha`, scope и audit matrix, после чего
переводит `preparing → auditing`. Новый auditor работает read-only и формирует
report с evidence и findings, затем `auditing → recording` передаёт результат
recorder.

Recorder может byte-identical перенести report и предложенные auditor'ом roadmap
findings в task branch, но не меняет их классификацию. Добавление findings в
roadmap и их приоритет требуют решения владельца проекта; до решения они остаются
предложениями внутри audit report. Второй независимый reviewer проверяет полноту
audit scope, evidence и корректность переноса в состоянии `reviewing`; дефект
переноса возвращает `reviewing → recording`, а положительный verdict даёт
`reviewing → accepted`. Если
аудит открыл новый Blocker, Regression либо более приоритетную задачу, audit item
переходит `reviewing → suspended` и остаётся `[ ]`. После решения владельца
принятые findings добавляются выше зависимых задач и записываются в `depends_on`;
orchestrator может выполнять только эти remediation work items. После их
интеграции audit возвращается `suspended → auditing` на новом canonical
`base_sha` и повторяется новым auditor. Audit item можно
принять только при выполнении его собственных критериев об отсутствии открытых
blocking findings.

Повтор suspended audit использует отдельный audit-refresh протокол. Orchestrator
читает новый canonical HEAD после интеграции всех `depends_on` remediation,
create-only создаёт `agent/<work_item_id>-audit-refresh-<N>` и подключает чистый
изолированный worktree. В новой branch создаётся новый
`docs/plans/<work_item_id>-audit-repeat-<N>.md`, который ссылается на прежний PR,
immutable audit report и интегрированные remediation SHA; исторический plan не
переписывается.

Новый draft PR сначала получает скопированный `suspended` state с новым
`base_sha`/`branch`. При переносе authority сохраняются `work_item_id`,
`claimed_by`, scope и `depends_on`, но очищаются `candidate_sha`,
`accepted_code_sha`, design/verdict fields, `review_history`; `review_round`
становится `0`, а активные `auditor`, `recorder` и reviewer — `null`. Старый PR
закрывается как superseded со ссылкой на новый; новый PR становится единственным
authoritative store только после успешной проверки обеих сторон переноса. Любой
частичный перенос даёт `decision_required`. Затем orchestrator назначает нового
auditor в state block и выполняет разрешённый `suspended → auditing`; до
`auditing → recording` он отдельно фиксирует recorder. Audit-refresh никогда не
использует `implementing` и не переносит старый acceptance verdict.

### Git-протокол и граница candidate

- Task branch создаётся от согласованного `base_sha`; baseline не выводится из
  плавающего имени ветки после начала работы.
- Существующие пользовательские или посторонние изменения не stash, reset,
  amend и не включаются в task commits. Если их нельзя изолировать безопасным
  worktree, задача переходит в `decision_required`.
- Основная реализация фиксируется отдельным implementation commit. Каждая
  законченная итерация замечаний фиксируется новым `fix(review-N): ...` commit
  после focused validation; несвязанные findings не смешиваются с новой
  функциональностью.
- Reviewer всегда проверяет полный диапазон `base_sha..candidate_sha`, а также
  отдельно последний review-fix diff. Зелёный последний repro не заменяет
  повторную проверку cumulative candidate.
- Автономный цикл не выполняет push, merge, rebase опубликованной ветки, squash,
  tag или release, если соответствующее полномочие не было выдано явно.

### Цикл реализации и независимого review

1. Orchestrator выполняет authoritative claim и при необходимости
   `unclaimed → preparing → ready`, затем переводит `ready → implementing`.
2. Implementer создаёт implementation commit, записывает `candidate_sha`,
   выполняет self-review и применимые проверки, затем сообщает о готовности;
   orchestrator переводит результат в `self_review`.
3. После успешного pre-review orchestrator замораживает candidate и начинает
   `reviewing` с новым независимым reviewer.
4. Reviewer классифицирует каждое finding и выдаёт явный verdict.
5. При Blocker или Regression orchestrator переводит задачу в `fixing`;
   implementer делает один review-fix commit для согласованной итерации,
   повторяет релевантный gate и формирует новый `candidate_sha`.
6. Новый reviewer заново проверяет cumulative candidate. Цикл повторяется, пока
   blocking findings не исчезнут либо не возникнет `decision_required`.
7. Hardening и Refactor не расширяют candidate: orchestrator записывает их как
   отдельные follow-up candidates. До `integrated` владелец назначает каждому
   принятому follow-up критичность и место в roadmap либо явно отклоняет его;
   это решение не аннулирует acceptance текущего candidate.

Для каждого design/review шага state block фиксирует agent identity. Каждая
запись `review_history` содержит `round`, `reviewer`, `candidate_sha`,
`verdict_id` и verdict. Orchestrator отклоняет reviewer, уже указанного как
implementer, recorder, auditor либо автор code/tests candidate. `review_round`
равен числу записей history; расхождение считается повреждённым state и даёт
`decision_required`.

### Обязательная остановка `decision_required`

Orchestrator обязан остановить автономное исполнение и сформулировать один
конкретный вопрос владельцу проекта, если:

- найден `Specification gap` или требуется изменить результат, scope,
  архитектурный инвариант, acceptance criteria либо принятое ADR;
- implementer и reviewer расходятся в классификации или достаточности evidence;
- один и тот же по существу Blocker или Regression повторился после двух
  последовательных review-fix итераций;
- завершены пять review rounds независимо от того, повторялись ли findings;
- исправление требует несогласованной зависимости, новой подсистемы или
  существенного расширения execution plan;
- обязательная проверка недоступна, недетерминирована либо дважды завершилась
  инфраструктурной ошибкой;
- безопасная изоляция baseline, task branch или пользовательских изменений
  невозможна;
- требуется push за пределы заранее разрешённой task branch либо отсутствует
  заранее выданное полномочие на нужный push; требуется merge, release, секрет,
  иное внешнее полномочие, необратимое действие или доступ к реальным
  пользовательским данным;
- task-specific контракт прямо требует решения владельца проекта.

Остановка содержит: текущее состояние, `base_sha`/`candidate_sha`, уже
проверенные факты, блокирующее решение, минимальные взаимоисключающие варианты и
последствия каждого. После ответа orchestrator фиксирует решение в execution
plan или ADR и продолжает с подходящего gate, а не с произвольного шага.

### Acceptance SHA и завершение

Положительный итоговый verdict относится к неизменяемому `candidate_sha`, который
записывается как `accepted_code_sha`. Любое последующее изменение product code,
tests, runtime configuration, пользовательского контракта или acceptance
evidence с новыми утверждениями, интерпретацией либо результатами аннулирует
verdict и создаёт новый candidate. Механическая фиксация неизменённого verdict и
уже проверенных результатов в closeout report не является новым evidence.

Итоговый reviewer сначала публикует verdict как неизменяемый PR review/comment.
Orchestrator записывает его внешний `verdict_id` и SHA-256 точных UTF-8 байтов в
state block. Closeout report копирует эти байты без изменений и повторяет hash;
любое отличие, дополнение либо новая интерпретация требует нового verdict.

До `accepted` и closeout orchestrator читает CI именно для `candidate_sha` и
требует успешные, non-skipped Windows jobs Python 3.10, 3.11, 3.12 и 3.13.
Отсутствующий, stale, skipped, cancelled, недоступный или failed обязательный job
не считается зелёным; недоступность и повторная инфраструктурная ошибка
обрабатываются через `decision_required` по правилам выше.

Итоговое evidence версионируется без перезаписи в `docs/plans/`. Обычная
реализация использует `<work_item_id>-acceptance.md`, implementation refresh —
`<work_item_id>-refresh-<N>-acceptance.md`, первичный audit —
`<work_item_id>-audit-acceptance.md`, audit refresh —
`<work_item_id>-audit-refresh-<N>-acceptance.md`. Конфликт с существующим путём
даёт `decision_required`; исторические reports не переписываются.

Перед closeout и непосредственно перед merge orchestrator fetch'ит canonical
default branch и требует, чтобы её HEAD оставался равен проверенному `base_sha`.
Если baseline продвинулся, verdict аннулируется, задача возвращается
из `accepted` либо `awaiting_merge` в `preparing`, новый default HEAD
становится `base_sha`, а
orchestrator создаёт create-only replacement branch
`agent/<work_item_id>-refresh-<N>` от нового baseline, без force/rebase
опубликованной branch. Orchestrator создаёт новый
`docs/plans/<work_item_id>-refresh-<N>.md`, который ссылается на исторический
plan и описывает только новый baseline и перенос неизменного контракта; этот plan
commit проходит новый независимый design review до переноса task commits.
Orchestrator создаёт новый draft PR, атомарно объявляет его authoritative store,
а старый PR помечает superseded ссылкой на новый. Новый state сохраняет
`work_item_id`, `claimed_by` и роли, но заменяет `base_sha`/`branch`, очищает
`candidate_sha`, `accepted_code_sha`, `design_reviewer`, `design_plan_sha`,
`design_verdict_id`, `design_verdict_sha256`, `verdict_id`, `verdict_sha256`,
`review_history`, устанавливает `review_round: 0` и `status: preparing`.
После нового design verdict выполняются `preparing → ready → implementing`, и
только тогда implementer переносит task commits в replacement branch.
Актуализированный cumulative candidate проходит полный pre-review и новое
независимое review. Неполный перенос state или конфликт commits даёт
`decision_required`.

После verdict разрешён один metadata-only closeout commit, который только:

- сохраняет acceptance report и точный `accepted_code_sha`;
- переводит work item из `accepted` в `awaiting_merge`;
- ставит `[x]` у принятой roadmap-задачи;
- по уже полученному решению владельца добавляет принятые Hardening/Refactor
  follow-ups в `ROADMAP.md` с назначенной критичностью либо фиксирует их явное
  отклонение в acceptance report.

Такой closeout commit не аннулирует verdict, поскольку не меняет проверенный
candidate. Если closeout требует содержательного изменения документации о
поведении, оно выполняется до итогового review и входит в `candidate_sha`.

Отметка `[x]` в task branch не удовлетворяет зависимостям других задач. Результат
становится `integrated` только после появления `accepted_code_sha` и closeout в
canonical default branch. Разрешён fast-forward либо merge commit, у которого
первый parent равен проверенному `base_sha`, а второй ведёт к closeout commit;
squash/rebase merge не подтверждает `integrated`. Если фактический merge нарушил
эту границу, orchestrator переводит `awaiting_merge → decision_required` и не
считает combined tree принятым. До `integrated` следующая задача не начинается.
Исключение — `suspended` audit: до его повторения разрешены только перечисленные
в `depends_on` remediation work items, принятые владельцем проекта.
После `integrated` он может автоматически выбрать следующую готовую задачу в
пределах выданного полномочия. Если следующая задача выходит за эти границы,
цикл завершается отчётом, а не расширяет разрешённый scope.

При следующем запуске после owner merge orchestrator сначала reconciles текущий
work item из durable Goal, а не выбирает новую задачу. Ровно один terminal merged
PR в корректной superseded-цепочке переводится `awaiting_merge → integrated`
только после проверки, что canonical `main` содержит `accepted_code_sha` и
metadata-only closeout с разрешённой parent/ancestry формой выше. State
`integrated` записывается в merged PR; затем текущий work item освобождается и
разрешён выбор следующего. Несовпадение ancestry, `[x]`, SHA или несколько
terminal merged PR дают `decision_required`.

## Минимальный шаблон execution plan

```markdown
# <Название результата>

## Результат и предотвращаемый риск
## Класс сложности и затрагиваемые подсистемы
## Зависимости и архитектурные enablers
## In scope
## Out of scope
## Инварианты
## Фазы, состояния и необратимые границы
## Этапы реализации
## Матрица сценариев
## Adversarial matrix и границы equivalence
## Differential proof baseline → candidate
## Conservation и non-target side effects
## Критерии приёмки и команды проверки
## Acceptance evidence, непроверенные области и пределы verdict
## Риски и отдельные follow-up задачи
```

План должен быть достаточно конкретным, чтобы приёмочное ревью проверяло уже
согласованный контракт, а не формировало его задним числом.
