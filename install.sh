#!/bin/bash
# Скрипт установки YouTube → Яндекс Станция Lite 2
# Настраивает колонку, создает необходимые файлы и делает скрипты исполняемыми

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_error() {
    echo -e "${RED}[ОШИБКА]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_info() {
    echo -e "${YELLOW}[INFO]${NC} $1"
}

# Проверка Python 3
echo "Проверка Python 3..."
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 не найден. Установите Python 3.10 или новее."
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    print_error "Нужен Python 3.10 или новее, установлен $(python3 --version 2>&1)."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
print_success "Python $PYTHON_VERSION найден"

# Проверка yt-dlp
echo
echo "Проверка yt-dlp..."
YTDLP_PATH="${YTDLP:-$(command -v yt-dlp || true)}"
[ -n "$YTDLP_PATH" ] || YTDLP_PATH="$HOME/.local/bin/yt-dlp"

if [ ! -x "$YTDLP_PATH" ]; then
    print_info "yt-dlp не найден в $YTDLP_PATH"
    read -p "Установить yt-dlp через pipx/pip? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[YyДд]$ ]]; then
        if command -v pipx &> /dev/null; then
            pipx install yt-dlp || pipx upgrade yt-dlp
        elif python3 -m pip --version &> /dev/null; then
            python3 -m pip install --user --upgrade yt-dlp
        else
            print_error "Нет pipx/pip. Установите yt-dlp через пакетный менеджер или pipx."
            exit 1
        fi
        YTDLP_PATH="$(command -v yt-dlp || echo "$HOME/.local/bin/yt-dlp")"
        print_success "yt-dlp установлен: $YTDLP_PATH"
        print_info "Для входа в YouTube нужен Python из окружения yt-dlp (pipx/pip), а не одиночный бинарник."
    else
        print_error "yt-dlp обязателен для работы. Установите его вручную."
        exit 1
    fi
else
    print_success "yt-dlp найден: $YTDLP_PATH"
fi

# Node.js нужен yt-dlp для обхода JS-проверок YouTube.
if command -v node &> /dev/null; then
    print_success "Node.js найден: $(node --version)"
else
    print_info "Node.js не найден. yt-dlp может не справиться с проверками YouTube; установите nodejs."
fi

# Firefox нужен для входа в YouTube из веб-пульта.
if command -v firefox &> /dev/null || command -v firefox-esr &> /dev/null; then
    print_success "Firefox найден"
else
    print_info "Firefox не найден. Вход в YouTube можно выполнить в другом браузере, но автоэкспорт cookies настроен на Firefox."
fi

# Настройка IP-адреса колонки
echo
echo "Настройка подключения к Яндекс Станции..."
read -p "Введите IP-адрес станции: " STATION_HOST

if [ -z "$STATION_HOST" ]; then
    print_error "IP-адрес станции обязателен!"
    exit 1
fi

# Сохраняем адрес для start.sh и bridge.py
export STATION_HOST="$STATION_HOST"
printf 'STATION_HOST=%s\n' "$STATION_HOST" > station.env
print_success "IP-адрес станции сохранен в station.env: $STATION_HOST"

STATION_PORT="2222"
STATION_USER="root"

print_info "IP-адрес станции: $STATION_HOST"
echo

# Проверка SSH-подключения
echo "Проверка SSH-подключения к станции..."
if ssh -p "$STATION_PORT" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$STATION_USER@$STATION_HOST" "echo test" &>/dev/null; then
    print_success "SSH-подключение к станции работает"
else
    print_error "Не удалось подключиться к станции"
    echo
    echo "Настройте SSH-ключ для входа без пароля:"
    echo "  ssh-copy-id -p $STATION_PORT $STATION_USER@$STATION_HOST"
    echo
    echo "Или измените параметры подключения в bridge.py:"
    echo "  STATION_HOST, STATION_PORT, STATION_USER"
    exit 1
fi

# Plan B: native Yandex Music streams must not sound.  Keep TTS intact and
# answer only *.strm.yandex.net with 127.0.0.1.
if [ -x ./setup-station-dns.sh ]; then
    print_info "Блокирую нативные аудиопотоки Яндекс Музыки на станции..."
    if ./setup-station-dns.sh; then
        print_success "Нативная Яндекс Музыка отключена на уровне DNS"
    else
        print_info "Не удалось настроить DNS-блокировку; мост всё равно будет глушить плеер Алисы."
    fi
fi

# Создание директорий
echo
echo "Создание директорий..."
mkdir -p cache
print_success "Директория cache создана"

# Проверка портов
echo
echo "Проверка портов..."
HTTP_PORT=8888

check_port() {
    if ss -tuln 2>/dev/null | grep -q ":$1 " || netstat -tuln 2>/dev/null | grep -q ":$1 "; then
        print_error "Порт $1 уже занят"
        return 1
    fi
    return 0
}

if check_port $HTTP_PORT; then
    print_success "Порт $HTTP_PORT свободен (веб-интерфейс)"
fi

# Настройка автозапуска (опционально)
echo
echo "Настройка автозапуска (опционально)..."
read -p "Добавить в автозапуск (cron @reboot)? (y/n): " -n 1 -r
echo

if [[ $REPLY =~ ^[YyДд]$ ]]; then
    CRON_LINE="@reboot $SCRIPT_DIR/start.sh >/dev/null 2>&1"
    if crontab -l 2>/dev/null | grep -qF "$SCRIPT_DIR/start.sh"; then
        print_info "Автозапуск уже настроен"
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        print_success "Автозапуск добавлен в crontab"
    fi
fi

# Создание default settings.json
echo
echo "Создание default settings.json..."
if [ ! -f "settings.json" ]; then
    cat > settings.json << 'EOF'
{
    "cookies_from_browser": "firefox",
    "cookies_file": "",
    "js_runtimes": "node",
    "playlist_limit": 50,
    "proxy": "",
    "native_watch": true
}
EOF
    print_success "Создан default settings.json"
else
    print_info "settings.json уже существует, пропускаю создание"
fi

# Создание default playlists.json
echo
echo "Создание default playlists.json..."
if [ ! -f "playlists.json" ]; then
    cat > playlists.json << 'EOF'
[]
EOF
    print_success "Создан default playlists.json"
else
    print_info "playlists.json уже существует, пропускаю создание"
fi

# Сделать скрипты исполняемыми
echo
echo "Сделать скрипты исполняемыми..."
chmod +x start.sh stop.sh install.sh setup-station-dns.sh
print_success "Скрипты установлены"

echo
echo "=== Установка завершена ==="
echo
print_info "Следующие шаги:"
echo "  1. Запустите мост: ./start.sh"
echo "  2. Откройте веб-интерфейс: http://localhost:8888 (по дефолту)"
echo "  3. Войдите в YouTube через кнопку 'Войти' в веб-интерфейсе"
echo
print_info "Для остановки: ./stop.sh"
echo
print_info "Готово! Теперь запустите ./start.sh для запуска системы."
