"""
Shopify Book Enrichment Script
Reads Excel → fetches data via ISBN APIs → outputs Shopify import CSV
"""

import openpyxl
import csv
import requests
import time
import json
import os
import re
import unicodedata
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
EXCEL_FILE      = "common distributor stock IBD PBI 10.06.2026.xlsx"
OUTPUT_CSV      = "shopify_import.csv"
REJECTED_CSV    = "rejected_no_image.csv"
CACHE_FILE      = "isbn_cache.json"
PROGRESS_FILE   = "progress.json"

# Split accepted output into chunks of this many rows (Shopify recommends ≤5000).
# Set to 0 to disable chunking (single file).
CHUNK_SIZE      = 2000

REJECTED_HEADERS = [
    "ISBN", "Original Title", "Author", "Publisher",
    "Binding", "Language", "Price", "Stock", "Reason"
]

# REQUIRED for 74k books — get a free key:
#   1. Go to https://console.cloud.google.com/
#   2. Create a project → Enable "Books API"
#   3. Credentials → Create API Key → paste it below
GOOGLE_BOOKS_API_KEY = "AIzaSyDfPAlm8bm7kWPZZchDw97kBNK2bxirCh4"

# Delay between API calls (seconds).
API_DELAY = 0.2

# Shared headers — Open Library blocks requests without a User-Agent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
# ─────────────────────────────────────────────────────────────────────────────

SHOPIFY_HEADERS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type",
    "Tags", "Published",
    "Option1 Name", "Option1 Value",
    "Variant SKU", "Variant Inventory Tracker", "Variant Inventory Qty",
    "Variant Inventory Policy", "Variant Fulfillment Service",
    "Variant Price", "Variant Compare At Price",
    "Variant Requires Shipping", "Variant Taxable",
    "Variant Barcode",
    "Image Src", "Image Position", "Image Alt Text",
    "Status",
]


def clean_text(text: str) -> str:
    """Normalize unicode, strip garbage/control characters."""
    if not text:
        return ""
    text = str(text)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def make_handle(title: str, isbn: str) -> str:
    handle = title.lower()
    handle = re.sub(r"[^a-z0-9]+", "-", handle).strip("-")
    return f"{handle}-{isbn}"[:255]


def _get_with_retry(url: str, params: dict = None) -> requests.Response | None:
    """GET with up to 3 retries and exponential backoff."""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"    Rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            return r
        except Exception as e:
            if attempt == 2:
                return None
            time.sleep(2 ** attempt)
    return None


def fetch_open_library(isbn: str) -> dict:
    # Only used for cover image fallback — no multi-hop API chains.
    # Returns just an image URL constructed directly from the ISBN.
    return {
        "image": f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    }


def fetch_google_books(isbn: str) -> dict:
    if not GOOGLE_BOOKS_API_KEY:
        return {}  # skip to avoid 429s without a key
    params = {"q": f"isbn:{isbn}", "key": GOOGLE_BOOKS_API_KEY}
    try:
        r = _get_with_retry("https://www.googleapis.com/books/v1/volumes", params)
        if not r or r.status_code != 200:
            return {}
        items = r.json().get("items", [])
        if not items:
            return {}

        info = items[0].get("volumeInfo", {})
        title = clean_text(info.get("title", ""))
        subtitle = clean_text(info.get("subtitle", ""))
        if subtitle:
            title = f"{title}: {subtitle}"
        authors = ", ".join(clean_text(a) for a in info.get("authors", []))
        publisher = clean_text(info.get("publisher", ""))
        description = clean_text(info.get("description", ""))

        img = info.get("imageLinks", {})
        image = (
            img.get("extraLarge")
            or img.get("large")
            or img.get("medium")
            or img.get("thumbnail", "")
        )
        if image:
            image = image.replace("http://", "https://")
            image = re.sub(r"&edge=curl", "", image)    # removes curl effect
            image = re.sub(r"&zoom=\d+", "&zoom=6", image)  # zoom=6 = highest quality (~800x1200px)

        categories = info.get("categories", [])
        page_count = info.get("pageCount", "")
        published_date = info.get("publishedDate", "")
        language_code = info.get("language", "")
        preview_link = info.get("previewLink", "").replace("http://", "https://")
        rating = info.get("averageRating", "")
        ratings_count = info.get("ratingsCount", "")

        return {
            "title": title,
            "authors": authors,
            "publisher": publisher,
            "description": description,
            "image": image,
            "categories": categories,
            "page_count": str(page_count) if page_count else "",
            "published_date": published_date,
            "language_code": language_code,
            "preview_link": preview_link,
            "rating": str(rating) if rating else "",
            "ratings_count": str(ratings_count) if ratings_count else "",
        }
    except Exception:
        return {}


def merge_api_data(ol: dict, gb: dict) -> dict:
    """Google Books is primary; Open Library fills any gaps."""
    merged = {}
    all_keys = ("title", "authors", "publisher", "description", "image",
                "categories", "page_count", "published_date", "language_code",
                "preview_link", "rating", "ratings_count")
    for key in all_keys:
        merged[key] = gb.get(key) or ol.get(key) or ""
    return merged


def lookup_isbn(isbn: str) -> dict:
    # Google Books is primary (Open Library is blocked on many South Asian networks)
    gb = fetch_google_books(isbn)
    time.sleep(API_DELAY)
    ol = fetch_open_library(isbn)  # fallback — skipped if unreachable
    time.sleep(API_DELAY)
    return merge_api_data(ol, gb)


def _image_is_real(url: str, min_bytes: int = 5120) -> bool:
    """Return True if URL serves a real image (> min_bytes content)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, stream=True)
        if r.status_code != 200:
            return False
        size = 0
        for chunk in r.iter_content(chunk_size=1024):
            size += len(chunk)
            if size >= min_bytes:
                r.close()
                return True
        r.close()
        return False
    except Exception:
        return False


def open_library_cover_url(isbn: str) -> str:
    """Return Open Library cover URL only if a real image exists there."""
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    if _image_is_real(url, min_bytes=2048):
        return url
    return ""


def best_image_url(base_google_url: str, isbn: str) -> str:
    """Priority: Google zoom=6 → Open Library → Google zoom=3 → Google zoom=1."""
    base = re.sub(r"&zoom=\d+", "", base_google_url)

    # 1. Google Books best quality
    url = f"{base}&zoom=6"
    if _image_is_real(url):
        return url

    # 2. Open Library
    ol = open_library_cover_url(isbn)
    if ol:
        return ol

    # 3 & 4. Google Books fallback zoom levels
    for zoom in (3, 1):
        url = f"{base}&zoom={zoom}"
        if _image_is_real(url):
            return url

    return ""


def build_shopify_row(isbn: str, excel: dict, api: dict, verified_image: str = "") -> dict:
    title        = clean_text(api.get("title")        or excel["name"])
    authors      = clean_text(api.get("authors")      or excel["AUTHOR1"])
    publisher    = clean_text(api.get("publisher")    or excel["PUBLISHER"])
    desc         = clean_text(api.get("description",  ""))
    image        = verified_image
    categories   = api.get("categories", [])
    page_count   = api.get("page_count", "")
    pub_date     = api.get("published_date", "")
    preview_link = api.get("preview_link", "")
    rating       = api.get("rating", "")
    price        = excel["price"]
    stock        = excel["stock"]
    binding      = clean_text(excel["BINDINGTYPE"])
    language     = clean_text(excel["LANGUAGE"])

    # Rich HTML description
    parts = []
    if desc:
        parts.append(f"<p>{desc}</p>")
    meta_rows = []
    if authors:
        meta_rows.append(f"<tr><td><strong>Author(s)</strong></td><td>{authors}</td></tr>")
    if publisher:
        meta_rows.append(f"<tr><td><strong>Publisher</strong></td><td>{publisher}</td></tr>")
    if pub_date:
        meta_rows.append(f"<tr><td><strong>Published</strong></td><td>{pub_date}</td></tr>")
    if page_count:
        meta_rows.append(f"<tr><td><strong>Pages</strong></td><td>{page_count}</td></tr>")
    if binding:
        meta_rows.append(f"<tr><td><strong>Format</strong></td><td>{binding.title()}</td></tr>")
    if language:
        meta_rows.append(f"<tr><td><strong>Language</strong></td><td>{language.title()}</td></tr>")
    meta_rows.append(f"<tr><td><strong>ISBN</strong></td><td>{isbn}</td></tr>")
    if rating:
        meta_rows.append(f"<tr><td><strong>Rating</strong></td><td>{rating} / 5</td></tr>")
    if preview_link:
        meta_rows.append(f"<tr><td><strong>Preview</strong></td><td><a href='{preview_link}' target='_blank'>Google Books Preview</a></td></tr>")
    if meta_rows:
        parts.append("<table>" + "".join(meta_rows) + "</table>")
    body_html = "\n".join(parts)

    # Tags: language, binding, authors, genres
    tags = []
    if language:
        tags.append(language.title())
    if binding:
        tags.append(binding.title())
    for author in (authors or "").split(","):
        a = author.strip()
        if a:
            tags.append(a)
    if isinstance(categories, list):
        for cat in categories:
            for part in cat.split("/"):
                p = part.strip()
                if p:
                    tags.append(p)

    handle = make_handle(title, isbn)

    return {
        "Handle":                       handle,
        "Title":                        title,
        "Body (HTML)":                  body_html,
        "Vendor":                       publisher,
        "Product Category":             "Media > Books",
        "Type":                         "Books",
        "Tags":                         ", ".join(tags),
        "Published":                    "TRUE",
        "Option1 Name":                 "Title",
        "Option1 Value":                "Default Title",
        "Variant SKU":                  isbn,
        "Variant Inventory Tracker":    "shopify",
        "Variant Inventory Qty":        str(int(stock)) if stock else "0",
        "Variant Inventory Policy":     "deny",
        "Variant Fulfillment Service":  "manual",
        "Variant Price":                str(price) if price else "0",
        "Variant Compare At Price":     "",
        "Variant Requires Shipping":    "TRUE",
        "Variant Taxable":              "FALSE",
        "Variant Barcode":              isbn,
        "Image Src":                    image,
        "Image Position":               "1" if image else "",
        "Image Alt Text":               title,
        "Status":                       "active",
    }


def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def save_progress(row_index: int):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"last_row": row_index}, f)


def _chunk_path(n: int) -> str:
    """Return output path for chunk n (1-based). Uses single file when chunking off."""
    base = Path(OUTPUT_CSV)
    return str(base.with_stem(f"{base.stem}_{n:03d}"))


def main():
    # Load persisted cache
    cache: dict = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"Loaded {len(cache)} cached ISBNs.")

    # Resume support
    start_row = 0
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            prog = json.load(f)
            start_row = prog.get("last_row", 0)
        print(f"Resuming from row {start_row}.")

    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    total = len(rows)
    print(f"Total book rows: {total}")
    if CHUNK_SIZE:
        print(f"Chunking output every {CHUNK_SIZE} accepted rows.")

    chunking = CHUNK_SIZE > 0
    accepted = rejected = 0
    chunk_num = 1
    rows_in_chunk = 0

    def _open_chunk(n: int, append: bool):
        path = _chunk_path(n) if chunking else OUTPUT_CSV
        mode = "a" if append else "w"
        f = open(path, mode, newline="", encoding="utf-8-sig")
        w = csv.DictWriter(f, fieldnames=SHOPIFY_HEADERS)
        if not append:
            w.writeheader()
        return f, w

    def _count_csv_rows(path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return sum(1 for _ in f) - 1  # subtract header
        except FileNotFoundError:
            return 0

    # When resuming, append to the last chunk; otherwise start fresh.
    resuming = start_row > 0
    if resuming and chunking:
        existing = sorted(Path(".").glob(_chunk_path(0).replace("000", "*").lstrip("./")))
        chunk_num = max(len(existing), 1)
        rows_in_chunk = _count_csv_rows(str(existing[-1])) if existing else 0

    main_f, writer = _open_chunk(chunk_num, append=resuming)

    rej_mode = "a" if resuming else "w"
    rej_f = open(REJECTED_CSV, rej_mode, newline="", encoding="utf-8-sig")
    rej_writer = csv.DictWriter(rej_f, fieldnames=REJECTED_HEADERS)
    if not resuming:
        rej_writer.writeheader()

    chunk_paths = [_chunk_path(chunk_num) if chunking else OUTPUT_CSV]
    last_completed_row = start_row

    try:
        for i, row in enumerate(rows):
            if i < start_row:
                continue

            (isbn_raw, name, author, publisher,
             binding, language, currency, price, stock, location) = row

            if not isbn_raw:
                continue

            isbn = str(int(isbn_raw))

            excel = {
                "name":        name or "",
                "AUTHOR1":     author or "",
                "PUBLISHER":   publisher or "",
                "BINDINGTYPE": binding or "",
                "LANGUAGE":    language or "",
                "price":       price or 0,
                "stock":       stock or 0,
            }

            if isbn not in cache:
                api_data = lookup_isbn(isbn)
                cache[isbn] = api_data
            else:
                api_data = cache[isbn]

            # Verify image once and persist result so subsequent runs skip HTTP checks
            if "verified_image" not in api_data:
                raw = api_data.get("image", "")
                api_data["verified_image"] = best_image_url(raw, isbn) if raw else open_library_cover_url(isbn)
                cache[isbn] = api_data

            shopify_row = build_shopify_row(isbn, excel, api_data, verified_image=api_data["verified_image"])

            if shopify_row["Image Src"]:
                # Roll over to a new chunk if needed
                if chunking and rows_in_chunk >= CHUNK_SIZE:
                    main_f.close()
                    chunk_num += 1
                    rows_in_chunk = 0
                    main_f, writer = _open_chunk(chunk_num, append=False)
                    chunk_paths.append(_chunk_path(chunk_num))

                writer.writerow(shopify_row)
                accepted += 1
                rows_in_chunk += 1
            else:
                rej_writer.writerow({
                    "ISBN":           isbn,
                    "Original Title": clean_text(name or ""),
                    "Author":         clean_text(author or ""),
                    "Publisher":      clean_text(publisher or ""),
                    "Binding":        clean_text(binding or ""),
                    "Language":       clean_text(language or ""),
                    "Price":          str(price or ""),
                    "Stock":          str(int(stock)) if stock else "0",
                    "Reason":         "No verified image found (Google Books + Open Library both failed)",
                })
                rejected += 1

            last_completed_row = i + 1

            # Checkpoint every 200 rows
            if last_completed_row % 200 == 0:
                save_cache(cache)
                save_progress(last_completed_row)
                pct = last_completed_row / total * 100
                print(f"  {last_completed_row}/{total} ({pct:.1f}%)  accepted={accepted}  rejected={rejected}")

    finally:
        main_f.close()
        rej_f.close()
        save_cache(cache)
        save_progress(last_completed_row)  # saves actual position, not total, on crash/interrupt

    print(f"\nDone!")
    if chunking:
        print(f"  ✅ Imported:  {accepted} books → {len(chunk_paths)} chunk(s):")
        for p in chunk_paths:
            print(f"       {p}")
    else:
        print(f"  ✅ Imported:  {accepted} books → {OUTPUT_CSV}")
    print(f"  ❌ Rejected:  {rejected} books → {REJECTED_CSV} (no verified image)")
    hits = sum(1 for v in cache.values() if v.get("title"))
    print(f"  API hits: {hits} / {len(cache)}")


if __name__ == "__main__":
    main()
