"""One-shot patch: inject 4 manually-found cover images into cache, rewrite CSV 001."""
import json, csv, os
from pathlib import Path

CACHE_FILE = "isbn_cache.json"
CSV_FILE   = "shopify_import_001.csv"

PATCHES = {
    "9789351033011": "https://books.google.com/books/content?id=2N1GBAAAQBAJ&printsec=frontcover&img=1&zoom=1",
    "9788184004427": "https://books.google.com/books/content?id=LdxABAAAQBAJ&printsec=frontcover&img=1&zoom=1",
    "9781447262794": "https://books.google.com/books/content?id=Hiu_Px_hedoC&printsec=frontcover&img=1&zoom=1",
    "9781447262817": "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1398194804i/21470857.jpg",
}

# 1. Update cache
with open(CACHE_FILE, "r", encoding="utf-8") as f:
    cache = json.load(f)

for isbn, url in PATCHES.items():
    cache.setdefault(isbn, {})["image"] = url
    print(f"  cache updated: {isbn} -> {url[:60]}...")

with open(CACHE_FILE, "w", encoding="utf-8") as f:
    json.dump(cache, f, ensure_ascii=False)
print(f"Cache saved ({len(cache)} entries).\n")

# 2. Rewrite CSV 001 — replace Image Src for these 4 ISBNs
tmp = CSV_FILE + ".tmp"
patched = 0

with open(CSV_FILE, "r", encoding="utf-8-sig") as fin, \
     open(tmp, "w", newline="", encoding="utf-8-sig") as fout:
    reader = csv.DictReader(fin)
    writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in reader:
        isbn = row.get("Variant SKU", "").strip()
        if isbn in PATCHES:
            current = row.get("Image Src", "")
            if not current or "covers.openlibrary.org" in current:
                row["Image Src"]      = PATCHES[isbn]
                row["Image Position"] = "1"
                patched += 1
                print(f"  patched CSV: {isbn}")
        writer.writerow(row)

os.replace(tmp, CSV_FILE)
print(f"\nDone — {patched} rows updated in {CSV_FILE}.")
