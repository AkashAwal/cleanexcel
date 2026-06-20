import socket; socket.setdefaulttimeout(15)
import requests, time

HEADERS = {"User-Agent": "Mozilla/5.0"}
isbn = "9781907410352"

print("Testing Google Books...", flush=True)
try:
    r = requests.get(
        "https://www.googleapis.com/books/v1/volumes",
        params={"q": "isbn:" + isbn, "key": "AIzaSyDfPAlm8bm7kWPZZchDw97kBNK2bxirCh4"},
        headers=HEADERS, timeout=(5, 15)
    )
    items = r.json().get("items", [])
    print(f"  status={r.status_code} items={len(items)}", flush=True)
    if items:
        img = items[0].get("volumeInfo", {}).get("imageLinks", {})
        print(f"  imageLinks={img}", flush=True)
except Exception as e:
    print(f"  ERROR: {e}", flush=True)

print("Testing Open Library HEAD...", flush=True)
try:
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    r2 = requests.head(url, headers=HEADERS, timeout=(5, 5), allow_redirects=True)
    print(f"  status={r2.status_code} CL={r2.headers.get('Content-Length')}", flush=True)
except Exception as e:
    print(f"  ERROR: {e}", flush=True)

print("Done", flush=True)
