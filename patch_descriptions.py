"""
Patch missing descriptions AND images in already-generated Shopify CSVs.

ISBNdb bulk API: 100 ISBNs per POST call, 1 call/sec → 57k ISBNs in ~10 min.

Usage:
  py patch_descriptions.py --isbndb YOUR_KEY
  py patch_descriptions.py --isbndb YOUR_KEY --claude
"""
import sys, json, csv, os, re, time, requests, unicodedata, shutil
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.modules.pop("enrich_books", None)
from enrich_books import CACHE_FILE, SHOPIFY_HEADERS, clean_text, HEADERS

from rich.progress import (
    Progress, BarColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn, SpinnerColumn,
)
from rich.console import Console
from rich.rule import Rule

console = Console(force_terminal=True, highlight=False)

# ── CLI flags ──────────────────────────────────────────────────────────────────
USE_CLAUDE   = "--claude" in sys.argv
REWRITE_ONLY = "--rewrite-only" in sys.argv
ISBNDB_KEY   = next(
    (sys.argv[i+1] for i, a in enumerate(sys.argv)
     if a == "--isbndb" and i+1 < len(sys.argv)), None
)

CALL_DELAY   = 1.05  # seconds between calls (stay under 1/sec limit)
CSV_GLOB     = "shopify_import_*.csv"
CACHE_BACKUP = "isbn_cache_pre_patch.json"

ISBNDB_URL = "https://api2.isbndb.com/book/{isbn}"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_description(body_html: str) -> bool:
    return bool(re.search(r"<p>.+?</p>", body_html, re.DOTALL))


def _has_real_image(img_src: str) -> bool:
    """Real image = any non-empty URL that isn't an OL placeholder/fallback."""
    if not img_src:
        return False
    if "covers.openlibrary.org/b/isbn/" in img_src:
        return False
    if "covers.openlibrary.org/b/id/-1-" in img_src:
        return False
    return True


def _inject_description(body_html: str, description: str) -> str:
    return f"<p>{description}</p>\n{body_html}"


def _fetch_isbndb(isbn: str, key: str) -> dict:
    """GET single book from ISBNdb. Returns {image, description, title} or {}."""
    try:
        r = requests.get(
            ISBNDB_URL.format(isbn=isbn),
            headers={**HEADERS, "Authorization": key},
            timeout=(5, 15),
        )
        if r.status_code == 429:
            return {"_429": True}
        if r.status_code != 200:
            return {}
        book = r.json().get("book", {})
        return {
            "image":       clean_text(book.get("image", "")),
            "description": clean_text(book.get("synopsis", "")),
            "title":       clean_text(book.get("title", "")),
        }
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(CACHE_FILE):
        console.print("[red]No isbn_cache.json found.[/red]")
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

    # ── Scan CSVs for missing data ─────────────────────────────────────────────
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

    needs_desc  = set(dict.fromkeys(needs_desc))
    needs_image = set(dict.fromkeys(needs_image))
    all_targets = list(dict.fromkeys(list(needs_image) + list(needs_desc)))

    console.print(Rule())
    console.print(f"[bold yellow]{len(needs_desc)}[/bold yellow] ISBNs missing descriptions")
    console.print(f"[bold yellow]{len(needs_image)}[/bold yellow] ISBNs missing real cover images")
    console.print(f"[bold yellow]{len(all_targets)}[/bold yellow] unique ISBNs to process")

    # How many are already satisfied by cache from previous runs
    already_done = sum(
        1 for isbn in all_targets
        if (isbn not in needs_image or _has_real_image(cache.get(isbn, {}).get("image", "")))
        and (isbn not in needs_desc  or cache.get(isbn, {}).get("description"))
    )
    console.print(f"[dim]{already_done} already fixed in cache (will skip) → {len(all_targets)-already_done} real API calls needed[/dim]")

    def _save_cache():
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    # ── Phase 1: ISBNdb ───────────────────────────────────────────────────────
    if REWRITE_ONLY:
        console.print(Rule("[bold yellow]--rewrite-only: skipping all API calls[/bold yellow]"))
    elif ISBNDB_KEY:
        console.print(Rule("[bold]Phase 1: ISBNdb (1 call/sec)[/bold]"))
        console.print(f"[dim]{len(all_targets)} ISBNs @ 5000/day limit[/dim]\n")

        got_img = got_desc = errors = 0

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
            refresh_per_second=4,
        ) as prog:
            task = prog.add_task("ISBNdb", total=len(all_targets), stats="img=0 desc=0")

            for idx, isbn in enumerate(all_targets, 1):
                # Skip if cache already satisfies what this ISBN needed
                entry = cache.get(isbn, {})
                still_needs_img  = (isbn in needs_image) and not _has_real_image(entry.get("image", ""))
                still_needs_desc = (isbn in needs_desc)  and not entry.get("description")
                if not still_needs_img and not still_needs_desc:
                    prog.update(task, advance=1, stats=f"img={got_img} desc={got_desc}")
                    continue

                book  = _fetch_isbndb(isbn, ISBNDB_KEY)

                if book.get("_429"):
                    prog.console.print(f"[yellow]429[/yellow] [dim]{isbn}[/dim]  rate limited — skipping")
                    prog.update(task, advance=1, stats=f"img={got_img} desc={got_desc}")
                    time.sleep(CALL_DELAY)
                    continue

                entry = cache.setdefault(isbn, {})

                new_img   = book.get("image", "")
                new_desc  = book.get("description", "")
                new_title = book.get("title", "")

                patched_img = patched_desc = False

                if new_title and not entry.get("title"):
                    entry["title"] = new_title

                if new_img and not _has_real_image(entry.get("image", "")):
                    entry["image"] = new_img
                    got_img += 1
                    patched_img = True

                if new_desc and not entry.get("description"):
                    entry["description"] = new_desc
                    got_desc += 1
                    patched_desc = True

                dot      = "[green]OK[/green]" if (patched_img or patched_desc) else "[red]x[/red]"
                img_tag  = "[green]IMG[/green]"  if patched_img  else "[dim]img[/dim]"
                desc_tag = "[green]DESC[/green]" if patched_desc else "[dim]desc[/dim]"
                title_preview = (entry.get("title") or isbn)[:38]
                prog.console.print(
                    f"{dot} [dim]{isbn}[/dim]  {title_preview:<39}  {img_tag}  {desc_tag}"
                )
                prog.update(task, advance=1, stats=f"img={got_img} desc={got_desc}")

                if idx % 100 == 0:
                    _save_cache()

                time.sleep(CALL_DELAY)

        _save_cache()
        console.print(f"\n[bold]ISBNdb result:[/bold] [green]{got_img} images[/green] + [green]{got_desc} descriptions[/green] filled.")
    else:
        console.print(Rule("[dim]Phase 1: ISBNdb skipped (no --isbndb key)[/dim]"))

    # ── Phase 2: Claude (optional) ────────────────────────────────────────────
    if USE_CLAUDE and not REWRITE_ONLY:
        claude_targets = [i for i in needs_desc if not cache.get(i, {}).get("description")]
        console.print(Rule("Phase 2: Claude Haiku (descriptions only)"))
        console.print(f"[dim]{len(claude_targets)} still missing descriptions[/dim]\n")
        filled = 0
        for i, isbn in enumerate(claude_targets, 1):
            entry = cache.get(isbn, {})
            desc  = _generate_with_claude(
                isbn,
                entry.get("title",   f"ISBN {isbn}"),
                entry.get("authors", "Unknown"),
                entry.get("categories", []),
            )
            if desc:
                cache.setdefault(isbn, {})["description"] = desc
                filled += 1
                console.print(f"[green]OK[/green] [dim]{isbn}[/dim]  [green]DESC[/green]")
            else:
                console.print(f"[red]x[/red]  [dim]{isbn}[/dim]")
            if i % 50 == 0:
                _save_cache()
        _save_cache()
        console.print(f"\nClaude filled {filled}/{len(claude_targets)} descriptions.")
    else:
        console.print(Rule("[dim]Phase 2: Claude skipped (pass --claude to enable)[/dim]"))

    # ── Rewrite CSVs in order ─────────────────────────────────────────────────
    console.print(Rule("[bold]Rewriting CSVs (001 → 002 → ...)[/bold]"))
    total_desc_n = total_img_n = 0

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
        total_desc_n += desc_n
        total_img_n  += img_n

    console.print(Rule())
    console.print(
        f"[bold green]All done![/bold green]  "
        f"[green]{total_desc_n} descriptions[/green] + "
        f"[green]{total_img_n} images[/green] patched across {len(csv_files)} files."
    )


if __name__ == "__main__":
    main()
