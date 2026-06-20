"""
Patch missing descriptions AND images in already-generated Shopify CSVs.

Sources tried in order:
  1. Open Library Works API  (free, descriptions only)
  2. Google Books API         (free 1000/day, images + descriptions)
  3. ISBNdb API               (pass --isbndb KEY, images + descriptions)
  4. Claude Haiku             (pass --claude, descriptions only, last resort)

CSVs are processed in filename order: 001, 002, 003 ...

Usage:
  py patch_descriptions.py
  py patch_descriptions.py --isbndb YOUR_KEY
  py patch_descriptions.py --claude
  py patch_descriptions.py --isbndb YOUR_KEY --claude
"""
import sys, json, csv, os, re, time, requests, unicodedata, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.modules.pop("enrich_books", None)
from enrich_books import (
    CACHE_FILE, SHOPIFY_HEADERS, clean_text,
    _fetch_works_description, HEADERS,
)

from rich.progress import (
    Progress, BarColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn,
)
from rich.console import Console
from rich.rule import Rule

console = Console(force_terminal=True, highlight=False)

# ── CLI flags ──────────────────────────────────────────────────────────────────
USE_CLAUDE = "--claude" in sys.argv
ISBNDB_KEY = next(
    (sys.argv[i+1] for i, a in enumerate(sys.argv)
     if a == "--isbndb" and i+1 < len(sys.argv)), None
)

WORKERS     = 8
CACHE_DELAY = 0.1
CSV_GLOB    = "shopify_import_*.csv"
CACHE_BACKUP = "isbn_cache_pre_patch.json"

GOOGLE_URL = "https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&maxResults=1"
ISBNDB_URL = "https://api2.isbndb.com/book/{isbn}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_description(body_html: str) -> bool:
    return bool(re.search(r"<p>.+?</p>", body_html, re.DOTALL))


def _has_real_image(img_src: str) -> bool:
    return bool(img_src and "/b/id/" in img_src)


def _inject_description(body_html: str, description: str) -> str:
    return f"<p>{description}</p>\n{body_html}"


def _fetch_works_for_isbn(isbn: str) -> str:
    try:
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
        r = requests.get(url, headers=HEADERS, timeout=(5, 15))
        if r.status_code != 200:
            return ""
        data = r.json().get(f"ISBN:{isbn}", {})
        works = data.get("works", [])
        if not works:
            return ""
        return _fetch_works_description(works[0].get("key", ""))
    except Exception:
        return ""


def _fetch_google_books(isbn: str) -> dict:
    try:
        r = requests.get(GOOGLE_URL.format(isbn=isbn), headers=HEADERS, timeout=(5, 15))
        if r.status_code == 429:
            return {"quota": True}
        if r.status_code != 200:
            return {}
        items = r.json().get("items", [])
        if not items:
            return {}
        info  = items[0].get("volumeInfo", {})
        links = info.get("imageLinks", {})
        image = (
            links.get("extraLarge") or links.get("large") or
            links.get("medium")    or links.get("small")  or
            links.get("thumbnail") or ""
        )
        if image.startswith("http://"):
            image = "https://" + image[7:]
        return {"image": image, "description": clean_text(info.get("description", ""))}
    except Exception:
        return {}


def _fetch_isbndb(isbn: str, key: str) -> dict:
    try:
        r = requests.get(
            ISBNDB_URL.format(isbn=isbn),
            headers={**HEADERS, "Authorization": key},
            timeout=(5, 15),
        )
        if r.status_code != 200:
            return {}
        book  = r.json().get("book", {})
        image = clean_text(book.get("image", ""))
        desc  = clean_text(book.get("synopsis", ""))
        return {"image": image, "description": desc}
    except Exception:
        return {}


def _generate_with_claude(isbn: str, title: str, authors: str, categories: list) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic()
        genre  = ", ".join(categories[:3]) if categories else "general"
        prompt = (
            f"Write a compelling 2-3 sentence book description for '{title}' by {authors}. "
            f"Genre/subjects: {genre}. Be informative and engaging. No spoilers. No markdown."
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return clean_text(msg.content[0].text)
    except Exception as e:
        console.print(f"  [red]Claude error for {isbn}: {e}[/red]")
        return ""


def _run_phase(name: str, targets: list, worker_fn, cache: dict, progress_desc: str):
    """Run a fetch phase with a live progress bar. Returns updated cache."""
    console.print(Rule(f"[bold]{name}[/bold]"))
    console.print(f"[dim]{len(targets)} ISBNs to process[/dim]\n")

    got_img = got_desc = quota = 0

    with Progress(
        SpinnerColumn(),
        BarColumn(bar_width=35),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>5.1f}%"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        TextColumn("{task.fields[stats]}"),
        console=console,
        transient=False,
        refresh_per_second=4,
    ) as prog:
        task = prog.add_task(progress_desc, total=len(targets), stats="img=0 desc=0")

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            future_map = {ex.submit(worker_fn, isbn): isbn for isbn in targets}
            for fut in as_completed(future_map):
                isbn   = future_map[fut]
                result = fut.result()

                if result.get("quota"):
                    quota += 1
                    prog.console.print(f"[yellow]~[/yellow] [dim]{isbn}[/dim]  [yellow]quota hit[/yellow]")
                    prog.update(task, advance=1, stats=f"img={got_img} desc={got_desc} quota={quota}")
                    continue

                entry    = cache.setdefault(isbn, {})
                new_img  = result.get("image", "")
                new_desc = result.get("description", "")

                patched_img  = False
                patched_desc = False

                if new_img and not _has_real_image(entry.get("image", "")):
                    entry["image"] = new_img
                    got_img += 1
                    patched_img = True

                if new_desc and not entry.get("description"):
                    entry["description"] = new_desc
                    got_desc += 1
                    patched_desc = True

                # Build status line
                img_tag  = "[green]IMG[/green]"  if patched_img  else "[dim]img[/dim]"
                desc_tag = "[green]DESC[/green]" if patched_desc else "[dim]desc[/dim]"
                dot      = "[green]OK[/green]"   if (patched_img or patched_desc) else "[red]x[/red]"

                title_preview = (entry.get("title") or isbn)[:40]
                prog.console.print(
                    f"{dot} [dim]{isbn}[/dim]  {title_preview:<41}  {img_tag}  {desc_tag}"
                )
                prog.update(task, advance=1, stats=f"img={got_img} desc={got_desc}")
                time.sleep(CACHE_DELAY)

    console.print(f"\n[bold]Result:[/bold] {got_img} images + {got_desc} descriptions filled.")
    if quota:
        console.print(f"[yellow]Quota hits: {quota}[/yellow]")
    return cache


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(CACHE_FILE):
        console.print("[red]No isbn_cache.json found — run the main enrichment first.[/red]")
        return

    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)
    console.print(f"[dim]Loaded {len(cache)} cached ISBNs.[/dim]")

    if not os.path.exists(CACHE_BACKUP):
        shutil.copy(CACHE_FILE, CACHE_BACKUP)
        console.print(f"[dim]Cache backed up to {CACHE_BACKUP}[/dim]")

    csv_files = sorted(Path(".").glob(CSV_GLOB))
    if not csv_files:
        console.print("[red]No shopify_import_*.csv files found.[/red]")
        return
    console.print(f"[dim]Found {len(csv_files)} CSV files — processing in order.[/dim]")

    # Collect what needs fixing
    needs_desc  = []
    needs_image = []
    for csv_path in csv_files:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                isbn = row.get("Variant SKU", "").strip()
                if not isbn:
                    continue
                if not _has_description(row.get("Body (HTML)", "")):
                    needs_desc.append(isbn)
                if not _has_real_image(row.get("Image Src", "")):
                    needs_image.append(isbn)

    needs_desc  = list(dict.fromkeys(needs_desc))
    needs_image = list(dict.fromkeys(needs_image))

    console.print(Rule())
    console.print(f"[bold yellow]{len(needs_desc)}[/bold yellow] ISBNs missing descriptions")
    console.print(f"[bold yellow]{len(needs_image)}[/bold yellow] ISBNs missing real cover images")

    def _save_cache():
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    # ── Phase 1: OL Works ─────────────────────────────────────────────────────
    cache = _run_phase(
        "Phase 1: Open Library Works API (descriptions)",
        needs_desc,
        lambda isbn: {"description": _fetch_works_for_isbn(isbn)},
        cache,
        "OL Works",
    )
    _save_cache()

    # ── Phase 2: Google Books ─────────────────────────────────────────────────
    gb_targets = list(dict.fromkeys(
        needs_image +
        [i for i in needs_desc if not cache.get(i, {}).get("description")]
    ))
    cache = _run_phase(
        "Phase 2: Google Books (images + descriptions)",
        gb_targets,
        _fetch_google_books,
        cache,
        "Google Books",
    )
    _save_cache()

    # ── Phase 3: ISBNdb ───────────────────────────────────────────────────────
    if ISBNDB_KEY:
        idb_targets = list(dict.fromkeys(
            [i for i in needs_image if not _has_real_image(cache.get(i, {}).get("image", ""))] +
            [i for i in needs_desc  if not cache.get(i, {}).get("description")]
        ))
        cache = _run_phase(
            "Phase 3: ISBNdb (images + descriptions)",
            idb_targets,
            lambda isbn: _fetch_isbndb(isbn, ISBNDB_KEY),
            cache,
            "ISBNdb",
        )
        _save_cache()
    else:
        console.print(Rule("[dim]Phase 3: ISBNdb skipped (no --isbndb key)[/dim]"))

    # ── Phase 4: Claude ───────────────────────────────────────────────────────
    if USE_CLAUDE:
        claude_targets = [i for i in needs_desc if not cache.get(i, {}).get("description")]
        console.print(Rule("Phase 4: Claude Haiku (descriptions)"))
        filled = 0
        for i, isbn in enumerate(claude_targets, 1):
            entry   = cache.get(isbn, {})
            desc = _generate_with_claude(
                isbn,
                entry.get("title", f"ISBN {isbn}"),
                entry.get("authors", "Unknown"),
                entry.get("categories", []),
            )
            if desc:
                cache.setdefault(isbn, {})["description"] = desc
                filled += 1
                console.print(f"[green]OK[/green] [dim]{isbn}[/dim]  [green]DESC[/green]")
            else:
                console.print(f"[red]x[/red] [dim]{isbn}[/dim]  [dim]no desc[/dim]")
            if i % 50 == 0:
                _save_cache()
        console.print(f"\nClaude filled {filled} descriptions.")
        _save_cache()
    else:
        console.print(Rule("[dim]Phase 4: Claude skipped (no --claude flag)[/dim]"))

    # ── Rewrite CSVs in order ─────────────────────────────────────────────────
    console.print(Rule("[bold]Rewriting CSVs (001 → 002 → ...)[/bold]"))
    total_desc = total_img = 0

    for csv_path in csv_files:
        desc_n = img_n = 0
        tmp    = str(csv_path) + ".tmp"

        with open(csv_path, "r", encoding="utf-8-sig") as fin, \
             open(tmp, "w", newline="", encoding="utf-8-sig") as fout:

            reader = csv.DictReader(fin)
            writer = csv.DictWriter(fout, fieldnames=SHOPIFY_HEADERS)
            writer.writeheader()

            for row in reader:
                isbn = row.get("Variant SKU", "").strip()
                if isbn:
                    entry = cache.get(isbn, {})
                    body  = row.get("Body (HTML)", "")
                    if not _has_description(body):
                        desc = entry.get("description", "")
                        if desc:
                            row["Body (HTML)"] = _inject_description(body, desc)
                            desc_n += 1
                    if not _has_real_image(row.get("Image Src", "")):
                        new_img = entry.get("image", "")
                        if new_img and _has_real_image(new_img):
                            row["Image Src"]      = new_img
                            row["Image Position"] = "1"
                            img_n += 1
                writer.writerow(row)

        os.replace(tmp, csv_path)
        console.print(
            f"  [cyan]{csv_path.name}[/cyan]  "
            f"[green]{desc_n} desc[/green]  [green]{img_n} img[/green]"
        )
        total_desc += desc_n
        total_img  += img_n

    console.print(Rule())
    console.print(
        f"[bold green]Done![/bold green]  "
        f"[green]{total_desc} descriptions[/green] + "
        f"[green]{total_img} images[/green] patched across {len(csv_files)} files."
    )


if __name__ == "__main__":
    main()
