# Steam Deck MCP Kit

Минимальный пакет для управления Steam Deck через SSH из Codex MCP.

## Что внутри

- `steamdeck_mcp.py` - MCP server с инструментами для SSH, EmuDeck, ROM/Bios upload, Steam ROM Manager и диагностики.
- `.env.example` - шаблон настроек подключения.
- `requirements.txt` - минимальные Python-зависимости.
- `test_connection.ps1` - быстрая проверка SSH/MCP без запуска Codex.

## Подготовка Steam Deck

1. В Desktop Mode включить SSH:

   ```bash
   sudo systemctl enable --now sshd
   ```

2. Узнать IP Deck:

   ```bash
   ip addr
   ```

3. Убедиться, что пароль пользователя `deck` известен:

   ```bash
   passwd
   ```

## Установка на Windows

1. Распаковать папку, например в:

   ```text
   C:\Users\<you>\Downloads\steamdeck-mcp-kit
   ```

2. Открыть PowerShell в этой папке и выполнить:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item .env.example .env
   notepad .env
   ```

3. В `.env` заполнить:

   ```dotenv
   STEAMDECK_HOST=192.168.x.x
   STEAMDECK_USER=deck
   STEAMDECK_PASSWORD=<deck password>
   STEAMDECK_SUDO_PASSWORD=<deck password>
   ```

4. Проверить подключение:

   ```powershell
   .\test_connection.ps1
   ```

## Подключение к Codex

Добавить MCP server в конфиг Codex, подставив свой путь:

```toml
[mcp_servers.steamdeck]
command = "C:\\Users\\<you>\\Downloads\\steamdeck-mcp-kit\\.venv\\Scripts\\python.exe"
args = ["C:\\Users\\<you>\\Downloads\\steamdeck-mcp-kit\\steamdeck_mcp.py"]
```

После этого перезапустить Codex.

## Важно

- Не передавайте заполненный `.env`; в нем пароль от Deck.
- Игры, BIOS, firmware, keys и лицензии используйте только из своих легальных дампов.
- Для EmuDeck на SD-карте можно указать путь вручную:

  ```dotenv
  STEAMDECK_EMULATION_ROOT=/run/media/deck/<sd-id>/Emulation
  ```
