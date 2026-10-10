#!/usr/bin/env bash
# Установка и обновление бота на сервере (Ubuntu). Запускать от root:
#   curl -fsSL https://raw.githubusercontent.com/somcland/Telegram-bot-design/main/server/install.sh | bash
# Повторный запуск = обновление до свежей версии из GitHub.
set -euo pipefail

REPO="https://github.com/somcland/Telegram-bot-design.git"
APP="/home/bot/app"

echo "▶ Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq python3-venv git >/dev/null

echo "▶ Пользователь bot"
id bot >/dev/null 2>&1 || adduser --disabled-password --gecos "" bot

echo "▶ Код"
if [ -d "$APP/.git" ]; then
  sudo -u bot git -C "$APP" pull --ff-only -q
else
  sudo -u bot git clone -q "$REPO" "$APP"
fi

echo "▶ Библиотеки Python (минута-две)"
[ -d "$APP/.venv" ] || sudo -u bot python3 -m venv "$APP/.venv"
sudo -u bot "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u bot "$APP/.venv/bin/pip" install -q -r "$APP/server/requirements.txt"

echo "▶ Настройки"
if [ ! -f "$APP/.env" ]; then
  sudo -u bot cp "$APP/server/.env.example" "$APP/.env"
fi
chmod 600 "$APP/.env"

echo "▶ Автозапуск"
cp "$APP/server/design-bot.service" /etc/systemd/system/design-bot.service
systemctl daemon-reload
systemctl enable -q design-bot

if grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$APP/.env"; then
  systemctl restart design-bot
  sleep 3
  if systemctl is-active -q design-bot; then
    echo "✅ Бот запущен. Напиши ему /start в Telegram."
  else
    echo "❌ Бот не стартовал. Покажи вывод команды: journalctl -u design-bot -n 30"
  fi
else
  echo ""
  echo "Осталось вписать токен бота:"
  echo "  1) nano /home/bot/app/.env"
  echo "  2) вставь токен после TELEGRAM_BOT_TOKEN=  (без пробелов и кавычек)"
  echo "  3) Ctrl+O, Enter, Ctrl+X"
  echo "  4) systemctl restart design-bot"
fi
