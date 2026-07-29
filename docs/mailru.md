# Локальный read-only мост Mail.ru и Яндекс для Codex

Основной MCP-сервер намеренно работает только на чтение. Для разовой,
подтверждённой пользователем уборки есть отдельный локальный модуль
`mailru_approved_cleanup.py`: он принимает только уже просмотренные opaque
ссылки на письма, повторно проверяет UIDVALIDITY и выполняет исключительно
`UID MOVE`. Он не зарегистрирован как MCP-инструмент, не отправляет почту и не
вызывает `EXPUNGE`; перенос в архив или корзину обратим через интерфейс почты.

Мост подключает до 10 почтовых ящиков Mail.ru и Яндекса по IMAPS и даёт Codex только операции чтения: проверить подключение, перечислить папки, искать письма, читать письмо или цепочку и получать небольшие вложения в base64.

## Границы безопасности

- сервер запускается локально через stdio и не открывает сетевой порт;
- внешние точки жёстко ограничены `imap.mail.ru:993` и `imap.yandex.ru:993`; используется системная проверка TLS-сертификата и имени хоста;
- папки открываются с `readonly=True`, письма загружаются через `BODY.PEEK`;
- в MCP нет отправки, удаления, архивации, перемещения, изменения флагов или произвольной IMAP-команды;
- HTML переводится в текст, внешние ссылки и ресурсы не открываются;
- размеры выдачи, писем, вложений, пакетных запросов и таймаутов ограничены;
- пароль приложения хранится в Windows Credential Manager, а не в репозитории, `.env`, `config.toml` или Context Store.

## Что потребуется от пользователя

Для каждого ящика:

1. Включить доступ почтовых программ по IMAP в настройках Mail.ru. Официальные параметры: `imap.mail.ru`, порт `993`, SSL/TLS — <https://help.mail.ru/mail/login/mailer/>.
2. Создать отдельный пароль приложения Mail.ru в разделе «Безопасность → Пароли для внешних приложений»: <https://help.mail.ru/mail/faq/password/external/>. Обычный пароль от ящика не использовать.
3. Выбрать короткий идентификатор латиницей, например `personal` или `work`.
4. Сохранить пароль локально интерактивной командой из корня проекта:

   ```powershell
   .\mailru_credential.ps1 -AccountId personal -Address name@mail.ru
   ```

   Для Яндекса используется отдельный провайдер и логин без домена:

   ```powershell
   .\mailru_credential.ps1 -AccountId yandex_personal -Provider yandex -Address name@yandex.ru -Login name
   ```

   В Яндекс Почте должны быть включены IMAP и пароли приложений: <https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/others>. Создание отдельного пароля описано в <https://yandex.ru/support/id/ru/authorization/app-passwords>.

   Пароль вводится в скрытом поле и не передаётся в командной строке или чат.

5. Создать `%LOCALAPPDATA%\Codex\mailru-mcp\accounts.json` по образцу `mailru_accounts.example.json`. В этом JSON нет паролей:

   ```json
   {
     "version": 1,
     "accounts": [
       {
         "account_id": "personal",
         "address": "name@mail.ru",
         "credential_target": "CodexMailru/personal",
         "enabled": true
       }
     ]
   }
   ```

## Регистрация MCP

В `%USERPROFILE%\.codex\config.toml` используется локальный Python проекта:

```toml
[mcp_servers.mailru_mail]
command = 'C:\path\to\mcp-control-plane\.venv\Scripts\python.exe'
args = ['C:\path\to\mcp-control-plane\mailru_mcp.py']
startup_timeout_sec = 20
tool_timeout_sec = 60
```

После изменения `config.toml` Codex нужно перезапустить. Перед массовым анализом сначала выполнить `health(connect=true)`, затем проверить список папок и несколько тестовых писем.

## Локальная проверка без доступа к почте

```powershell
.\.venv\Scripts\python.exe -m py_compile mailru_imap.py mailru_mcp.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_mailru_mcp.py
```

Конфигурацию можно переопределить переменной `MAILRU_ACCOUNTS_FILE`. Допустимые защитные настройки: `MAILRU_TIMEOUT_SECONDS`, `MAILRU_MAX_MESSAGE_BYTES`, `MAILRU_MAX_ATTACHMENT_BYTES`.
