# Обработка узлов через Multica

Клиент и технические критерии находятся в `gooddaytoday/loginom-ai-agent`, ветка `multica`. В этом каталоге — только эксплуатационная обвязка. Ядро Multica, прежние слоты Swarm и данные Paperclip не изменяются. Старые инструкции сохранены в [history/paperclip](https://github.com/kartamyshev-dev/swarm/tree/swarm/ops/loginom-multica/history/paperclip).

## Установка одной копии инструментов

Linux x64: Bun 1.3.14 (0d9b296a), полный Node 24.19.0, Chromium revision 1243 / Playwright 1.63.0-alpha-2026-08-31. Системные пакеты: bubblewrap, xvfb, openbox, xauth, x11-utils, dbus-x11, curl, unzip, fonts-liberation и зависимости Chromium. Ubuntu 24 требует адресного AppArmor-разрешения `userns` для `/usr/bin/bwrap`; глобальное ограничение userns сохраняется.

Источники Node и браузерных ресурсов должны соответствовать `packages/product/loginom-release.json` клиента. Они установлены один раз в `~/.local/share/loginom-multica-tools`. Кандидат содержит собственные ресурсы; общий внешний runtime ему не нужен.

Экспорт небольшой копии из зафиксированного коммита swarm:

```sh
python3 ops/loginom-multica/scripts/package.py --repo /path/to/swarm --ref <commit> --out /new/operator-bundle
```

Перенести bundle в `~/.local/share/loginom-multica`. `VERSION.json` фиксирует коммит и контрольные суммы. Обновлять только после завершения использующих прежнюю копию карточек. Полный swarm и его зависимости на runtime не передавать; второй checkout для карточки не создавать.

Приватные постоянные файлы: `~/.config/loginom-multica/operator.json`, `auth.json`, `models.json`, `cards/<issue>/{worker,reviewer}.json`. Каталоги 0700, файлы 0600, вне Git и GC. [operator.example.json](operator.example.json) — шаблон без секретов. `auth.json` должен иметь формат OAuth Loginom CLI (provider→type/access/refresh/expires/accountId), не формат Codex. Подключается writable file bind; одна файловая блокировка сериализует модельные прогоны и обновления refresh token.

Генератор выполняет `provision-accounts.py --issue <UUID>`. Имена, пароли, связь карточки и ролей сохраняются до создания пользователей. Повтор проверяет вход с сохранёнными данными. Неопределённый исход требует сверки, а не смены пароля/повторного создания. После Done аккаунты и серверные пакеты остаются до ручной очистки.

## Работа в штатном checkout

```sh
multica repo checkout https://github.com/gooddaytoday/loginom-ai-agent.git --ref multica
~/.local/share/loginom-multica/scripts/build-candidate.sh "$PWD" "$PWD/.multica-node/current"
~/.local/share/loginom-multica/scripts/accept-node.sh --worktree "$PWD" \
  --config "$HOME/.config/loginom-multica/cards/<issue>/worker.json" \
  --node transform-crosstable --cli "$PWD/.multica-node/current/bin/loginom-ai-agent-cli" \
  --out "$PWD/.multica-node/attempts/<new-attempt>"
```

`LOGINOM_MULTICA_CONFIG` заменяет `--config`. Reviewer использует reviewer.json и checkout точного переданного SHA. Перед сборкой проверяются native owner marker и границы пути. Установка выбирает только Agent/Host с транзитивными зависимостями и `--linker=hoisted`: это требуется существующему file-алиасу plugin при выборочной установке; Desktop/Web UI не собираются. Runtime ставится по npm lockfile без devDependencies. Сборщик использует `--no-archive`, проверяет манифест и исходники, заменяет только один `current` через staging. При обычной ошибке прежний клиент сохраняется. Каждая команда установки/сборки ограничена 15 минутами; после превышения её дочерние процессы останавливаются, старый current сохраняется. Две операции rename не являются одной атомарной транзакцией: после SIGKILL в момент публикации current может отсутствовать; тогда пересобрать. Аварийные остатки не чистятся по PID/возрасту.

Перед Review нужен чистый опубликованный SHA:

```sh
LOGINOM_NODE_WORKFLOW=cli git -c core.hooksPath=.husky push
```

CLI-режим pre-push проверяет Core, Agent, Host. Обычный hook и полный CI остаются прежними. Не считать удачный push доказательством приёмки. Кандидат повторно используется только после проверки исходников, закреплённых входов сборки и полной целостности.

Приёмка создаёт профиль с каналом из манифеста, запускает foreground Xvfb/Openbox/bwrap, выполняет CLI с моделью `openai/gpt-6.1-sol` / `low` и пределом 7200 секунд. Независимые ожидания и административные данные не доступны модельному прогону. Cold-check вызывается из проверяемого клиентского SHA. PASS требует совпадения чисел/типов и подтверждений закрытия пакета/выхода. Убийство процесса не подтверждает закрытие. Журналы остаются приватными; результат не ослабляет durable journal.

## Диагностика и повторный запуск

При сбое сначала читать `attempts/<attempt>/evidence/result.json`, отредактированные `events.jsonl` и `stderr.txt`. Сверить SHA клиента, исходников и обвязки. Сырой профиль, stdout и приватные конфиги не публиковать. `CLI_SETUP_FAILED` указывает на подготовку соединения; ненулевой `cli_exit` и отсутствие oracle PASS не являются успешной обработкой узла.

При `PROFILE_BUSY`, `LOGINOM_RECOVERY_REQUIRED` или неизвестном исходе операции сохранить попытку и поставить Blocked. Нельзя удалять writer marker, повторять сомнительную операцию или считать остановку процесса закрытием пакета. Состояние сверяется в Loginom только для собственного аккаунта. Техническое восстановление клиента описано в его RUNBOOK; подтверждение `loginom recover` допускается после реальной сверки. По разрешённому продолжению создать новую попытку/профиль под той же парой аккаунтов и тем же проверяемым SHA. Старые каталоги остаются штатному GC. Повреждённый current пересобрать обычным build-candidate.sh.

## Публикация и процесс

Проверенный комментарий сохранить внутри попытки, затем:

```sh
python3 ~/.local/share/loginom-multica/scripts/publish-evidence.py --worktree "$PWD" \
  --config <role-config> --attempt "$PWD/.multica-node/attempts/<attempt>" \
  --content-file "$PWD/.multica-node/attempts/<attempt>/comment.md" --parent <trigger-comment-uuid>
```

Используется штатный `multica issue comment add --attachment`. Helper перечитывает комментарий и скачивает каждое вложение для проверки SHA256. Он не выставляет Done и не удаляет данные. До подтверждённой receipt Done запрещён. Разрешены только result, отредактированные события и stderr, результат/cleanup oracle; клиент, зависимости, профили и сырые журналы не прикладываются.

Инструкции [сквада](instructions/squad.md), [Генератора](instructions/generator.md), [исполнителя](instructions/worker.md), [Ловца](instructions/reviewer.md) загружаются через API. `configure.py` требует успешную pilot receipt; API-параметры `MULTICA_SERVER_URL`, `MULTICA_TOKEN`, `MULTICA_WORKSPACE_ID`, токен не записывается в репозиторий. Лидер и лимит 1 сохраняются, результат проверяется чтением API.

## Хранение и штатная очистка

В управляемом checkout только `.multica-node/current`, `staging`, `attempts/<attempt>`. Скрипт удаляет лишь собственный временный каталог после штатного завершения сборки. Истории клиентов по SHA нет. Блокировка предотвращает замену current во время нашей приёмки; daemon не читает эту блокировку, поэтому запуск должен оставаться активным штатным запуском Multica.

После Done применяется только native GC Multica v0.6.1: root TTL 24 часа от последнего обновления Done/Closed-карточки, проверка каждые 2 часа; активный запуск или ошибка откладывает удаление. Вложенные `node_modules`, `.next`, `.turbo` могут удаляться через 12 часов после завершения неактивного запуска. Перед повторным использованием integrity проверяется и клиент пересобирается при повреждении. Это не общий лимит места: зависимости checkout, Bun/Git caches, staging и отчёты занимают место отдельно.

GC удаляет сборки, зависимости, профили и отчёты вместе с native root. Опубликованные вложения, установленная обвязка, постоянные приватные конфиги, аккаунты и серверные пакеты остаются. Отдельного очистителя, таймера и заключительной команды Ловца нет. Настройки daemon не меняются. Удаление всего root применимо к managed Git checkout, а не произвольному local_directory.

## Проверки и переключение

```sh
python3 -m unittest discover -s ops/loginom-multica/tests -v
```

Default-тесты используют временные файлы и поддельные команды. Linux-квалификация и реальный пилот проводятся отдельно. Основной ресурс остаётся `loginom` до успешной Кросс-таблицы: реальные аккаунты/приёмка, независимая проверка того же SHA, передача вопроса/ответа, возврат на исправление, проверенные вложения и Done без продолжения переписки. При дефекте сохраняются доказательства, ресурс не переключается. Текущий checkpoint: [STATUS.md](https://github.com/kartamyshev-dev/swarm/blob/swarm/ops/loginom-multica/STATUS.md).
