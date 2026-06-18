"""Quick test: fetch 3 ISBNs and print results."""
from enrich_books import fetch_open_library, fetch_google_books, merge_api_data
import time

test_isbns = ["9780552142397", "9788179921623", "9780749918354"]

for isbn in test_isbns:
    ol = fetch_open_library(isbn)
    time.sleep(0.2)
    gb = fetch_google_books(isbn)
    time.sleep(0.2)
    m = merge_api_data(ol, gb)
    print(f"ISBN: {isbn}")
    print(f"  Title:     {m['title']}")
    print(f"  Authors:   {m['authors']}")
    print(f"  Publisher: {m['publisher']}")
    print(f"  Desc:      {m['description'][:90] + '...' if m['description'] else '(none)'}")
    print(f"  Image:     {m['image'][:70] + '...' if m['image'] else '(none)'}")
    print()
