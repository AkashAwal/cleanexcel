"""Run enrichment on first 10 rows, output accepted + rejected CSVs."""
import sys
sys.modules.pop("enrich_books", None)

from enrich_books import (
    EXCEL_FILE, SHOPIFY_HEADERS, REJECTED_HEADERS, clean_text,
    fetch_open_library, verify_image, build_shopify_row, API_DELAY
)
import openpyxl, csv, time

OUTPUT   = "demo_10_shopify.csv"
REJECTED = "demo_10_rejected.csv"
LIMIT    = 10

wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)
ws = wb["Sheet1"]
rows = list(ws.iter_rows(min_row=2, max_row=LIMIT + 1, values_only=True))
wb.close()

with open(OUTPUT, "w", newline="", encoding="utf-8-sig") as mf, \
     open(REJECTED, "w", newline="", encoding="utf-8-sig") as rf:

    writer     = csv.DictWriter(mf, fieldnames=SHOPIFY_HEADERS)
    rej_writer = csv.DictWriter(rf, fieldnames=REJECTED_HEADERS)
    writer.writeheader()
    rej_writer.writeheader()

    accepted = rejected = 0

    for i, row in enumerate(rows):
        (isbn_raw, name, author, publisher,
         binding, language, currency, price, stock, location) = row
        if not isbn_raw:
            continue

        isbn = str(int(isbn_raw))
        excel = {
            "name": name or "", "AUTHOR1": author or "",
            "PUBLISHER": publisher or "", "BINDINGTYPE": binding or "",
            "LANGUAGE": language or "", "price": price or 0, "stock": stock or 0,
        }

        api = fetch_open_library(isbn)
        time.sleep(API_DELAY)
        img = verify_image(api.get("image", ""), isbn)
        row_data = build_shopify_row(isbn, excel, api, verified_image=img)

        if row_data["Image Src"]:
            writer.writerow(row_data)
            accepted += 1
            status = "YES"
        else:
            rej_writer.writerow({
                "ISBN": isbn, "Original Title": clean_text(name or ""),
                "Author": clean_text(author or ""), "Publisher": clean_text(publisher or ""),
                "Binding": clean_text(binding or ""), "Language": clean_text(language or ""),
                "Price": str(price or ""), "Stock": str(int(stock)) if stock else "0",
                "Reason": "No verified image on Open Library",
            })
            rejected += 1
            status = "NO"

        src = "OL" if api.get("title") else "Excel"
        print(f"[{i+1:02d}] img={status} | {src} | {row_data['Title'][:50]:<50} | ${row_data['Variant Price']}")

print(f"\nAccepted: {accepted} -> {OUTPUT}")
print(f"Rejected: {rejected} -> {REJECTED}")
