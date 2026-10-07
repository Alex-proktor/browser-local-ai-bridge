# Устойчивый RDC на Windows

RDC отвечает за соединение браузера с ПК. Долгие сборки и выполнение проектного кода следует передавать отдельному локальному worker через durable mailbox, описанный в `durable-worker.md`. Worker должен запускаться независимо от дерева процессов RDC: запуск worker командой `start_process` внутри RDC не создаёт такую изоляцию.

## Подтверждённая причина обрывов

7 октября 2026 экспортированный отчёт Kaspersky показал `PDM:Trojan.Win32.Generic`, завершение RDC и удаление его `dist/index.js`. В той же цепочке антивирус завершил и удалил `build_editor.ps1`. Это объясняет повторное исчезновение файла после восстановления. Сам по себе такой отчёт не доказывает ложное срабатывание; требуется проверка цепочки выполнения и решение для конкретного обнаружения.

Прежняя реализация имела дополнительные дефекты:

- watchdog был Disabled; интерактивный PowerShell мог мешать работе видимым окном;
- UTC timestamp приводился к локальному `DateTime`, поэтому короткий backoff мог задерживать восстановление на разницу часовых поясов;
- проверялся только hash entrypoint, а отсутствующие зависимости оставались незамеченными;
- копирование entrypoint из recovery обходило лимит ремонтов;
- launcher мог записать пустой ExitCode и вернуть успех после падения Node;
- Scheduled Tasks зависели от изменяемого Git checkout.

## Новая схема

- `install-rdc-resilience.ps1` устанавливает скрипты в каталог `%LOCALAPPDATA%\RemoteDesktopCommander\releases\<hash>`. Task actions указывают на конкретный release, поэтому checkout другого branch не меняет runtime. Повторная установка тех же файлов использует тот же release.
- Agent остаётся интерактивной пользовательской задачей: это требуется инструментам рабочего стола. Watchdog запускается в фоновом S4U session без окна и без сохранения пароля. Однократная установка требует elevated PowerShell.
- Watchdog проверяет процесс каждую минуту. Только он управляет автоматическими перезапусками; Task Scheduler Agent не создаёт параллельную retry-цепочку. UTC backoff ограничен 30 минутами, учитывает смену часов, сбрасывается при живом процессе. `IgnoreNew`, startup grace и mutex launcher предотвращают дублирование.
- Bootstrap проверяет version, SHA-256 entrypoint и наличие всех объявленных зависимостей. Отдельный Node smoke импортирует MCP SDK и Supabase из глобальной установки.
- Повреждённый пакет переводит supervision в `integrity_blocked`. Автоматических npm reinstall и восстановления удалённых антивирусом файлов нет. Коррелированное событие `avp` записывается в диагностику.
- Stdout/stderr сохраняются отдельно; status log использует единый UTF-8 и реальный ExitCode. `supervision-status.json` содержит свежий результат проверки процесса. `process_alive` не заменяет backend online/RPC ping.
- Installer включает TaskScheduler Operational, чтобы последующие изменения/отключения задач можно было расследовать. Battery settings не останавливают Agent/Watchdog.

## Установка и восстановление

1. Проверить отчёт антивируса. Если он удаляет RDC, решить конфликт для конкретной цепочки выполнения; не добавлять весь `node.exe`, весь npm или все PowerShell scripts в исключения. Настройки защиты installer не меняет. См. [инструкцию Kaspersky](https://support.kaspersky.com/help/Kaspersky/Win21.5/en-US/227390.htm).
2. При отсутствующем/повреждённом пакете и остановленном RDC выполнить `tools/windows/repair-rdc-runtime.ps1`. Это явное обслуживание глобальной версии 0.2.51, а не фоновый цикл. Скрипт отказывается менять живой пакет.
3. Один раз запустить `tools/windows/install-rdc-resilience.ps1` в elevated PowerShell. Обе задачи будут Enabled; текущий Agent process не перезапускается. Результат установки записывается в `installation-status.json`.
4. Запустить Watchdog task. При целостном пакете и отсутствии процесса он стартует Agent. Подтвердить глобальный command line, Agent Running, Watchdog S4U/Enabled, backend online и RPC ping.
5. После lifecycle changes провести один kill/recovery smoke, проверить отсутствие duplicate remote agents и новых AV events. Reboot/login и sleep/wake требуют отдельных фактических проверок.

## Ограничения и rollback

Supervision восстанавливает умерший процесс, но не отменяет решение endpoint protection. Если Kaspersky снова удалит пакет, статус будет `integrity_blocked`; это требует обслуживания и проверки политики, а не бесконечных repairs. Для длинных задач использовать независимый durable worker и читать его результат через mailbox.

Ранее установленные определения задач сохраняются в release как `*.before.xml`. Для rollback можно восстановить эти определения через Task Scheduler. Старые releases автоматически не удаляются. RDC auth/config не меняются и не удаляются.
