import time, openpyxl
print("Loading Excel...", flush=True)
t = time.time()
wb = openpyxl.load_workbook("common distributor stock IBD PBI 10.06.2026.xlsx", read_only=True, data_only=True)
ws = wb["Sheet1"]
rows = list(ws.iter_rows(min_row=2, values_only=True))
wb.close()
print(f"Done in {time.time()-t:.1f}s — {len(rows)} rows", flush=True)
