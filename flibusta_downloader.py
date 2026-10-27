#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
==============================================================================
📚 Flibusta Series Downloader (flibusta-dl)
==============================================================================
Высокоскоростной многопоточный загрузчик целых серий и циклов книг с Флибусты.
Поддерживает форматы FB2 (с авто-распаковкой), EPUB, MOBI, PDF, умную
дедупликацию переводов, поиск серий, кастомные зеркала и прокси.

Разработчик: Ivankolp (https://github.com/Ivankolp)
==============================================================================
"""

import os
import sys
import re
import io
import time
import json
import zipfile
import argparse
import unicodedata
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TaskID,
)
from rich.prompt import Prompt, Confirm
from rich import box

# Инициализация консоли Rich
console = Console()

# Константы
DEFAULT_HOST = "http://flibusta.is"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
BANNER = r"""[bold cyan]
  ______ _ _ _                   _            _____  _      
 |  ____| (_) |                 | |          |  __ \| |     
 | |__  | |_| |__  _   _ ___ ___| |_  __ _   | |  | | |     
 |  __| | | | '_ \| | | / __/ __| __|/ _` |  | |  | | |     
 | |    | | | |_) | |_| \__ \__ \ |_| (_| |  | |__| | |____ 
 |_|    |_|_|_.__/ \__,_|___/___/\__|\__,_|  |_____/|______|
[/bold cyan][bold yellow] 📚 Fast Multithreaded Book Series Downloader v1.0.0[/bold yellow]
"""


def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Очищает строку от запрещённых для файловой системы символов."""
    if not name:
        return "unnamed"
    name = unicodedata.normalize("NFKC", str(name))
    # Заменяем запрещённые символы
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" ._\t\r\n")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" ._")
    return name or "unnamed"


class FlibustaClient:
    """HTTP-клиент для прямого взаимодействия с официальной Флибустой (flibusta.is)."""

    def __init__(self, base_url: str = DEFAULT_HOST, proxy: Optional[str] = None, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        if self.proxy:
            self.session.proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Выполняет прямой HTTP-запрос к Флибусте."""
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        # Если передан относительный путь
        if url.startswith("/"):
            full_url = f"{self.base_url}{url}"
        elif url.startswith("http"):
            full_url = url
        else:
            full_url = f"{self.base_url}/{url}"

        return self.session.request(method, full_url, **kwargs)

    def search_series(self, query: str) -> List[Dict[str, str]]:
        """Ищет серии по названию на Флибусте."""
        url = f"/booksearch?ask={requests.utils.quote(query)}&chs=on"
        resp = self.request("GET", url)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        series_list = []
        seen_ids = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/sequence/") or href.startswith("/s/"):
                s_id = href.split("/")[-1].split("?")[0]
                title = a.get_text().strip()
                if s_id.isdigit() and s_id not in seen_ids:
                    seen_ids.add(s_id)
                    series_list.append({
                        "id": s_id,
                        "title": title,
                        "url": f"{self.base_url}{href}",
                    })
        return series_list

    def parse_series(self, series_id_or_url: str) -> Dict[str, Any]:
        """Парсит страницу серии и возвращает полную информацию и список книг."""
        # Извлекаем числовой ID серии
        if series_id_or_url.isdigit():
            series_id = series_id_or_url
        else:
            match = re.search(r"/(?:sequence|s)/(\d+)", series_id_or_url)
            if match:
                series_id = match.group(1)
            else:
                raise ValueError(f"Не удалось распознать ID серии из: '{series_id_or_url}'")

        url = f"/sequence/{series_id}"
        resp = self.request("GET", url)
        if resp.status_code != 200:
            raise RuntimeError(f"Ошибка загрузки страницы серии {series_id}: HTTP {resp.status_code}")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Заголовок серии
        h1 = soup.find("h1", class_="title") or soup.find("h1")
        series_title = h1.get_text().strip() if h1 else f"Серия {series_id}"

        # Мета-информация (Авторы, Жанры, Переводчики, Тип серии)
        meta_info = {
            "type": "не указан",
            "authors": [],
            "translators": [],
            "genres": [],
        }

        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) == 2:
                key = tds[0].get_text().strip().lower()
                val_td = tds[1]
                if "тип" in key:
                    meta_info["type"] = val_td.get_text().strip()
                elif "автор" in key:
                    meta_info["authors"] = [a.get_text().strip() for a in val_td.find_all("a")]
                elif "переводчик" in key:
                    meta_info["translators"] = [a.get_text().strip() for a in val_td.find_all("a")]
                elif "жанр" in key:
                    meta_info["genres"] = [a.get_text().strip() for a in val_td.find_all("a")]

        # Извлечение книг серии со всех страниц (на сайте по 50 книг на странице)
        book_input_pattern = re.compile(r"^bchk\d+")
        book_inputs = soup.find_all("input", attrs={"name": book_input_pattern})

        last_page = 0
        for a in soup.find_all("a", href=True):
            page_match = re.search(rf"/sequence/{series_id}\?page=(\d+)", a["href"])
            if page_match:
                last_page = max(last_page, int(page_match.group(1)))

        for page in range(1, last_page + 1):
            page_resp = self.request("GET", f"{url}?page={page}")
            if page_resp.status_code != 200:
                raise RuntimeError(f"Ошибка загрузки страницы {page + 1} серии {series_id}: HTTP {page_resp.status_code}")
            page_soup = BeautifulSoup(page_resp.text, "html.parser")
            book_inputs.extend(page_soup.find_all("input", attrs={"name": book_input_pattern}))

        books: List[Dict[str, Any]] = []

        for idx, inp in enumerate(book_inputs, start=1):
            book_id = inp["name"].replace("bchk", "")
            
            # Собираем все элементы строки книги до следующего <br> или <input>
            sib = inp.next_sibling
            entry_elements = []
            while sib:
                if sib.name == "br":
                    break
                if hasattr(sib, "get") and str(sib.get("name", "")).startswith("bchk"):
                    break
                entry_elements.append(sib)
                sib = sib.next_sibling

            entry_text = "".join([str(e.get_text() if hasattr(e, "get_text") else str(e)) for e in entry_elements])

            # Ищем ссылку на саму книгу
            book_a = next(
                (
                    e for e in entry_elements
                    if getattr(e, "name", None) == "a"
                    and e.get("href", "").startswith(f"/b/{book_id}")
                    and not any(f in e.get("href", "") for f in ["/read", "/fb2", "/epub", "/mobi", "/download", "/pdf", "/txt"])
                ),
                None
            )
            title = book_a.get_text().strip() if book_a else f"Книга {book_id}"

            # Извлечение порядкового номера книги в серии
            num_match = re.search(r"-\s*(\d+(?:\.\d+)?)\.?", entry_text)
            seq_num_str = num_match.group(1) if num_match else None
            try:
                seq_num = float(seq_num_str) if seq_num_str else float(idx)
            except ValueError:
                seq_num = float(idx)

            # Извлечение авторов книги (ссылки /a/ после форматов скачивания)
            book_authors = []
            translators = []
            size_pages = ""

            # Ищем переводчиков (в тексте пер. <a href="/a/...">)
            trans_match = re.search(r"\(пер\.\s*(.*?)\)", entry_text)
            if trans_match:
                translators.append(trans_match.group(1).strip())

            # Ищем размер и страницы
            size_match = re.search(r"(\d+K|\d+M)(?:,\s*(\d+\s*с\.))?", entry_text)
            if size_match:
                size_pages = size_match.group(0).strip()

            # Ищем ссылки авторов
            for e in entry_elements:
                if getattr(e, "name", None) == "a":
                    href = e.get("href", "")
                    if href.startswith("/a/"):
                        a_name = e.get_text().strip()
                        if a_name not in book_authors and a_name not in translators:
                            book_authors.append(a_name)

            # Доступные форматы скачивания
            formats = {}
            for e in entry_elements:
                if getattr(e, "name", None) == "a":
                    href = e.get("href", "")
                    if f"/b/{book_id}/fb2" in href:
                        formats["fb2"] = f"/b/{book_id}/fb2"
                    elif f"/b/{book_id}/epub" in href:
                        formats["epub"] = f"/b/{book_id}/epub"
                    elif f"/b/{book_id}/mobi" in href:
                        formats["mobi"] = f"/b/{book_id}/mobi"
                    elif f"/b/{book_id}/pdf" in href or f"/b/{book_id}/download" in href:
                        formats["pdf"] = href

            # Дополнительное примечание к изданию (в квадратных скобках после названия)
            edition_match = re.search(r"(\[.*?\])", entry_text)
            edition = edition_match.group(1).strip() if edition_match else ""

            books.append({
                "index": idx,
                "id": book_id,
                "seq_num": seq_num,
                "seq_num_str": seq_num_str or str(idx),
                "title": title,
                "authors": book_authors or meta_info["authors"] or ["Неизвестный автор"],
                "translators": translators,
                "edition": edition,
                "size_pages": size_pages,
                "formats": formats,
                "raw_text": entry_text.strip(),
            })

        return {
            "id": series_id,
            "title": series_title,
            "url": f"{self.base_url}/sequence/{series_id}",
            "meta": meta_info,
            "books": books,
        }


def deduplicate_books(books: List[Dict[str, Any]], target_format: str = "fb2") -> List[Dict[str, Any]]:
    """
    Оставляет только один (лучший/наиболее полный) вариант перевода/издания
    для каждого порядкового номера в серии.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    
    for b in books:
        key = b["seq_num_str"] if b.get("seq_num_str") else b["title"].lower()
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(b)

    deduped = []
    for key, variants in grouped.items():
        if len(variants) == 1:
            deduped.append(variants[0])
        else:
            def score_variant(v):
                has_fmt = 1 if target_format in v["formats"] else 0
                has_rosmen = 1 if "росмэн" in v["edition"].lower() or "шенин" in v["edition"].lower() else 0
                has_litres = 1 if "litres" in v["edition"].lower() else 0
                return (has_fmt, has_rosmen, has_litres, -len(v["edition"]))

            best = sorted(variants, key=score_variant, reverse=True)[0]
            deduped.append(best)

    # Сортируем по порядковому номеру
    return sorted(deduped, key=lambda x: x["seq_num"])


class SeriesDownloader:
    """Многопоточный загрузчик книг серии."""

    def __init__(
        self,
        client: FlibustaClient,
        output_dir: Path,
        fmt: str = "fb2",
        threads: int = 5,
        keep_zip: bool = False,
        force: bool = False,
        name_template: str = "{seq:02d}. {title} - {author}.{ext}",
    ):
        self.client = client
        self.output_dir = output_dir
        self.fmt = fmt.lower()
        self.threads = threads
        self.keep_zip = keep_zip
        self.force = force
        self.name_template = name_template

    def download_book(
        self,
        book: Dict[str, Any],
        series_dir: Path,
        progress: Progress,
        main_task: TaskID,
    ) -> Tuple[bool, str, int]:
        """Скачивает одну книгу в выбранном формате."""
        book_id = book["id"]
        title = book["title"]
        author = ", ".join(book["authors"]) if book["authors"] else "Автор"
        seq_num = int(book["seq_num"]) if book["seq_num"].is_integer() else book["seq_num"]

        # Определяем доступный формат (с фоллбэком)
        chosen_fmt = self.fmt
        if chosen_fmt not in book["formats"]:
            if "fb2" in book["formats"]:
                chosen_fmt = "fb2"
            elif "epub" in book["formats"]:
                chosen_fmt = "epub"
            elif "mobi" in book["formats"]:
                chosen_fmt = "mobi"
            elif "pdf" in book["formats"]:
                chosen_fmt = "pdf"
            else:
                progress.advance(main_task)
                return False, f"Формат {self.fmt} недоступен для '{title}' (ID {book_id})", 0

        download_path = book["formats"][chosen_fmt]
        ext = "fb2" if chosen_fmt == "fb2" and not self.keep_zip else chosen_fmt
        if chosen_fmt == "fb2" and self.keep_zip:
            ext = "fb2.zip"

        # Формируем имя файла
        clean_title = sanitize_filename(title)
        clean_author = sanitize_filename(author)
        
        try:
            seq_val = int(book["seq_num"]) if isinstance(book["seq_num"], (int, float)) and float(book["seq_num"]).is_integer() else 0
            filename = self.name_template.format(
                seq=seq_val,
                title=clean_title,
                author=clean_author,
                id=book_id,
                ext=ext,
            )
        except Exception:
            filename = f"{seq_num:02d}. {clean_title} - {clean_author}.{ext}"

        filename = sanitize_filename(filename)
        dest_file = series_dir / filename

        # Проверка существующего файла
        if dest_file.exists() and not self.force and dest_file.stat().st_size > 1024:
            progress.advance(main_task)
            return True, f"[yellow]Пропущен (уже существует)[/yellow]: {filename}", dest_file.stat().st_size

        # Скачиваем файл
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                resp = self.client.request("GET", download_path, stream=True, timeout=15)
                if resp.status_code != 200:
                    time.sleep(1.0 * attempt)
                    continue

                chunks = []
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        chunks.append(chunk)
                content = b"".join(chunks)

                if not content or len(content) < 100:
                    time.sleep(1.0 * attempt)
                    continue

                # Если это FB2 и не запрошено сохранение raw zip — распаковываем
                if chosen_fmt == "fb2" and not self.keep_zip:
                    if zipfile.is_zipfile(io.BytesIO(content)):
                        with zipfile.ZipFile(io.BytesIO(content)) as z:
                            fb2_files = [n for n in z.namelist() if n.lower().endswith(".fb2")]
                            if fb2_files:
                                content = z.read(fb2_files[0])
                            else:
                                content = z.read(z.namelist()[0])

                # Записываем файл на диск
                with open(dest_file, "wb") as f:
                    f.write(content)

                file_size = len(content)
                progress.advance(main_task)
                return True, f"[green]Скачан[/green]: {filename} ({file_size / (1024 * 1024):.2f} MB)", file_size

            except Exception as e:
                if attempt == retries:
                    progress.advance(main_task)
                    return False, f"[red]Ошибка скачивания[/red] '{title}' (ID {book_id}): {e}", 0
                time.sleep(2 * attempt)

        progress.advance(main_task)
        return False, f"[red]Не удалось скачать[/red] '{title}' (ID {book_id}) после {retries} попыток", 0

    def download_series(self, series_data: Dict[str, Any], books_to_download: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Запускает многопоточную выкачку всех выбранных книг серии."""
        series_title = series_data["title"]
        authors_str = ", ".join(series_data["meta"]["authors"]) if series_data["meta"]["authors"] else ""
        folder_name = sanitize_filename(f"{series_title} - {authors_str}" if authors_str and authors_str != "разные" else series_title)
        series_dir = self.output_dir / folder_name
        series_dir.mkdir(parents=True, exist_ok=True)

        total_books = len(books_to_download)
        console.print(f"\n[bold green]📁 Папка для сохранения:[/bold green] [cyan]{series_dir}[/cyan]")
        console.print(f"[bold cyan]⚡️ Запуск многопоточной загрузки ({self.threads} потоков)...[/bold cyan]\n")

        results = []
        total_bytes = 0
        success_count = 0
        fail_count = 0
        start_time = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TextColumn("[bold cyan]{task.completed}/{task.total}[/bold cyan] книг"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            main_task = progress.add_task(f"[bold cyan]Выкачивание серии '{series_title}'[/bold cyan]", total=total_books)

            with ThreadPoolExecutor(max_workers=self.threads) as executor:
                future_to_book = {
                    executor.submit(self.download_book, book, series_dir, progress, main_task): book
                    for book in books_to_download
                }

                for future in as_completed(future_to_book):
                    book = future_to_book[future]
                    try:
                        success, message, b_size = future.result()
                        if success:
                            success_count += 1
                            total_bytes += b_size
                        else:
                            fail_count += 1
                        progress.console.print(f"  {message}")
                        results.append((success, message))
                    except Exception as exc:
                        fail_count += 1
                        err_msg = f"[red]Критическая ошибка для '{book['title']}': {exc}[/red]"
                        progress.console.print(f"  {err_msg}")
                        results.append((False, err_msg))

        elapsed = max(time.time() - start_time, 0.1)
        speed_mb_s = (total_bytes / (1024 * 1024)) / elapsed

        # Сохраняем сводный файл серии (series_info.txt & JSON)
        self.save_series_info(series_dir, series_data, books_to_download, success_count, total_bytes)

        return {
            "series_dir": series_dir,
            "total_books": total_books,
            "success_count": success_count,
            "fail_count": fail_count,
            "total_bytes": total_bytes,
            "elapsed": elapsed,
            "speed_mb_s": speed_mb_s,
            "results": results,
        }

    def save_series_info(
        self,
        series_dir: Path,
        series_data: Dict[str, Any],
        books: List[Dict[str, Any]],
        downloaded_count: int,
        total_bytes: int,
    ):
        """Сохраняет описание серии и список книг в текстовый файл и JSON."""
        # TXT summary
        txt_path = series_dir / "series_info.txt"
        lines = [
            "=" * 70,
            f"📚 СЕРИЯ: {series_data['title']}",
            f"🔗 Источник: {series_data['url']}",
            f"👤 Авторы: {', '.join(series_data['meta']['authors']) or 'Не указаны'}",
            f"🗣 Переводчики: {', '.join(series_data['meta']['translators']) or 'Не указаны'}",
            f"🏷 Жанры: {', '.join(series_data['meta']['genres']) or 'Не указаны'}",
            f"📊 Всего книг в серии: {len(series_data['books'])} (скачано: {downloaded_count})",
            f"💾 Общий объем: {total_bytes / (1024 * 1024):.2f} MB",
            f"📅 Дата выгрузки: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 70,
            "\n📖 СПИСОК КНИГ:",
        ]

        for b in books:
            seq = b.get("seq_num_str", "-")
            ed = f" {b['edition']}" if b.get("edition") else ""
            lines.append(f"  [{seq:>2}] {b['title']}{ed} — {', '.join(b['authors'])} ({b.get('size_pages', '')})")

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # JSON metadata
        json_path = series_dir / "series_info.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "series_id": series_data["id"],
                "series_title": series_data["title"],
                "url": series_data["url"],
                "meta": series_data["meta"],
                "downloaded_books_count": downloaded_count,
                "total_bytes": total_bytes,
                "download_date": time.strftime('%Y-%m-%d %H:%M:%S'),
                "books": books,
            }, f, ensure_ascii=False, indent=2)


def display_series_table(series_data: Dict[str, Any], books: List[Dict[str, Any]], deduped: bool = True):
    """Отображает красивую сводную таблицу книг серии."""
    meta = series_data["meta"]
    
    # Инфо-панель
    info_lines = [
        f"[bold]Серия:[/bold] [yellow]{series_data['title']}[/yellow] (ID: {series_data['id']})",
        f"[bold]Авторы:[/bold] {', '.join(meta['authors']) or 'Разные / Не указаны'}",
        f"[bold]Жанры:[/bold] {', '.join(meta['genres']) or 'Не указаны'}",
        f"[bold]Всего на сайте:[/bold] {len(series_data['books'])} книг [dim]({'уникальных позиций: ' + str(len(books)) if deduped else 'со всеми переводами'})[/dim]",
    ]
    console.print(Panel("\n".join(info_lines), title="📖 [bold cyan]Информация о серии[/bold cyan]", border_style="cyan"))

    table = Table(title="📚 Список книг к загрузке", box=box.ROUNDED, header_style="bold magenta")
    table.add_column("№", justify="right", style="cyan", width=5)
    table.add_column("Название книги", style="white", min_width=30)
    table.add_column("Издание / Перевод", style="dim", min_width=20)
    table.add_column("Автор", style="green", min_width=18)
    table.add_column("Форматы", style="yellow", justify="center", width=16)
    table.add_column("Размер", style="blue", justify="right", width=12)

    for b in books:
        seq = b.get("seq_num_str", "-")
        fmts = " ".join([f"[{f}]" for f in b["formats"].keys()])
        authors = ", ".join(b["authors"])[:22]
        edition = b["edition"] or (b["translators"][0] if b["translators"] else "-")
        table.add_row(
            str(seq),
            b["title"][:40],
            edition[:28],
            authors,
            fmts or "[dim]нет[/dim]",
            b.get("size_pages", "-"),
        )

    console.print(table)


def parse_books_filter(filter_str: str, max_count: int) -> set:
    """Парсит строку фильтра книг, например '1-5,7,10'."""
    indices = set()
    parts = filter_str.split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                indices.update(range(start, end + 1))
            except ValueError:
                pass
        elif part.isdigit():
            indices.add(int(part))
    return indices


def interactive_search(client: FlibustaClient, query: str) -> Optional[str]:
    """Интерактивный поиск серии."""
    with console.status(f"[cyan]🔍 Поиск серий по запросу '{query}' на Флибусте...[/cyan]"):
        results = client.search_series(query)

    if not results:
        console.print(f"[bold red]❌ По запросу '{query}' серий не найдено![/bold red]")
        return None

    table = Table(title=f"🔎 Найденные серии по запросу '{query}'", box=box.ROUNDED)
    table.add_column("№", justify="right", style="cyan", width=4)
    table.add_column("ID", justify="center", style="yellow", width=8)
    table.add_column("Название серии", style="bold white")

    for i, r in enumerate(results, start=1):
        table.add_row(str(i), r["id"], r["title"])

    console.print(table)
    choice = Prompt.ask(
        "\n[bold green]Выберите номер серии для скачивания (или 'q' для отмены)[/bold green]",
        default="1"
    )
    if choice.lower() == "q":
        return None

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(results):
            return results[idx]["id"]
    except ValueError:
        pass

    console.print("[red]Неверный выбор.[/red]")
    return None


def main():
    parser = argparse.ArgumentParser(
        description="📚 Flibusta Series Downloader — многопоточный загрузчик книжных серий с Флибусты.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  flibusta-dl 2192                                   # Скачать серию по ID в FB2
  flibusta-dl "http://flibusta.is/sequence/1824"     # Скачать по официальной ссылке в FB2
  flibusta-dl --search "Ведьмак"                     # Найти и скачать серию
  flibusta-dl 1824 --format epub --threads 8         # Скачать в EPUB в 8 потоков
  flibusta-dl 1824 --books 1-3,5                     # Скачать только книги 1, 2, 3 и 5
  flibusta-dl 1824 --all-editions                    # Скачать все издания/переводы
        """,
    )

    parser.add_argument("target", nargs="?", default=None, help="ID серии, URL серии на Флибусте или поисковый запрос")
    parser.add_argument("-s", "--search", type=str, default=None, help="Поиск серии по названию")
    parser.add_argument("-f", "--format", dest="fmt", choices=["fb2", "epub", "mobi", "pdf"], default="fb2", help="Формат книг (по умолчанию: fb2)")
    parser.add_argument("-t", "--threads", type=int, default=5, help="Количество параллельных потоков загрузки (по умолчанию: 5)")
    parser.add_argument("-o", "--output", type=str, default="./downloads", help="Папка для сохранения книг (по умолчанию: ./downloads)")
    parser.add_argument("-u", "--unique", dest="unique", action="store_true", default=True, help="Дедупликация (один лучший перевод на номер книги, по умолчанию: True)")
    parser.add_argument("--all-editions", dest="unique", action="store_false", help="Скачивать абсолютно все переводы и издания каждой книги")
    parser.add_argument("-b", "--books", type=str, default=None, help="Фильтр книг по номерам, например: '1-5,7,10'")
    parser.add_argument("-m", "--max-books", type=int, default=None, help="Максимальное количество книг для скачивания")
    parser.add_argument("--keep-zip", action="store_true", help="Сохранять FB2 в исходных .zip архивах (не распаковывать в .fb2)")
    parser.add_argument("--force", action="store_true", help="Перезаписывать уже существующие файлы")
    parser.add_argument("--host", "--url", type=str, default=DEFAULT_HOST, help=f"Адрес официального сервера Флибусты (по умолчанию: {DEFAULT_HOST})")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP/SOCKS5 прокси (например для Tor: socks5://127.0.0.1:9050)")
    parser.add_argument("--name-template", type=str, default="{seq:02d}. {title} - {author}.{ext}", help="Шаблон имени файла (по умолчанию: '{seq:02d}. {title} - {author}.{ext}')")
    parser.add_argument("-y", "--yes", action="store_true", help="Не запрашивать подтверждение перед стартом загрузки")

    args = parser.parse_args()

    console.print(BANNER)

    client = FlibustaClient(base_url=args.host, proxy=args.proxy)

    target = args.target or args.search
    if not target:
        # Интерактивный ввод
        target = Prompt.ask("[bold green]Введите ID серии, URL или поисковый запрос[/bold green]")

    series_id = None
    if target.isdigit():
        series_id = target
    elif "/sequence/" in target or "/s/" in target:
        match = re.search(r"/(?:sequence|s)/(\d+)", target)
        if match:
            series_id = match.group(1)
        else:
            series_id = target
    else:
        # Если передан текстовый поисковый запрос
        series_id = interactive_search(client, target)
        if not series_id:
            return

    # Загружаем и парсим данные серии
    with console.status(f"[bold cyan]⏳ Получение списка книг серии ID {series_id} с Флибусты...[/bold cyan]"):
        try:
            series_data = client.parse_series(series_id)
        except Exception as e:
            console.print(f"[bold red]❌ Ошибка парсинга серии {series_id}: {e}[/bold red]")
            return

    all_books = series_data["books"]
    if not all_books:
        console.print("[bold red]❌ В этой серии не найдено книг![/bold red]")
        return

    # Дедупликация
    if args.unique:
        books_to_download = deduplicate_books(all_books, target_format=args.fmt)
    else:
        books_to_download = all_books

    # Фильтрация по номерам
    if args.books:
        allowed_nums = parse_books_filter(args.books, len(books_to_download))
        books_to_download = [
            b for b in books_to_download
            if (isinstance(b.get("seq_num"), (int, float)) and int(b["seq_num"]) in allowed_nums)
            or b.get("index") in allowed_nums
        ]

    # Ограничение по макс. количеству
    if args.max_books and args.max_books > 0:
        books_to_download = books_to_download[:args.max_books]

    if not books_to_download:
        console.print("[bold red]❌ После применения фильтров не осталось книг для скачивания![/bold red]")
        return

    # Показываем таблицу
    display_series_table(series_data, books_to_download, deduped=args.unique)

    # Подтверждение
    if not args.yes:
        confirm = Confirm.ask(
            f"\n[bold green]Начать скачивание {len(books_to_download)} книг в формате [yellow]{args.fmt.upper()}[/yellow]?[/bold green]",
            default=True,
        )
        if not confirm:
            console.print("[yellow]Загрузка отменена пользователем.[/yellow]")
            return

    output_dir = Path(args.output).expanduser().resolve()
    downloader = SeriesDownloader(
        client=client,
        output_dir=output_dir,
        fmt=args.fmt,
        threads=args.threads,
        keep_zip=args.keep_zip,
        force=args.force,
        name_template=args.name_template,
    )

    stats = downloader.download_series(series_data, books_to_download)

    # Финальная статистика
    summary_table = Table(title="🎉 Итоги загрузки серии", box=box.HEAVY_EDGE, border_style="green")
    summary_table.add_column("Параметр", style="bold cyan")
    summary_table.add_column("Значение", style="bold white")

    summary_table.add_row("Серия", series_data["title"])
    summary_table.add_row("Папка", str(stats["series_dir"]))
    summary_table.add_row("Успешно скачано", f"[green]{stats['success_count']} из {stats['total_books']}[/green]")
    if stats["fail_count"] > 0:
        summary_table.add_row("Ошибок", f"[red]{stats['fail_count']}[/red]")
    summary_table.add_row("Общий объем", f"{stats['total_bytes'] / (1024 * 1024):.2f} MB")
    summary_table.add_row("Время выполнения", f"{stats['elapsed']:.1f} сек")
    summary_table.add_row("Средняя скорость", f"{stats['speed_mb_s']:.2f} MB/s")

    console.print()
    console.print(summary_table)
    console.print(f"\n[bold green]✅ Выкачка завершена! Все файлы сохранены в:[/bold green] [cyan]{stats['series_dir']}[/cyan]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Загрузка прервана пользователем (Ctrl+C).[/yellow]")
        sys.exit(130)
