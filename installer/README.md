# pasevsu-installer-agent

Инсталационен агент за това хранилище. Изпълнява **реалната миграция при първо
стартиране** и публикува резултата: избира само агентите с ясно заявена промяна,
прави проверен бекъп, прилага промените в репото, пуска тестовете и публикува
ъпдейт.

Пакетът е **само стандартна библиотека** — не изисква `pip install`, `uv sync`
или виртуална среда, защото работата му е да *извърши* инсталацията.

## Бърз старт

```bash
python -m pasevsu_installer status                  # само четене: какво е станало
python -m pasevsu_installer install --dry-run       # план, без промени по диска
python -m pasevsu_installer install                 # истинската миграция
python -m pasevsu_installer install --agents installer-bootstrap,installer-docs
python -m pasevsu_installer install --no-publish    # без комит/таг
python -m pasevsu_installer reset --force           # изчисти състоянието
```

`install` е verb по подразбиране: `python -m pasevsu_installer` прави същото.

## Флагове

| Флаг | Значение |
| --- | --- |
| `--root <път>` | корен на хранилището (по подразбиране текущата папка) |
| `--config <път>` | конфигурация (по подразбиране `installer/installer.config.json`) |
| `--agents a,b` | ограничи изпълнението до изброените агенти |
| `--dry-run` | предсказва всяка операция, нищо не се записва |
| `--no-tests` | пропусни тестовите пакети (не се препоръчва) |
| `--no-publish` | пропусни комит/таг/пуш |
| `--force` | преизпълни дори когато състоянието е `done` |
| `--status` | съкращение за verb `status` |
| `--reset` | съкращение за verb `reset` (изисква `--force`) |

## Кодове за изход

| Код | Значение |
| --- | --- |
| `0` | успех (включително „няма заявени промени“) |
| `1` | неуспешна стъпка: прилагане, тестове или публикуване |
| `2` | невалидна конфигурация или липсващ аргумент |
| `3` | друг процес държи ключалката (`.installer/lock.json`) |

## Порядък на изпълнение

```mermaid
flowchart LR
    A[selection] --> B[backup]
    B --> C[apply]
    C --> D[tests]
    D --> E[publish]
```

1. **selection** — зарежда `installer/agents/*.json`, изхвърля агентите без
   `status: active` и задачите без `declared: true`, подрежда по зависимости.
   Всяко изхвърляне се записва с причина.
2. **backup** — `git tag installer-backup-<stamp>` (или ZIP извън git) и
   манифест; архивът се проверява по SHA-256 **преди** първата промяна.
3. **apply** — прилага операциите върху работното дърво и спира на първия
   провал. `master` не се пипа: при нужда се създава клон `installer/<версия>`.
4. **tests** — пуска всички пакети от `installer.config.json`. Липсващ
   инструмент става *измерен пропуск с причина*, никога „тихо мина“.
5. **publish** — комит `feat(installer): … (<версия>)`, annotated таг
   `installer-v<версия>`, release notes и `.installer/publish.json`.

## Операции, които агентът може да прилага

Променящи (само в `changes`): `write_file_if_absent`, `write_file`
(изисква `allow_overwrite: true`), `append_block_if_absent`, `replace_once`,
`emit_selection_manifest`.

Само за четене (само в `verify`): `assert_file_exists`, `assert_file_contains`,
`assert_block_present`.

Пътищата извън корена на хранилището се отхвърлят.

## Декларация на агент

```json
{
  "schema": "pasevsu.installer.agent.v1",
  "id": "installer-docs",
  "status": "active",
  "tasks": [
    {
      "id": "doc-workflow-guide",
      "declared": true,
      "changes": [{ "op": "write_file_if_absent", "params": { "path": "…", "content": "…" } }],
      "verify": [{ "op": "assert_file_exists", "params": { "path": "…" } }]
    }
  ]
}
```

Задача без `declared: true` не се изпълнява — причината се записва като
„няма ясно заявена промяна“.

## Артефакти от последното изпълнение

| Файл | Съдържание |
| --- | --- |
| `.installer/state.json` | фаза, брой пускания, състояние на задачите, бекъп, публикация |
| `.installer/journal.jsonl` | append-only журнал на събитията |
| `.installer/report.md` | човешки отчет: избрани, приложени, изключени, тестове |
| `.installer/selection.json` | машинен запис на селекцията |
| `.installer/backup.json` | манифест на бекъпа с SHA-256 |
| `.installer/publish.json` | комит, таг и разклонение |

`.installer/` е в `.gitignore` и никога не се комитва.

## Структура на пакета

```
installer/
├── installer.config.json     конфигурация (тестови пакети, публикуване)
├── VERSION                   текуща версия
├── agents/                   декларации на агентите
├── pasevsu_installer/        кодът (stdlib only)
├── tests/                    unittest тестове
└── RELEASE-NOTES/            бележки по версии (пишат се при публикуване)
```
