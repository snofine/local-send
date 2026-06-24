#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local-send — лёгкая утилита для обмена файлами по локальной Wi-Fi сети.

Запуск одной командой:
    python main.py

Зависимости ставятся один раз:
    pip install fastapi uvicorn qrcode

При запуске определяется локальный IPv4-адрес, выбирается порт (8080 или
свободный), в консоль выводится QR-код. Смартфон сканирует QR — открывается
веб-интерфейс для отправки/скачивания файлов.
"""

from __future__ import annotations

import html
import logging
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List

# --------------------------------------------------------------------------- #
# Проверка зависимостей ДО того, как импортировать тяжёлые библиотеки ниже.
# --------------------------------------------------------------------------- #


def check_deps() -> None:
    # python-multipart импортируется как модуль 'multipart'
    checks = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "qrcode": "qrcode",
        "python-multipart": "multipart",
    }
    missing: list[str] = []
    for pip_name, mod in checks.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(
            "Отсутствуют необходимые библиотеки: " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "\nУстановите их одной командой:\n"
            "    pip install " + " ".join(missing) + "\n",
            file=sys.stderr,
        )
        sys.exit(1)


check_deps()

# --- Теперь безопасно импортировать тяжёлое --------------------------------- #

import qrcode  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import (  # noqa: E402
    FastAPI,
    File,
    Form,
    UploadFile,
)
from fastapi.responses import (  # noqa: E402
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"  # сюда сохраняются принятые файлы/заметки
UPLOADS_DIR = BASE_DIR / "uploads"  # отсюда файлы скачиваются на телефон
CHUNK_SIZE = 1024 * 1024  # 1 MiB — для поблочного чтения/записи
PREFERRED_PORT = 8080

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("local-send")


# --------------------------------------------------------------------------- #
# Сетевые утилиты
# --------------------------------------------------------------------------- #


def get_local_ip() -> str:
    """
    Определяет IPv4-адрес машины в локальной сети.

    Трюк: открываем UDP-сокет к публичному адресу (пакет не уходит — это UDP),
    но ядро при этом резолвит маршрут и сообщает локальный адрес интерфейса.
    """
    candidates: list[str] = []

    # Основной способ — быстрый и не требует реального сетевого трафика.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Адрес не обязан быть достижим; важен сам факт выбора маршрута.
            s.connect(("8.8.8.8", 80))
            candidates.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    # Запасной способ — перебор имён хоста.
    try:
        hostname = socket.gethostname()
        info = socket.gethostbyname_ex(hostname)
        candidates.extend(info[2])
    except OSError:
        pass

    # Перебор всех интерфейсов через getaddrinfo (IPv4 только).
    try:
        for res in socket.getaddrinfo(hostname := socket.gethostname(), None):
            ip = res[4][0]
            candidates.append(ip)
    except OSError:
        pass

    for ip in candidates:
        if _is_usable_ip(ip):
            return ip

    # Ничего не нашли — fallback на 127.0.0.1 (QR всё равно сработает локально).
    return "127.0.0.1"


def _is_usable_ip(ip: str) -> bool:
    """Отбрасывает localhost, link-local и все не-IPv4 адреса."""
    if ":" in ip:  # IPv6
        return False
    if ip.startswith("127."):  # loopback
        return False
    if ip.startswith("169.254."):  # link-local (APIPA)
        return False
    if ip == "0.0.0.0":
        return False
    try:
        socket.inet_aton(ip)
    except OSError:
        return False
    return True


def find_free_port(preferred: int = PREFERRED_PORT) -> int:
    """Возвращает preferred, если свободен; иначе случайный свободный порт."""
    if _port_is_free(preferred):
        return preferred
    # Случайный свободный порт.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------- #
# QR-код в консоли (half-block 2:1)
# --------------------------------------------------------------------------- #
#
# Каждая пара строк матрицы кодируется одним рядом символов высотой в "полу-блок":
#   верх/низ 1/1 -> █      (обе строки чёрные)
#   верх/низ 0/1 -> ▄      (только низ чёрный)
#   верх/низ 1/0 -> ▀      (только верх чёрный)
#   верх/низ 0/0 -> пробел (обе строки белые)
# Чёрный модуль QR рендерится как фон (т.е. печать символа), белый — как
# пустота. Чтобы получить достаточный контраст и симметричное поле, выводим
# QR инвертированно не по цвету, а по "заполненности": модуль True = '█'-символ.
#
# Тёмная клетка QR = True в матрице qrcode. Для надёжного сканирования
# добавляем 2-модульный белый quiet-zone вокруг (qrcode добавляет его сам).


def render_qr(data: str) -> str:
    """Возвращает многострочную ASCII-отрисовку QR-кода (half-block)."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,  # quiet zone
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix: List[List[bool]] = qr.get_matrix()  # True = чёрный модуль

    # Символьная карта для пар (top, bottom), где True = чёрный модуль.
    glyphs = {
        (True, True): "█",
        (True, False): "▀",
        (False, True): "▄",
        (False, False): " ",
    }

    height = len(matrix)
    width = len(matrix[0]) if height else 0
    lines: list[str] = []
    # Если высота нечётная — добавляем виртуальную белую строку снизу.
    for y in range(0, height, 2):
        top = matrix[y]
        bottom = matrix[y + 1] if y + 1 < height else [False] * width
        row = "".join(glyphs[(top[x], bottom[x])] for x in range(width))
        lines.append(row)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Хранилище
# --------------------------------------------------------------------------- #


def ensure_dirs() -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # Подсказка пользователю: кладёт файлы сюда, чтобы они появились в списке.
    readme = UPLOADS_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Положите в эту папку файлы — они появятся в списке для скачивания "
            "на телефоне.\n",
            encoding="utf-8",
        )


def unique_path(directory: Path, name: str) -> Path:
    """
    Возвращает путь в directory, который не занят.
    При конфликте добавляет суффикс _1, _2, … перед расширением.
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}" if unit != "B" else f"{int(num)} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


# --------------------------------------------------------------------------- #
# FastAPI приложение
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(_app: FastAPI) -> None:
    """Создаём рабочие директории при старте приложения."""
    ensure_dirs()
    yield


app = FastAPI(
    title="local-send",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML_PAGE


@app.post("/upload")
async def upload(files: List[UploadFile] = File(...)) -> dict:
    """
    Приём нескольких файлов. Читается поблочно, без ограничения размера.
    Сохраняются в downloads/.
    """
    if not files:
        return JSONResponse({"ok": False, "error": "no files"}, status_code=400)

    saved: list[dict] = []
    for f in files:
        original = os.path.basename(f.filename or "unnamed")
        # Защита от path-traversal: оставляем только имя файла.
        original = original.replace("\\", "/").split("/")[-1] or "unnamed"
        target = unique_path(DOWNLOADS_DIR, original)
        total = 0
        try:
            with open(target, "wb") as out:
                while True:
                    chunk = await f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
        except OSError as e:
            log.error("Ошибка при сохранении %s: %s", original, e)
            saved.append({"name": original, "ok": False, "error": str(e)})
            continue
        finally:
            await f.close()

        log.info(
            "Файл принят: %s  (%s)  -> %s",
            original,
            human_size(total),
            target.name,
        )
        saved.append(
            {"name": target.name, "original": original, "size": total, "ok": True}
        )

    return {"ok": True, "files": saved}


@app.post("/notes")
async def notes(text: str = Form(...)) -> dict:
    """Сохраняет заметку/ссылку в downloads/."""
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
    target = unique_path(DOWNLOADS_DIR, f"note_{stamp}.txt")
    body = text.strip()
    if not body:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    header = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
    target.write_text(header + body + "\n", encoding="utf-8")
    log.info("Заметка сохранена (%s симв.) -> %s", len(body), target.name)
    return {"ok": True, "file": target.name}


@app.get("/files")
async def list_files() -> dict:
    """Список файлов из uploads/, доступных для скачивания."""
    items: list[dict] = []
    try:
        for entry in UPLOADS_DIR.iterdir():
            if not entry.is_file():
                continue
            st = entry.stat()
            items.append(
                {
                    "name": entry.name,
                    "size": st.st_size,
                    "size_human": human_size(st.st_size),
                    "modified": int(st.st_mtime),
                    "modified_human": datetime.fromtimestamp(
                        st.st_mtime
                    ).strftime("%Y-%m-%d %H:%M"),
                }
            )
    except OSError as e:
        log.error("Не удалось прочитать uploads/: %s", e)
        return JSONResponse(
            {"ok": False, "error": str(e), "files": []}, status_code=500
        )
    # Свежие сверху.
    items.sort(key=lambda it: it["modified"], reverse=True)
    return {"ok": True, "files": items}


@app.get("/download/{name:path}")
async def download(name: str) -> StreamingResponse:
    """
    Отдаёт файл из uploads/ поблочно (StreamingResponse) — память не уходит,
    работает с гигабайтными видео.
    """
    safe = os.path.basename(name)
    path = UPLOADS_DIR / safe
    if not path.is_file():
        return JSONResponse(
            {"ok": False, "error": "not found", "name": safe}, status_code=404
        )

    st = path.stat()

    def iterfile() -> "Iterator[bytes]":
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        except OSError as e:
            log.error("Ошибка отдачи %s: %s", safe, e)

    log.info("Отдаю файл: %s (%s)", safe, human_size(st.st_size))
    return StreamingResponse(
        iterfile(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "Content-Length": str(st.st_size),
        },
    )


# --------------------------------------------------------------------------- #
# Веб-интерфейс (HTML + Tailwind CDN + Vanilla JS)
# --------------------------------------------------------------------------- #

HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>local-send — обмен файлами</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'class',
    theme: { extend: {} }
  }
</script>
<style>
  html, body { -webkit-tap-highlight-color: transparent; }
  .drag-active { border-color: #3b82f6 !important; background: #eff6ff !important; }
  .dark .drag-active { border-color: #60a5fa !important; background: #1e3a8a !important; }
</style>
</head>
<body class="bg-slate-100 dark:bg-slate-900 text-slate-800 dark:text-slate-100 min-h-screen transition-colors">

<div class="max-w-2xl mx-auto px-4 py-6 sm:py-10">

  <!-- Шапка -->
  <header class="flex items-center justify-between mb-6">
    <div>
      <h1 class="text-2xl sm:text-3xl font-bold flex items-center gap-2">
        <span>📡</span> local-send
      </h1>
      <p class="text-sm text-slate-500 dark:text-slate-400 mt-1">
        Обмен файлами в локальной сети
      </p>
    </div>
    <button id="themeBtn" class="p-2 rounded-lg bg-white dark:bg-slate-800 shadow text-xl" title="Сменить тему">
      🌗
    </button>
  </header>

  <!-- ============ Блок 1: отправка на ПК ============ -->
  <section class="bg-white dark:bg-slate-800 rounded-2xl shadow p-4 sm:p-6 mb-6">
    <h2 class="text-lg font-semibold mb-3 flex items-center gap-2">
      <span>📤</span> Отправить на ПК
    </h2>

    <!-- Drop-зона -->
    <div id="dropZone"
         class="border-2 border-dashed border-slate-300 dark:border-slate-600 rounded-xl p-6 text-center cursor-pointer transition select-none">
      <div class="text-4xl mb-2">📁</div>
      <p class="font-medium">Перетащите файлы сюда</p>
      <p class="text-sm text-slate-500 dark:text-slate-400 mt-1">или нажмите для выбора (несколько файлов сразу)</p>
      <input id="fileInput" type="file" multiple class="hidden">
    </div>

    <!-- Прогресс -->
    <div id="progressWrap" class="mt-4 hidden">
      <div class="flex justify-between text-sm mb-1">
        <span id="progressLabel">Загрузка…</span>
        <span id="progressPct">0%</span>
      </div>
      <div class="w-full bg-slate-200 dark:bg-slate-700 rounded-full h-3 overflow-hidden">
        <div id="progressBar" class="bg-blue-500 h-3 rounded-full transition-all" style="width:0%"></div>
      </div>
    </div>

    <!-- Список загруженных -->
    <ul id="uploadResult" class="mt-3 space-y-1 text-sm"></ul>

    <hr class="my-5 border-slate-200 dark:border-slate-700">

    <!-- Заметка -->
    <h3 class="font-medium mb-2 flex items-center gap-2">
      <span>📝</span> Быстрая заметка / ссылка
    </h3>
    <div class="flex flex-col sm:flex-row gap-2">
      <textarea id="noteText" rows="2" placeholder="Вставьте ссылку или текст…"
        class="flex-1 rounded-lg border border-slate-300 dark:border-slate-600 bg-slate-50 dark:bg-slate-900 px-3 py-2 resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"></textarea>
      <button id="noteBtn" class="bg-blue-500 hover:bg-blue-600 text-white font-medium rounded-lg px-4 py-2 sm:w-auto">
        Отправить
      </button>
    </div>
    <p id="noteResult" class="text-sm mt-2"></p>
  </section>

  <!-- ============ Блок 2: скачивание с ПК ============ -->
  <section class="bg-white dark:bg-slate-800 rounded-2xl shadow p-4 sm:p-6">
    <div class="flex items-center justify-between mb-3">
      <h2 class="text-lg font-semibold flex items-center gap-2">
        <span>📥</span> Файлы на ПК
      </h2>
      <button id="refreshBtn" class="text-sm bg-slate-200 dark:bg-slate-700 hover:bg-slate-300 dark:hover:bg-slate-600 rounded-lg px-3 py-1.5">
        ↻ Обновить
      </button>
    </div>
    <ul id="filesList" class="divide-y divide-slate-100 dark:divide-slate-700">
      <li class="text-sm text-slate-400 py-4 text-center">Загрузка списка…</li>
    </ul>
  </section>

  <footer class="text-center text-xs text-slate-400 mt-8">
    local-send · файлы сохраняются на ПК в папку <code>downloads/</code>
  </footer>
</div>

<script>
(function () {
  const $ = (id) => document.getElementById(id);

  // ---------- Тема ----------
  const savedTheme = localStorage.getItem('ls-theme');
  if (savedTheme === 'dark' || (!savedTheme && matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
  }
  $('themeBtn').addEventListener('click', () => {
    document.documentElement.classList.toggle('dark');
    localStorage.setItem('ls-theme',
      document.documentElement.classList.contains('dark') ? 'dark' : 'light');
  });

  // ---------- Загрузка файлов ----------
  const dropZone = $('dropZone');
  const fileInput = $('fileInput');
  const progressWrap = $('progressWrap');
  const progressBar = $('progressBar');
  const progressPct = $('progressPct');
  const progressLabel = $('progressLabel');
  const uploadResult = $('uploadResult');

  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    if (fileInput.files.length) uploadFiles(fileInput.files);
    fileInput.value = '';
  });

  ['dragenter', 'dragover'].forEach(ev =>
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropZone.classList.add('drag-active');
    }));
  ['dragleave', 'drop'].forEach(ev =>
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropZone.classList.remove('drag-active');
    }));
  dropZone.addEventListener('drop', (e) => {
    const files = e.dataTransfer.files;
    if (files && files.length) uploadFiles(files);
  });

  function uploadFiles(files) {
    const form = new FormData();
    for (const f of files) form.append('files', f, f.name);

    progressWrap.classList.remove('hidden');
    progressBar.style.width = '0%';
    progressPct.textContent = '0%';
    progressLabel.textContent = 'Загрузка ' + files.length + ' файл(ов)…';

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        progressBar.style.width = pct + '%';
        progressPct.textContent = pct + '%';
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          renderUploadResult(data.files || []);
          progressLabel.textContent = 'Готово ✓';
          setTimeout(() => progressWrap.classList.add('hidden'), 1500);
        } catch {
          progressLabel.textContent = 'Ошибка ответа сервера';
        }
      } else {
        progressLabel.textContent = 'Ошибка: ' + xhr.status;
      }
    };
    xhr.onerror = () => { progressLabel.textContent = 'Сбой сети'; };
    xhr.send(form);
  }

  function renderUploadResult(files) {
    uploadResult.innerHTML = '';
    files.forEach(f => {
      const li = document.createElement('li');
      const ok = f.ok !== false;
      li.className = ok
        ? 'flex items-center justify-between text-green-600 dark:text-green-400'
        : 'flex items-center justify-between text-red-600 dark:text-red-400';
      const sz = typeof f.size === 'number' ? ' · ' + humanSize(f.size) : '';
      li.innerHTML = '<span>' + escapeHtml(f.original || f.name) + sz + '</span>'
        + '<span>' + (ok ? '✓' : '✗') + '</span>';
      uploadResult.appendChild(li);
    });
  }

  // ---------- Заметка ----------
  $('noteBtn').addEventListener('click', sendNote);
  $('noteText').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendNote();
  });

  async function sendNote() {
    const text = $('noteText').value.trim();
    const out = $('noteResult');
    if (!text) { out.textContent = 'Введите текст'; out.className = 'text-sm mt-2 text-amber-500'; return; }
    out.textContent = 'Отправка…'; out.className = 'text-sm mt-2 text-slate-400';
    try {
      const res = await fetch('/notes', {
        method: 'POST',
        body: new URLSearchParams({ text })
      });
      const data = await res.json();
      if (data.ok) {
        out.textContent = 'Сохранено в ' + data.file; out.className = 'text-sm mt-2 text-green-600 dark:text-green-400';
        $('noteText').value = '';
      } else {
        throw new Error(data.error || 'ошибка');
      }
    } catch (e) {
      out.textContent = 'Ошибка: ' + e.message; out.className = 'text-sm mt-2 text-red-600 dark:text-red-400';
    }
  }

  // ---------- Список файлов ----------
  $('refreshBtn').addEventListener('click', loadFiles);

  async function loadFiles() {
    const list = $('filesList');
    list.innerHTML = '<li class="text-sm text-slate-400 py-4 text-center">Загрузка списка…</li>';
    try {
      const res = await fetch('/files');
      const data = await res.json();
      const files = data.files || [];
      if (!files.length) {
        list.innerHTML = '<li class="text-sm text-slate-400 py-6 text-center">'
          + 'Папка <code>uploads/</code> пуста — положите файлы в неё на ПК.</li>';
        return;
      }
      list.innerHTML = '';
      files.forEach(f => {
        const li = document.createElement('li');
        li.className = 'flex items-center justify-between py-3 gap-3';
        li.innerHTML =
          '<div class="min-w-0 flex-1">'
          + '<div class="font-medium truncate">' + escapeHtml(f.name) + '</div>'
          + '<div class="text-xs text-slate-400">' + f.size_human + ' · ' + f.modified_human + '</div>'
          + '</div>'
          + '<a href="/download/' + encodeURIComponent(f.name) + '" '
          + 'class="shrink-0 bg-blue-500 hover:bg-blue-600 text-white text-sm font-medium rounded-lg px-3 py-2">'
          + 'Скачать</a>';
        list.appendChild(li);
      });
    } catch (e) {
      list.innerHTML = '<li class="text-sm text-red-500 py-4 text-center">Не удалось загрузить список</li>';
    }
  }

  // ---------- Утилиты ----------
  function humanSize(num) {
    const u = ['B','KB','MB','GB','TB'];
    let i = 0; let n = num;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return i === 0 ? (n|0) + ' B' : n.toFixed(1) + ' ' + u[i];
  }
  function escapeHtml(s) {
    const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
  }

  loadFiles();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #


def print_banner(ip: str, port: int, url: str) -> None:
    line = "═" * max(48, len(url) + 4)
    # flush=True — гарантирует порядок вывода относительно uvicorn-логов.
    out = [
        "",
        f"\033[1;36m{line}",
        "  📡  local-send запущен",
        f"\033[0;36m{line}",
        f"  IPv4 : {ip}",
        f"  Порт : {port}",
        f"  URL  : \033[1;33m{url}\033[0m",
        f"\033[0;36m{line}",
        "  Отсканируйте QR-код телефоном, чтобы открыть веб-интерфейс.",
        f"\033[0;36m{line}\033[0m",
        "  Остановить сервер: Ctrl+C",
        "",
        f"\033[1;37m{render_qr(url)}\033[0m",
        "",
    ]
    print("\n".join(out), flush=True)


def main() -> None:
    ip = get_local_ip()
    port = find_free_port(PREFERRED_PORT)
    url = f"http://{ip}:{port}"

    ensure_dirs()
    print_banner(ip, port, url)

    log.info("Слушаем на 0.0.0.0:%s (локальный адрес %s)", port, url)
    log.info("downloads/: %s", DOWNLOADS_DIR)
    log.info("uploads/  : %s", UPLOADS_DIR)

    # Глушим спамный access-log uvicorn (свой логер пишет только нужное).
    # Лимита на размер тела запроса НЕТ: uvicorn стримит тело чанками, а
    # /upload читает поблочно через await f.read(CHUNK_SIZE), поэтому большие
    # видео/логи (много ГБ) передаются без переполнения памяти.
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        timeout_keep_alive=30,
    )
    server = uvicorn.Server(config)

    try:
        server.run()
    except KeyboardInterrupt:
        print("\n\033[33mОстанавливаюсь… до встречи!\033[0m")


if __name__ == "__main__":
    main()
