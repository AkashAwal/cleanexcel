"""Patch 5 fake-placeholder OL images with real Goodreads covers."""
import json, csv, os, sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CACHE_FILE = "isbn_cache.json"
CSV_FILE   = "shopify_import_001.csv"

PATCHES = {
    "9788172341367": "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1561495703l/50205431.jpg",
    "9780545103695": "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1328838986i/6882181.jpg",
    "9781904233916": "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1394296634i/2650712.jpg",
    "9781447210948": "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1350395582i/16087499.jpg",
    "9781421518442": "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1348842455i/1997956.jpg",
}

def is_fake(url):
    return not url or "covers.openlibrary.org/b/isbn/" in url or "covers.openlibrary.org/b/id/-1-" in url

# 1. Update cache
with open(CACHE_FILE, "r", encoding="utf-8") as f:
    cache = json.load(f)

for isbn, url in PATCHES.items():
    cache.setdefault(isbn, {})["image"] = url
    print(f"cache  {isbn} -> {url[:65]}...")

with open(CACHE_FILE, "w", encoding="utf-8") as f:
    json.dump(cache, f, ensure_ascii=False)
print(f"Cache saved ({len(cache)} entries).\n")

# 2. Rewrite CSV 001
tmp = CSV_FILE + ".tmp"
patched = 0

with open(CSV_FILE, "r", encoding="utf-8-sig") as fin, \
     open(tmp, "w", newline="", encoding="utf-8-sig") as fout:
    reader = csv.DictReader(fin)
    writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
    writer.writeheader()
    for row in reader:
        isbn = row.get("Variant SKU", "").strip()
        if isbn in PATCHES and is_fake(row.get("Image Src", "")):
            row["Image Src"]      = PATCHES[isbn]
            row["Image Position"] = "1"
            patched += 1
            print(f"patched {isbn}")
        writer.writerow(row)

os.replace(tmp, CSV_FILE)
print(f"\nDone -- {patched} rows updated in {CSV_FILE}.")
