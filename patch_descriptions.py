"""
Patch missing descriptions in already-generated shopify CSVs.

Phase 1: Open Library Works API (free)
Phase 2: Claude AI generation (pass --claude flag, costs ~$3-4)

Usage:
  py patch_descriptions.py            # OL Works only
  py patch_descriptions.py --claude   # OL Works + Claude fallback
"""
import sys, json, csv, os, re, time, requests, unicodedata
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.modules.pop("enrich_books", None)
from enrich_books import (
    CACHE_FILE, SHOPIFY_HEADERS, clean_text,
    _fetch_works_description, HEADERS,
)

USE_CLAUDE  = "--claude" in sys.argv
WORKERS     = 8
CACHE_DELAY = 0.1

CSV_GLOB    = "shopify_import_*.csv"
CACHE_BACKUP = "isbn_cache_pre_patch.json"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_description(body_html: str) -> bool:
    return bool(re.search(r"<p>.+?</p>", body_html, re.DOTALL))


def _inject_description(body_html: str, description: str) -> str:
    return f"<p>{description}</p>\n{body_html}"


def _fetch_works_for_isbn(isbn: str) -> str:
    """Re-hit OL Books API to get works key, then fetch description."""
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


def _generate_with_claude(isbn: str, title: str, authors: str, categories: list) -> str:
    """Generate a short description using Claude Haiku."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        genre = ", ".join(categories[:3]) if categories else "general"
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
        print(f"  Claude error for {isbn}: {e}")
        return ""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Load cache
    if not os.path.exists(CACHE_FILE):
        print("No isbn_cache.json found — run the main enrichment first.")
        return
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)

    # Backup cache before modifying
    if not os.path.exists(CACHE_BACKUP):
        import shutil
        shutil.copy(CACHE_FILE, CACHE_BACKUP)
        print(f"Cache backed up to {CACHE_BACKUP}")

    # Find CSV files
    csv_files = sorted(Path(".").glob(CSV_GLOB))
    if not csv_files:
        print("No shopify_import_*.csv files found.")
        return
    print(f"Found {len(csv_files)} CSV file(s): {[f.name for f in csv_files]}")

    # Phase 1: find ISBNs needing description
    needs_desc = []
    for csv_path in csv_files:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                isbn = row.get("Variant SKU", "").strip()
                body  = row.get("Body (HTML)", "")
                if isbn and not _has_description(body):
                    needs_desc.append(isbn)

    needs_desc = list(dict.fromkeys(needs_desc))  # deduplicate, preserve order
    print(f"\n{len(needs_desc)} ISBNs missing descriptions — fetching from OL Works API...")

    # Phase 1: OL Works API
    filled_ol = 0

    def _worker(isbn):
        return isbn, _fetch_works_for_isbn(isbn)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_worker, isbn): isbn for isbn in needs_desc}
        for i, fut in enumerate(as_completed(futures), 1):
            isbn, desc = fut.result()
            if desc:
                if isbn in cache:
                    cache[isbn]["description"] = desc
                else:
                    cache[isbn] = {"description": desc}
                filled_ol += 1
            if i % 100 == 0:
                print(f"  OL Works: {i}/{len(needs_desc)}  filled={filled_ol}")
            time.sleep(CACHE_DELAY)

    print(f"OL Works filled {filled_ol}/{len(needs_desc)} descriptions.")

    # Phase 2: Claude for remaining
    if USE_CLAUDE:
        still_missing = [
            isbn for isbn in needs_desc
            if not cache.get(isbn, {}).get("description")
        ]
        print(f"\n{len(still_missing)} still missing — generating with Claude Haiku...")
        filled_claude = 0
        for i, isbn in enumerate(still_missing, 1):
            entry = cache.get(isbn, {})
            title   = entry.get("title", f"ISBN {isbn}")
            authors = entry.get("authors", "Unknown")
            cats    = entry.get("categories", [])
            desc = _generate_with_claude(isbn, title, authors, cats)
            if desc:
                cache.setdefault(isbn, {})["description"] = desc
                filled_claude += 1
            if i % 50 == 0:
                print(f"  Claude: {i}/{len(still_missing)}  filled={filled_claude}")
                # Save cache periodically
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
        print(f"Claude filled {filled_claude}/{len(still_missing)} descriptions.")

    # Save updated cache
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    print("\nCache updated.")

    # Phase 3: rewrite CSVs
    print("\nRewriting CSV files with updated descriptions...")
    total_patched = 0

    for csv_path in csv_files:
        patched = 0
        tmp_path = str(csv_path) + ".tmp"

        with open(csv_path, "r", encoding="utf-8-sig") as fin, \
             open(tmp_path, "w", newline="", encoding="utf-8-sig") as fout:

            reader = csv.DictReader(fin)
            writer = csv.DictWriter(fout, fieldnames=SHOPIFY_HEADERS)
            writer.writeheader()

            for row in reader:
                isbn = row.get("Variant SKU", "").strip()
                body = row.get("Body (HTML)", "")
                if isbn and not _has_description(body):
                    desc = cache.get(isbn, {}).get("description", "")
                    if desc:
                        row["Body (HTML)"] = _inject_description(body, desc)
                        patched += 1
                writer.writerow(row)

        # Replace original with patched
        os.replace(tmp_path, csv_path)
        print(f"  {csv_path.name}: {patched} rows patched")
        total_patched += patched

    print(f"\nDone! {total_patched} rows updated across {len(csv_files)} file(s).")


if __name__ == "__main__":
    main()
