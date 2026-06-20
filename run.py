"""
Book enrichment runner — 5 parallel workers, rich live UI.
Usage: py run.py
"""
import sys, time, os, json, csv, threading
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.modules.pop("enrich_books", None)

from enrich_books import (
    EXCEL_FILE, SHOPIFY_HEADERS, REJECTED_HEADERS, clean_text,
    fetch_open_library, verify_image, build_shopify_row,
    CACHE_FILE, PROGRESS_FILE, OUTPUT_CSV, REJECTED_CSV,
    CHUNK_SIZE, save_cache, save_progress,
)
import openpyxl
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutTimeout

from rich.progress import (
    Progress, BarColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn
)
from rich.console import Console
from rich.rule import Rule

console = Console(force_terminal=True, highlight=False)

WORKERS      = 10
BOOK_TIMEOUT = 30   # seconds before a book is skipped
BATCH        = WORKERS * 4  # how many to keep in-flight at once


def _fetch_book(i: int, isbn: str, excel: dict, cached: dict | None):
    """Worker: fetch OL data and build Shopify row. Returns tuple."""
    t0 = time.time()
    try:
        if cached is not None:
            api = cached
        else:
            api = fetch_open_library(isbn)
        img = verify_image(api.get("image", ""), isbn)
        row = build_shopify_row(isbn, excel, api, verified_image=img)
        return (i, isbn, excel, row, api, time.time() - t0, None)
    except Exception as e:
        return (i, isbn, excel, None, {}, time.time() - t0, e)


def _chunk_path(n: int) -> str:
    base = Path(OUTPUT_CSV)
    return str(base.with_stem(f"{base.stem}_{n:03d}"))


def _count_csv_rows(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return sum(1 for _ in f) - 1
    except FileNotFoundError:
        return 0


def main():
    cache_lock = threading.Lock()

    # ── Load cache & progress ──────────────────────────────────────────────────
    cache: dict = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        console.print(f"[dim]Loaded {len(cache)} cached ISBNs.[/dim]")

    start_row = 0
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            start_row = json.load(f).get("last_row", 0)
        console.print(f"[dim]Resuming from row {start_row}.[/dim]")

    # ── Load Excel ─────────────────────────────────────────────────────────────
    console.print("[cyan]Loading Excel...[/cyan]", end=" ")
    t0 = time.time()
    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()
    total = len(rows)
    console.print(f"[green]done[/green] ({total:,} rows, {time.time()-t0:.1f}s)")
    console.print(Rule())

    # ── Open output files ──────────────────────────────────────────────────────
    chunking = CHUNK_SIZE > 0
    accepted = skipped = 0
    chunk_num = 1
    rows_in_chunk = 0

    resuming = start_row > 0
    if resuming and chunking:
        existing = sorted(Path(".").glob(_chunk_path(0).replace("000", "*").lstrip("./")))
        chunk_num = max(len(existing), 1)
        rows_in_chunk = _count_csv_rows(str(existing[-1])) if existing else 0

    def _open_chunk(n, append):
        path = _chunk_path(n) if chunking else OUTPUT_CSV
        f = open(path, "a" if append else "w", newline="", encoding="utf-8-sig")
        w = csv.DictWriter(f, fieldnames=SHOPIFY_HEADERS)
        if not append:
            w.writeheader()
        return f, w

    main_f, writer = _open_chunk(chunk_num, append=resuming)
    rej_f = open(REJECTED_CSV, "a" if resuming else "w", newline="", encoding="utf-8-sig")
    rej_writer = csv.DictWriter(rej_f, fieldnames=REJECTED_HEADERS)
    if not resuming:
        rej_writer.writeheader()

    chunk_paths = [_chunk_path(chunk_num) if chunking else OUTPUT_CSV]
    last_completed_row = start_row
    run_start = time.time()

    # ── Build pending list ─────────────────────────────────────────────────────
    pending = []
    for i, row in enumerate(rows):
        if i < start_row:
            continue
        isbn_raw = row[0]
        if not isbn_raw:
            continue
        try:
            isbn = str(int(isbn_raw))
        except Exception:
            continue
        (_, name, author, publisher, binding, language, _, price, stock, _) = row
        excel = {
            "name": name or "", "AUTHOR1": author or "",
            "PUBLISHER": publisher or "", "BINDINGTYPE": binding or "",
            "LANGUAGE": language or "", "price": price or 0, "stock": stock or 0,
        }
        pending.append((i, isbn, excel, row))

    remaining = len(pending)
    console.print(f"[dim]{remaining:,} books to process with {WORKERS} workers[/dim]\n")

    # ── Progress bar ───────────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        TextColumn("{task.fields[speed]}"),
        console=console,
        transient=False,
        refresh_per_second=4,
    ) as progress:

        task = progress.add_task("", total=remaining, speed="-- bk/s")

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:

            # Process in batches — keeps BATCH futures in-flight at once
            for batch_start in range(0, len(pending), BATCH):
                batch = pending[batch_start : batch_start + BATCH]

                # Submit all in batch simultaneously
                future_map = {}
                for i, isbn, excel, raw_row in batch:
                    with cache_lock:
                        cached = cache.get(isbn)
                    fut = executor.submit(_fetch_book, i, isbn, excel, cached)
                    future_map[fut] = (i, isbn, excel, raw_row)

                # Collect results as they complete (parallel!)
                for fut in as_completed(future_map):
                    i, isbn, excel, raw_row = future_map[fut]
                    (_, name, author, publisher,
                     binding, language, _, price, stock, _) = raw_row

                    try:
                        _, _isbn, _excel, shopify_row, api, elapsed, err = fut.result(timeout=BOOK_TIMEOUT)
                        if err:
                            raise err
                    except Exception as e:
                        elapsed = 0
                        title_disp = clean_text(name or f"ISBN {isbn}")[:42]
                        progress.console.print(
                            f"[red]x[/red] [dim]{i+1:05d}[/dim]  "
                            f"{title_disp:<43}  [red]{str(e)[:50]}[/red]"
                        )
                        rej_writer.writerow({
                            "ISBN": isbn, "Original Title": clean_text(name or ""),
                            "Author": clean_text(author or ""),
                            "Publisher": clean_text(publisher or ""),
                            "Binding": clean_text(binding or ""),
                            "Language": clean_text(language or ""),
                            "Price": str(price or ""), "Stock": str(int(stock)) if stock else "0",
                            "Reason": str(e)[:200],
                        })
                        skipped += 1
                    else:
                        # Chunk rollover
                        if chunking and rows_in_chunk >= CHUNK_SIZE:
                            main_f.close()
                            chunk_num += 1
                            rows_in_chunk = 0
                            main_f, writer = _open_chunk(chunk_num, append=False)
                            chunk_paths.append(_chunk_path(chunk_num))

                        writer.writerow(shopify_row)
                        accepted += 1
                        rows_in_chunk += 1

                        title_disp = shopify_row["Title"][:42] or f"ISBN {isbn}"
                        src_tag = "OL" if shopify_row["Body (HTML)"] else "Excel"
                        progress.console.print(
                            f"[green]OK[/green] [dim]{i+1:05d}[/dim]  "
                            f"{title_disp:<43}  [yellow]{elapsed:.1f}s[/yellow]  "
                            f"Rs.{price or 0:<6}  [dim]{src_tag}[/dim]"
                        )

                        with cache_lock:
                            cache[isbn] = {**api, "verified_image": shopify_row["Image Src"]}

                    last_completed_row = max(last_completed_row, i + 1)
                    done = last_completed_row - start_row
                    speed = done / max(time.time() - run_start, 1)
                    progress.update(task, advance=1, speed=f"{speed:.2f} bk/s")

                # Checkpoint after each batch
                main_f.flush()
                with cache_lock:
                    save_cache(cache)
                save_progress(last_completed_row)

    main_f.close()
    rej_f.close()
    with cache_lock:
        save_cache(cache)
    save_progress(last_completed_row)

    elapsed_h = (time.time() - run_start) / 3600
    console.print(Rule())
    console.print(
        f"[bold green]Done![/bold green]  "
        f"accepted=[green]{accepted:,}[/green]  "
        f"skipped=[red]{skipped}[/red]  "
        f"time=[yellow]{elapsed_h:.2f}h[/yellow]"
    )
    for p in (chunk_paths if chunking else [OUTPUT_CSV]):
        console.print(f"  -> {p}")


if __name__ == "__main__":
    main()
