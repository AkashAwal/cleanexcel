"""
Shopify Book Enrichment Script
Reads Excel → fetches data via Open Library API → outputs Shopify import CSV
"""

import socket
socket.setdefaulttimeout(15)

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
EXCEL_FILE    = "common distributor stock IBD PBI 10.06.2026.xlsx"
OUTPUT_CSV    = "shopify_import.csv"
REJECTED_CSV  = "rejected_no_image.csv"
CACHE_FILE    = "isbn_cache.json"
PROGRESS_FILE = "progress.json"

# Split accepted output into chunks (Shopify recommends ≤5000). 0 = single file.
CHUNK_SIZE    = 2000

# Delay between API calls (seconds) — be respectful to Open Library.
API_DELAY     = 0.25

REJECTED_HEADERS = [
    "ISBN", "Original Title", "Author", "Publisher",
    "Binding", "Language", "Price", "Stock", "Reason"
]

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
    if not text:
        return ""
    text = str(text)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def make_handle(title: str, isbn: str) -> str:
    handle = title.lower()
    handle = re.sub(r"[^a-z0-9]+", "-", handle).strip("-")
    return f"{handle}-{isbn}"[:255]


def _fetch_works_description(works_key: str) -> str:
    """Fetch description from Open Library Works API."""
    try:
        r = requests.get(f"https://openlibrary.org{works_key}.json", headers=HEADERS, timeout=(5, 15))
        if r.status_code != 200:
            return ""
        data = r.json()
        desc = data.get("description", "")
        if isinstance(desc, dict):
            return clean_text(desc.get("value", ""))
        return clean_text(desc)
    except Exception:
        return ""


def fetch_open_library(isbn: str) -> dict:
    """Fetch full book data from Open Library Books API, with Works API fallback for descriptions."""
    try:
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&jscmd=data&format=json"
        r = requests.get(url, headers=HEADERS, timeout=(5, 15))
        if r.status_code != 200:
            return {}

        data = r.json().get(f"ISBN:{isbn}", {})
        if not data:
            return {}

        title = clean_text(data.get("title", ""))
        subtitle = clean_text(data.get("subtitle", ""))
        if subtitle:
            title = f"{title}: {subtitle}"

        authors = ", ".join(clean_text(a.get("name", "")) for a in data.get("authors", []))
        publisher = ", ".join(clean_text(p.get("name", "")) for p in data.get("publishers", []))

        # Description: try "notes" first, then Works API
        description = ""
        notes = data.get("notes", "")
        if isinstance(notes, dict):
            description = clean_text(notes.get("value", ""))
        elif isinstance(notes, str):
            description = clean_text(notes)

        if not description:
            works = data.get("works", [])
            if works:
                works_key = works[0].get("key", "")
                if works_key:
                    description = _fetch_works_description(works_key)

        # Cover: prefer large from API data, fall back to ISBN-based URL
        cover = data.get("cover", {})
        image = (
            cover.get("large")
            or cover.get("medium")
            or cover.get("small")
            or f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
        )

        categories = [clean_text(s.get("name", "")) for s in data.get("subjects", [])][:10]
        page_count = str(data.get("number_of_pages", "")) if data.get("number_of_pages") else ""
        published_date = clean_text(data.get("publish_date", ""))

        return {
            "title":          title,
            "authors":        authors,
            "publisher":      publisher,
            "description":    description,
            "image":          image,
            "categories":     categories,
            "page_count":     page_count,
            "published_date": published_date,
            "preview_link":   f"https://openlibrary.org/isbn/{isbn}",
            "rating":         "",
            "ratings_count":  "",
        }
    except Exception:
        return {}


def lookup_isbn(isbn: str) -> dict:
    data = fetch_open_library(isbn)
    time.sleep(API_DELAY)
    return data


def _image_is_real(url: str, min_bytes: int = 2048) -> bool:
    """Return True if URL serves a real image (> min_bytes)."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=(5, 5), allow_redirects=True)
        if r.status_code == 200:
            cl = int(r.headers.get("Content-Length", 0))
            if cl >= min_bytes:
                return True
            if cl > 0:
                return False
        r = requests.get(url, headers=HEADERS, timeout=(5, 10))
        if r.status_code != 200:
            return False
        return len(r.content) >= min_bytes
    except Exception:
        return False


def verify_image(api_image: str, isbn: str) -> str:
    """Return image URL — OL API cover if available, else ISBN-based fallback."""
    return api_image or f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"


def build_shopify_row(isbn: str, excel: dict, api: dict, verified_image: str = "") -> dict:
    title        = clean_text(api.get("title")       or excel["name"])
    authors      = clean_text(api.get("authors")     or excel["AUTHOR1"])
    publisher    = clean_text(api.get("publisher")   or excel["PUBLISHER"])
    desc         = clean_text(api.get("description", ""))
    categories   = api.get("categories", [])
    page_count   = api.get("page_count", "")
    pub_date     = api.get("published_date", "")
    preview_link = api.get("preview_link", "")
    price        = excel["price"]
    stock        = excel["stock"]
    binding      = clean_text(excel["BINDINGTYPE"])
    language     = clean_text(excel["LANGUAGE"])

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
    if preview_link:
        meta_rows.append(f"<tr><td><strong>More Info</strong></td><td><a href='{preview_link}' target='_blank'>Open Library</a></td></tr>")
    if meta_rows:
        parts.append("<table>" + "".join(meta_rows) + "</table>")
    body_html = "\n".join(parts)

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
        "Image Src":                    verified_image,
        "Image Position":               "1" if verified_image else "",
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
    base = Path(OUTPUT_CSV)
    return str(base.with_stem(f"{base.stem}_{n:03d}"))


def main():
    cache: dict = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"Loaded {len(cache)} cached ISBNs.", flush=True)

    start_row = 0
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            prog = json.load(f)
            start_row = prog.get("last_row", 0)
        print(f"Resuming from row {start_row}.", flush=True)

    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()
    total = len(rows)
    print(f"Total book rows: {total}", flush=True)
    if CHUNK_SIZE:
        print(f"Chunking output every {CHUNK_SIZE} accepted rows.", flush=True)

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
                return sum(1 for _ in f) - 1
        except FileNotFoundError:
            return 0

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

            try:
                if isbn not in cache:
                    api_data = lookup_isbn(isbn)
                    cache[isbn] = api_data
                else:
                    api_data = cache[isbn]

                if "verified_image" not in api_data:
                    api_data["verified_image"] = verify_image(api_data.get("image", ""), isbn)
                    cache[isbn] = api_data

                shopify_row = build_shopify_row(isbn, excel, api_data, verified_image=api_data["verified_image"])
            except Exception as e:
                print(f"  [SKIP] ISBN {isbn} row {i+1}: {e}", flush=True)
                rej_writer.writerow({
                    "ISBN":           isbn,
                    "Original Title": clean_text(name or ""),
                    "Author":         clean_text(author or ""),
                    "Publisher":      clean_text(publisher or ""),
                    "Binding":        clean_text(binding or ""),
                    "Language":       clean_text(language or ""),
                    "Price":          str(price or ""),
                    "Stock":          str(int(stock)) if stock else "0",
                    "Reason":         f"Processing error: {e}",
                })
                rejected += 1
                last_completed_row = i + 1
                continue

            if chunking and rows_in_chunk >= CHUNK_SIZE:
                main_f.close()
                chunk_num += 1
                rows_in_chunk = 0
                main_f, writer = _open_chunk(chunk_num, append=False)
                chunk_paths.append(_chunk_path(chunk_num))

            writer.writerow(shopify_row)
            accepted += 1
            rows_in_chunk += 1

            last_completed_row = i + 1

            if last_completed_row % 200 == 0:
                save_cache(cache)
                save_progress(last_completed_row)
                pct = last_completed_row / total * 100
                print(f"  {last_completed_row}/{total} ({pct:.1f}%)  accepted={accepted}  rejected={rejected}", flush=True)

    finally:
        main_f.close()
        rej_f.close()
        save_cache(cache)
        save_progress(last_completed_row)

    print(f"\nDone!", flush=True)
    if chunking:
        print(f"  Imported:  {accepted} books → {len(chunk_paths)} chunk(s):", flush=True)
        for p in chunk_paths:
            print(f"       {p}", flush=True)
    else:
        print(f"  Imported:  {accepted} books → {OUTPUT_CSV}", flush=True)
    print(f"  Rejected:  {rejected} books → {REJECTED_CSV} (no verified image)", flush=True)
    hits = sum(1 for v in cache.values() if v.get("title"))
    print(f"  OL hits with title: {hits} / {len(cache)}", flush=True)


if __name__ == "__main__":
    main()
