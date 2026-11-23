# mcp-superset-sso

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Superset 6.x](https://img.shields.io/badge/Superset-6.x-orange.svg)](https://superset.apache.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io/)

[MCP](https://modelcontextprotocol.io/)-сервер для [Apache Superset](https://superset.apache.org/), который умеет работать **от имени вызывающего пользователя**: человек логинится через Google, и каждый вызов инструмента выполняется под его собственной учётной записью Superset — его роли, RLS, владение объектами и записи в журнале действий.

Форк [bintocher/mcp-superset](https://github.com/bintocher/mcp-superset) (MIT, v0.3.1) — все 137 инструментов и их поведение взяты из upstream. Что добавлено в форке:

- **идентификация каждого пользователя через Google SSO** (`SUPERSET_MCP_AUTH_MODE=google-sso`, см. ниже);
- поддержка `description` в `superset_chart_create` / `superset_chart_update`;
- `superset_get_current_user` — показывает, от чьего имени сейчас работают инструменты;
- `mcp-superset-selftest` — проверка работы от имени пользователя на живом Superset без браузера.

Поведение upstream (один сервисный аккаунт на всех) осталось режимом по умолчанию, поэтому существующие установки ничего не теряют.

## Работа от имени пользователя (Google SSO)

### Зачем

В режиме `service` сервер держит один логин Superset: все пользователи MCP получают права этого аккаунта, и в журнале действий Superset все изменения записаны на него. В режиме `google-sso` цепочка идентификации такая:

```
MCP-клиент  --OAuth 2.1-->  этот сервер  --redirect-->  Google (hd=<ваш домен>)
                                 |  <- access token, подтверждённый email
                                 v
                     email  ->  пользователь Superset (поиск по email)
                                 |
                                 v
              API-токен Superset, выпущенный для его user_id  ->  все вызовы /api/v1
```

У пользователей, созданных через SSO, нет пароля, пригодного для `POST /api/v1/security/login`, поэтому сервер выпускает ровно такой же токен Flask-JWT-Extended, какой Superset выдаёт сам: HS256, `sub` = id пользователя, подпись ключом `SECRET_KEY` Superset (`JWT_SECRET_KEY`). Дальше Superset сам применяет права этого пользователя — никакой логики прав в MCP-сервере не дублируется.

### Настройка

```bash
SUPERSET_MCP_AUTH_MODE=google-sso

# Публичный HTTPS-адрес этого сервера (под ним лежат OAuth-метаданные и redirect от Google)
SUPERSET_MCP_PUBLIC_URL=https://mcp.example.com

# OAuth-клиент Google (тип Web application)
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...

# SECRET_KEY / JWT_SECRET_KEY вашего Superset
SUPERSET_JWT_SECRET=...

# Домены, которым разрешён доступ (также ограничивает выбор аккаунта в Google)
SUPERSET_MCP_ALLOWED_DOMAINS=example.com

# Сервисный аккаунт — нужен только чтобы найти пользователя Superset по email (права Admin)
SUPERSET_BASE_URL=http://superset:8088
SUPERSET_USERNAME=mcp_service
SUPERSET_PASSWORD=...
```

Дополнительно: `SUPERSET_MCP_TOKEN_TTL` (время жизни выпускаемого токена, по умолчанию 600 с), `GOOGLE_HOSTED_DOMAIN`, `SUPERSET_JWT_ALGORITHM`, `SUPERSET_MCP_AUTO_CREATE_USERS` + `SUPERSET_MCP_DEFAULT_ROLE`.

Автосоздание пользователей выключено по умолчанию: Flask-AppBuilder формирует username из данных OAuth-провайдера, поэтому заранее созданная здесь запись с другим username может конфликтовать с SSO-входом по уникальному email. Лучше дать человеку один раз войти в Superset через Google — учётная запись создастся сама, а сервер найдёт её по email.

### Настройка в Google Cloud

В OAuth-клиенте (Web application) добавьте authorized redirect URI:

```
https://<SUPERSET_MCP_PUBLIC_URL>/auth/callback
```

Тот же клиент может обслуживать и вход в UI Superset (`https://<superset-host>/oauth-authorized/google`).

### Проверка

```bash
# без браузера: находит пользователя по email, выпускает его токен, обращается к Superset
mcp-superset-selftest someone@example.com

# что сервер сообщает о себе
curl https://mcp.example.com/health

# без авторизации запросы отклоняются с OAuth-челленджем
curl -i -X POST https://mcp.example.com/mcp
```

Из MCP-клиента вызовите `superset_get_current_user` — он вернёт учётную запись Superset, от имени которой работают инструменты.

### Безопасность

- Процесс может действовать от имени любого пользователя Superset, поэтому к `SUPERSET_JWT_SECRET` относитесь как к самому секрету Superset: храните в env-файле, доступном только сервису, и ротируйте одновременно в обоих местах.
- Всегда терминируйте TLS перед сервером — Google делает redirect только на HTTPS.
- Держите `SUPERSET_MCP_ALLOWED_DOMAINS` заполненным: без него доступ к поиску по email получит любой Google-аккаунт, который принимает OAuth-клиент.
- Референс-конфигурация Docker и nginx — в каталоге [`deploy/`](deploy/).

## Сравнение с другими MCP-серверами для Superset

| Возможность | **mcp-superset** | [superset-mcp](https://github.com/aptro/superset-mcp) | [superset-mcp (Winding2020)](https://github.com/Winding2020/superset-mcp) | [superset-mcp-server](https://github.com/LiusCraft/superset-mcp-server) |
|-------------|:-:|:-:|:-:|:-:|
| **Всего инструментов** | **137** | 60 | 31 | 4 |
| Язык | Python | Python | TypeScript | TypeScript |
| Дашборды CRUD | 15 | 5 | 8 | - |
| Нативные фильтры | **5** | - | - | - |
| Графики CRUD | 11 | 5 | 7 | - |
| Базы данных | 18 | 14 | 1 | 1 |
| Датасеты | 11 | 3 | 7 | - |
| SQL Lab | 5 | 7 | 1 | 1 |
| **Безопасность (пользователи/роли)** | **26** | 2 | - | - |
| **Row Level Security** | **5** | - | - | - |
| **Группы** | **9** | - | - | - |
| **Аудит прав** | **да** | - | - | - |
| **Grant/revoke доступа** | **да** | - | - | - |
| **Авто-синхр. datasource_access** | **да** | - | - | - |
| Отчёты и аннотации | 10 | - | - | - |
| Теги | 7 | 7 | - | - |
| Экспорт/импорт ассетов | да | - | - | - |
| **Защита: флаги подтверждения** | **14 типов** | - | - | - |
| **Защита: блокировка DDL/DML** | **да** | - | - | - |
| **Защита: системные роли** | **да** | - | - | - |
| Транспорт | HTTP, SSE, stdio | stdio | stdio | stdio |
| Аутентификация | JWT + авто-refresh + CSRF | Username/password + файл токена | Username/password или токен | LDAP |
| Версии Superset | 6.0.1 | 4.1.1 | не указано | не указано |
| CLI с параметрами | `--host --port --transport` | - | - | - |
| PyPI | `mcp-superset` | `superset-mcp` | `superset-mcp` (npm) | - |
| uvx | **да** | - | - | - |
| Лицензия | MIT | MIT | - | Apache 2.0 |

**Ключевые отличия:**
- Единственный MCP-сервер с **полным управлением безопасностью** (пользователи, роли, RLS, группы, аудит прав)
- Единственный с **встроенной защитой** (флаги подтверждения, блокировка DDL/DML)
- Единственный с **управлением нативными фильтрами дашбордов**
- Единственный с **автоматической синхронизацией datasource_access**
- Единственный с **несколькими транспортами** (HTTP, SSE, stdio)
- Единственный с **настраиваемым CLI** (`--host`, `--port`, `--transport`, `--env-file`)

## Возможности

- **137 MCP-инструментов**, покрывающих полный REST API Superset
- **Управление дашбордами** — CRUD, копирование, публикация, экспорт/импорт, встраивание, нативные фильтры
- **Управление графиками** — CRUD, копирование, получение данных, экспорт/импорт, прогрев кэша
- **Управление базами данных** — CRUD, проверка подключения, интроспекция схем/таблиц, валидация SQL
- **Управление датасетами** — CRUD, дублирование, обновление схемы, экспорт/импорт
- **SQL Lab** — выполнение запросов, форматирование, оценка стоимости, экспорт результатов
- **Безопасность** — пользователи, роли, права, Row Level Security (RLS), группы
- **Автоматизация доступа** — grant/revoke с автоматической синхронизацией datasource_access
- **Аудит** — матрица прав доступа (пользователь x дашборды x датасеты x RLS)
- **Теги, отчёты, аннотации, сохранённые запросы** — полный CRUD
- **Экспорт/импорт ассетов** — полный бэкап и восстановление инстанса
- **Встроенная защита** — подтверждения для деструктивных операций, блокировка DDL/DML в SQL Lab
- **JWT-аутентификация** с автоматическим обновлением токенов и CSRF
- **Транспорты**: Streamable HTTP, SSE, stdio

## Быстрый старт

### Установка

Форк не публикуется в PyPI — ставится из git:

```bash
# pip
pip install "git+https://github.com/AliakseiAliaksandrau/mcp-superset-sso.git@main"

# uv (рекомендуется)
uv pip install "git+https://github.com/AliakseiAliaksandrau/mcp-superset-sso.git@main"

# Из клона (editable, для разработки)
git clone https://github.com/AliakseiAliaksandrau/mcp-superset-sso.git
cd mcp-superset-sso && uv pip install -e ".[dev]"
```

Развёртывание в контейнере — см. [`deploy/`](deploy/).

### Конфигурация

Создайте файл `.env` в текущей директории или установите переменные окружения:

```env
# Обязательные
SUPERSET_BASE_URL=https://superset.example.com
SUPERSET_USERNAME=admin
SUPERSET_PASSWORD=your_password

# Необязательные
SUPERSET_AUTH_PROVIDER=db          # db (по умолчанию) или ldap
SUPERSET_MCP_HOST=127.0.0.1       # Адрес сервера (по умолчанию: 127.0.0.1)
SUPERSET_MCP_PORT=8001             # Порт сервера (по умолчанию: 8001)
SUPERSET_MCP_TRANSPORT=streamable-http  # streamable-http (по умолчанию), sse или stdio
```

#### Аутентификация через session cookie (SSO/OAuth)

Если Superset работает за SSO (OAuth/OIDC/SAML), вход по логину и паролю
через REST API недоступен. Вместо этого укажите **session cookie** из браузера:

| Переменная | Описание |
| --- | --- |
| `SUPERSET_SESSION_COOKIE` | Значение session cookie из браузера. Если задано, используется вместо логина и пароля. |
| `SUPERSET_SESSION_COOKIE_NAME` | Имя cookie. По умолчанию `session`. |

Скопируйте cookie из инструментов разработчика браузера (Application →
Cookies → домен Superset → `session`). MCP-сервер отправляет её с каждым
запросом и получает с ней CSRF-токены. Сессию нельзя продлить на стороне
сервера, поэтому после истечения вставьте новое значение и перезапустите сервер.

Что стоит проверить перед использованием режима:

- **Имя cookie не всегда `session`.** Если в `superset_config.py` задан
  `SESSION_COOKIE_NAME`, имя будет другим (например, `s`). Возьмите имя из
  инструментов разработчика и укажите его в `SUPERSET_SESSION_COOKIE_NAME`.
- **Инстанс должен принимать сессионную аутентификацию на `/api/v1/`.** Там, где
  REST API настроен только на JWT, запрос с рабочей cookie всё равно вернёт
  `401 {"msg": "Missing Authorization Header"}` - обновление cookie не поможет,
  подходит только режим JWT.

Относитесь к cookie как к учётным данным: она даёт полный доступ к вашей учётной
записи Superset на всё время жизни сессии. Храните её в `.env` (файл в
gitignore), а не в истории команд и не в закоммиченных конфигах.

### Запуск

```bash
# Через CLI (после установки)
mcp-superset

# Через Python-модуль
python -m mcp_superset

# Через uv из исходников
uv run mcp-superset

# С пользовательскими параметрами
mcp-superset --host 0.0.0.0 --port 9000 --transport sse

# С указанием .env файла
mcp-superset --env-file /path/to/.env

# Через stdio (для Claude Desktop, Cursor и др.)
mcp-superset --transport stdio
```

### Параметры CLI

| Параметр | По умолчанию | Переменная окружения | Описание |
|----------|-------------|---------------------|----------|
| `--host` | `127.0.0.1` | `SUPERSET_MCP_HOST` | Адрес привязки сервера |
| `--port` | `8001` | `SUPERSET_MCP_PORT` | Порт сервера |
| `--transport` | `streamable-http` | `SUPERSET_MCP_TRANSPORT` | Транспорт: `streamable-http`, `sse`, `stdio` |
| `--env-file` | авто | — | Путь к `.env` файлу |
| `--version` | — | — | Показать версию и выйти |

### Подключение к MCP-клиентам

#### Claude Code

Добавьте в `.mcp.json` проекта:

```json
{
  "mcpServers": {
    "superset": {
      "type": "http",
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

Затем запустите сервер: `mcp-superset` (см. [Запуск](#запуск)).

#### Claude Desktop

Добавьте в `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "superset": {
      "command": "uvx",
      "args": ["mcp-superset", "--transport", "stdio"],
      "env": {
        "SUPERSET_BASE_URL": "https://superset.example.com",
        "SUPERSET_USERNAME": "admin",
        "SUPERSET_PASSWORD": "your_password"
      }
    }
  }
}
```

#### Cursor / Windsurf

```json
{
  "mcpServers": {
    "superset": {
      "command": "uvx",
      "args": ["mcp-superset", "--transport", "stdio"],
      "env": {
        "SUPERSET_BASE_URL": "https://superset.example.com",
        "SUPERSET_USERNAME": "admin",
        "SUPERSET_PASSWORD": "your_password"
      }
    }
  }
}
```

#### Другие MCP-клиенты

Любой MCP-совместимый клиент может подключиться через:
- **Streamable HTTP**: `http://<host>:<port>/mcp`
- **SSE**: `http://<host>:<port>/sse`
- **stdio**: пайп к `mcp-superset --transport stdio`

## Доступные инструменты (137)

### Дашборды (15 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_dashboard_list` | Список дашбордов с фильтрацией и пагинацией |
| `superset_dashboard_get` | Информация о дашборде по ID |
| `superset_dashboard_create` | Создать дашборд |
| `superset_dashboard_update` | Изменить свойства дашборда |
| `superset_dashboard_delete` | Удалить дашборд (нужно подтверждение) |
| `superset_dashboard_copy` | Скопировать дашборд |
| `superset_dashboard_publish` | Опубликовать черновик дашборда |
| `superset_dashboard_unpublish` | Снять дашборд с публикации |
| `superset_dashboard_charts` | Графики на дашборде |
| `superset_dashboard_datasets` | Датасеты, которые использует дашборд |
| `superset_dashboard_export` | Экспорт дашборда в ZIP (base64) |
| `superset_dashboard_import` | Импорт дашборда из ZIP-файла |
| `superset_dashboard_embedded_get` | Настройки встраивания (embedded) |
| `superset_dashboard_embedded_set` | Включить встраивание для разрешённых доменов |
| `superset_dashboard_embedded_delete` | Выключить встраивание |

### Фильтры дашбордов (5 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_dashboard_filter_list` | Нативные фильтры дашборда |
| `superset_dashboard_filter_add` | Добавить нативный фильтр (ID нужного формата генерируется сам) |
| `superset_dashboard_filter_update` | Изменить существующий нативный фильтр |
| `superset_dashboard_filter_delete` | Удалить нативный фильтр (нужно подтверждение) |
| `superset_dashboard_filter_reset` | Удалить все фильтры (нужно подтверждение) |

### Графики (11 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_chart_list` | Список графиков с фильтрацией и пагинацией |
| `superset_chart_get` | Информация о графике по ID |
| `superset_chart_create` | Создать график |
| `superset_chart_update` | Изменить свойства графика |
| `superset_chart_delete` | Удалить график (нужно подтверждение) |
| `superset_chart_copy` | Скопировать график |
| `superset_chart_data` | Выполнить запрос графика и получить данные |
| `superset_chart_get_data` | Данные сохранённого графика |
| `superset_chart_export` | Экспорт графика в ZIP (base64) |
| `superset_chart_import` | Импорт графика из ZIP-файла |
| `superset_chart_cache_warmup` | Прогреть кэш графика |

### Базы данных (18 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_database_list` | Список подключений к БД |
| `superset_database_get` | Информация о БД |
| `superset_database_create` | Зарегистрировать новое подключение к БД |
| `superset_database_update` | Изменить настройки БД |
| `superset_database_delete` | Удалить БД (нужно подтверждение) |
| `superset_database_test_connection` | Проверить подключение к БД |
| `superset_database_schemas` | Схемы в БД |
| `superset_database_tables` | Таблицы в схеме |
| `superset_database_catalogs` | Каталоги (для БД с несколькими каталогами) |
| `superset_database_connection_info` | Информация о строке подключения |
| `superset_database_function_names` | Доступные SQL-функции |
| `superset_database_related_objects` | Графики и датасеты, которые используют эту БД |
| `superset_database_validate_sql` | Проверить синтаксис SQL |
| `superset_database_validate_parameters` | Проверить параметры подключения |
| `superset_database_select_star` | Сгенерировать запрос SELECT * для таблицы |
| `superset_database_table_metadata` | Метаданные таблицы: колонки и индексы |
| `superset_database_export` | Экспорт настроек БД в ZIP |
| `superset_database_available_engines` | Поддерживаемые движки БД |

### Датасеты (11 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_dataset_list` | Список датасетов с фильтрацией |
| `superset_dataset_get` | Информация о датасете, включая колонки и метрики |
| `superset_dataset_create` | Создать датасет из таблицы или SQL-запроса |
| `superset_dataset_update` | Изменить свойства датасета |
| `superset_dataset_delete` | Удалить датасет (нужно подтверждение) |
| `superset_dataset_duplicate` | Скопировать датасет |
| `superset_dataset_refresh_schema` | Обновить колонки из источника |
| `superset_dataset_related_objects` | Графики, которые используют этот датасет |
| `superset_dataset_export` | Экспорт датасета в ZIP |
| `superset_dataset_import` | Импорт датасета из ZIP |
| `superset_dataset_get_or_create` | Найти существующий датасет или создать новый |

### SQL Lab и запросы (13 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_sqllab_execute` | Выполнить SQL-запрос (только SELECT) |
| `superset_sqllab_format_sql` | Отформатировать SQL |
| `superset_sqllab_results` | Результаты завершённого запроса |
| `superset_sqllab_estimate_cost` | Оценить стоимость выполнения запроса |
| `superset_sqllab_export_csv` | Экспорт результатов запроса в CSV |
| `superset_query_list` | Список выполненных запросов |
| `superset_query_get` | Информация о запросе и его результаты |
| `superset_query_stop` | Остановить выполняющийся запрос |
| `superset_saved_query_list` | Список сохранённых запросов |
| `superset_saved_query_create` | Сохранить новый запрос |
| `superset_saved_query_get` | Информация о сохранённом запросе |
| `superset_saved_query_update` | Изменить сохранённый запрос |
| `superset_saved_query_delete` | Удалить сохранённый запрос (нужно подтверждение) |

### Безопасность и управление доступом (26 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_get_current_user` | Текущий аутентифицированный пользователь |
| `superset_get_current_user_roles` | Роли текущего пользователя |
| `superset_user_list` | Список пользователей с фильтрацией |
| `superset_user_get` | Информация о пользователе |
| `superset_user_create` | Создать пользователя |
| `superset_user_update` | Изменить свойства пользователя |
| `superset_user_delete` | Удалить пользователя (нужно подтверждение) |
| `superset_role_list` | Список ролей |
| `superset_role_get` | Информация о роли |
| `superset_role_create` | Создать роль |
| `superset_role_update` | Изменить название или описание роли |
| `superset_role_delete` | Удалить роль (нужно подтверждение, системные роли защищены) |
| `superset_permission_list` | Все доступные права |
| `superset_role_permissions_get` | Права, назначенные роли |
| `superset_role_permission_add` | Задать права роли (полная замена, нужно подтверждение) |
| `superset_role_copy_permissions` | Скопировать все права одной роли в другую |
| `superset_dashboard_grant_role_access` | Дать роли доступ к дашборду и его датасетам |
| `superset_dashboard_revoke_role_access` | Отозвать у роли доступ к датасетам дашборда |
| `superset_bulk_user_role_add` | Добавить роль нескольким пользователям (по ID или по текущей роли) |
| `superset_bulk_user_role_remove` | Убрать роль у нескольких пользователей |
| `superset_bulk_user_role_replace` | Заменить одну роль другой у всех подходящих пользователей |
| `superset_rls_list` | Список правил Row Level Security |
| `superset_rls_get` | Информация о правиле RLS |
| `superset_rls_create` | Создать правило RLS |
| `superset_rls_update` | Изменить правило RLS (нужны и роли, и таблицы) |
| `superset_rls_delete` | Удалить правило RLS (нужно подтверждение) |

### Группы (9 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_group_list` | Список групп |
| `superset_group_get` | Информация о группе с участниками и ролями |
| `superset_group_create` | Создать группу |
| `superset_group_update` | Изменить название группы |
| `superset_group_delete` | Удалить группу |
| `superset_group_add_users` | Добавить пользователей в группу |
| `superset_group_remove_users` | Убрать пользователей из группы |
| `superset_group_add_roles` | Добавить роли группе |
| `superset_group_remove_roles` | Убрать роли у группы |

### Теги (7 инструментов)

| Инструмент | Описание |
|------------|----------|
| `superset_tag_list` | Список тегов |
| `superset_tag_get` | Информация о теге |
| `superset_tag_create` | Создать тег (можно сразу привязать к объектам) |
| `superset_tag_update` | Изменить тег |
| `superset_tag_delete` | Удалить тег (нужно подтверждение) |
| `superset_tag_get_objects` | Объекты, связанные с тегом |
| `superset_tag_bulk_create` | Создать несколько тегов за раз |

### Система и отчёты (21 инструмент)

| Инструмент | Описание |
|------------|----------|
| `superset_report_list` | Список отчётов по расписанию |
| `superset_report_get` | Информация об отчёте |
| `superset_report_create` | Создать отчёт по расписанию |
| `superset_report_update` | Изменить отчёт |
| `superset_report_delete` | Удалить отчёт (нужно подтверждение) |
| `superset_annotation_layer_list` | Список слоёв аннотаций |
| `superset_annotation_layer_get` | Информация о слое аннотаций |
| `superset_annotation_layer_create` | Создать слой аннотаций |
| `superset_annotation_layer_update` | Изменить слой аннотаций |
| `superset_annotation_layer_delete` | Удалить слой аннотаций (нужно подтверждение) |
| `superset_annotation_list` | Аннотации в слое |
| `superset_annotation_get` | Информация об аннотации |
| `superset_annotation_create` | Создать аннотацию |
| `superset_annotation_update` | Изменить аннотацию |
| `superset_annotation_delete` | Удалить аннотацию (нужно подтверждение) |
| `superset_recent_activity` | Недавние действия пользователя |
| `superset_log_list` | Журнал действий (audit log) |
| `superset_get_menu` | Структура меню Superset |
| `superset_get_base_url` | Настроенный базовый URL Superset |
| `superset_assets_export` | Экспорт всех ассетов Superset в ZIP |
| `superset_assets_import` | Импорт ассетов из ZIP-файла |

### Аудит (1 инструмент)

| Инструмент | Описание |
|------------|----------|
| `superset_permissions_audit` | Построить полную матрицу прав доступа |

## Механизмы защиты

Сервер включает обширную встроенную защиту от случайной потери данных.

### Флаги подтверждения

Деструктивные операции требуют явного подтверждения:

| Операция | Требуемый флаг | Что показывает |
|----------|---------------|----------------|
| Удаление дашборда | `confirm_delete=True` | Название, slug, количество графиков |
| Удаление графика | `confirm_delete=True` | Привязанные дашборды |
| Удаление датасета | `confirm_delete=True` | Затронутые графики и дашборды |
| Удаление БД | `confirm_delete=True` | Затронутые датасеты, графики |
| Удаление RLS | `confirm_delete=True` | Clause, роли, датасеты |
| Удаление роли | `confirm_delete=True` | Блокирует системные роли |
| Удаление пользователя | `confirm_delete=True` | Блокирует удаление сервисного аккаунта |
| Обновление params графика | `confirm_params_replace=True` | — |
| Обновление columns датасета | `confirm_columns_replace=True` | — |
| Изменение URI БД | `confirm_uri_change=True` | Затронутые графики/дашборды |
| Обновление ролей пользователя | `confirm_roles_replace=True` | Текущие роли |
| Установка прав роли | `confirm_full_replace=True` | — |
| Выдача доступа к дашборду | `confirm_grant=True` | Результат dry-run |
| Отзыв доступа к дашборду | `confirm_revoke=True` | Результат dry-run |
| Массовое добавление роли | `confirm=True` | Число пользователей, пример списка |
| Массовое удаление роли | `confirm=True` | Число пользователей, не даёт убрать последнюю роль |
| Массовая замена роли | `confirm=True` | Старая и новая роль, число пользователей |
| Копирование прав роли | `confirm=True` | Число прав у исходной и целевой роли |

### Автоматическая защита

- **Блокировка DDL/DML** — SQL Lab отклоняет `DROP`, `DELETE`, `UPDATE`, `INSERT`, `TRUNCATE`, `ALTER`, `CREATE`, `GRANT`, `REVOKE` (SQL-комментарии вырезаются перед проверкой)
- **Защита системных ролей** — нельзя удалить Admin, Alpha, Gamma, Public
- **Защита сервисного аккаунта** — нельзя удалить MCP-пользователя
- **Безопасность RLS** — `rls_update` требует одновременно `roles` и `tables`, чтобы данные не стёрлись молча
- **ID нативных фильтров** — автоматически генерируются в формате `NATIVE_FILTER-<uuid>`
- **Валидация графиков** — отклоняет графики без `granularity_sqla` (без него не работают фильтры по времени на дашборде)
- **Авто-синхронизация** — права `datasource_access` автоматически синхронизируются при изменении ролей дашборда

## Архитектура

```
superset-mcp/
├── pyproject.toml              # Конфигурация пакета
├── .env.example                # Шаблон переменных окружения
├── LICENSE                     # Лицензия MIT
├── README.md                   # Документация (этот файл)
├── CHANGELOG.md                # История версий
└── src/mcp_superset/
    ├── __init__.py             # Инициализация с __version__
    ├── __main__.py             # CLI с argparse
    ├── server.py               # Настройка FastMCP-сервера
    ├── auth.py                 # JWT-аутентификация (login, refresh, CSRF)
    ├── client.py               # HTTP-клиент (авто-аутентификация, retry, RISON-пагинация)
    ├── models.py               # Pydantic-модели
    └── tools/
        ├── __init__.py         # register_all_tools()
        ├── helpers.py          # Авто-синхронизация datasource_access
        ├── dashboards.py       # Дашборды + фильтры (20)
        ├── charts.py           # Графики (11)
        ├── databases.py        # Базы данных (18)
        ├── datasets.py         # Датасеты (11)
        ├── queries.py          # SQL Lab + сохранённые запросы (13)
        ├── security.py         # Пользователи, роли, права, RLS, массовые операции (26)
        ├── groups.py           # Группы (9)
        ├── audit.py            # Аудит прав (1)
        ├── tags.py             # Теги (7)
        └── system.py           # Отчёты, аннотации, логи, ассеты (21)
```

## Совместимость с Superset

- **Протестировано с**: Apache Superset 6.0.1
- **Аутентификация**: JWT (рекомендуется) — API Key (`sst_*`) не реализован в Superset
- **Требуемый пользователь**: роль Admin (для полного доступа к API)

### Рекомендуемая настройка Superset

Добавьте в `superset_config.py`:

```python
from datetime import timedelta

# Увеличить время жизни JWT-токена (по умолчанию 15 мин)
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

# Максимальный размер страницы API
FAB_API_MAX_PAGE_SIZE = 100
```

## Разработка

### Настройка окружения

```bash
git clone https://github.com/AliakseiAliaksandrau/mcp-superset-sso.git
cd mcp-superset-sso

# Создать виртуальное окружение и установить в режиме разработки
uv venv
uv pip install -e ".[dev]"

# Скопировать и настроить .env
cp .env.example .env
# Отредактируйте .env с вашими данными Superset
```

### Локальный запуск

```bash
# Запуск из исходников
uv run python -m mcp_superset

# Или через CLI
uv run mcp-superset --port 8001
```

### Запуск тестов

```bash
uv run python test_all_tools.py
```

## Особенности API Superset

MCP-сервер обрабатывает их сам, но при отладке полезно о них знать:

| Особенность | Подробности |
|-------------|-------------|
| Пагинация через RISON | Superset игнорирует `page`/`page_size` в query-параметрах; нужен RISON в параметре `q` |
| Нужен CSRF | Для всех POST/PUT/DELETE нужны заголовок `X-CSRFToken` и session cookie |
| Нужен Referer | SQL Lab возвращает 403 без заголовка `Referer` |
| API тегов возвращает `{}` | При создании тега ID не возвращается; его приходится искать в списке |
| Обновлению тега нужен `name` | Поле обязательно, даже если не меняется |
| Права роли заменяются | `POST /security/roles/{id}/permissions` заменяет ВСЕ права роли |
| RLS заменяется | `PUT /rowlevelsecurity/{id}` заменяет ВСЕ переданные поля |
| Колонки датасета заменяются | `PUT /dataset/{id}` с `columns` заменяет ВСЕ колонки |
| Копирование дашборда | Нужен `json_metadata` (можно `"{}"`) |
| ID нативных фильтров | Должны быть в формате `NATIVE_FILTER-<uuid>` |
| `filter_time` требует `granularity_sqla` | Графики без него молча игнорируют фильтры по времени |
| Форматирование чисел | `SMART_NUMBER` сокращает числа; для точных значений используйте `,d` или `,.2f` |

## Лицензия

[MIT](LICENSE) — Stanislav Chernov ([@bintocher](https://github.com/bintocher))
