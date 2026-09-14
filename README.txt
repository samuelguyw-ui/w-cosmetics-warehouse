W Cosmetics Warehouse V28.1 - Product Name Repair

IMPORTANT: Copy your existing warehouse.db and uploads folder into this folder before starting if you want to keep existing orders.

Run:
  venv\Scripts\activate
  python -m uvicorn app:app --host 0.0.0.0 --port 8000

PDA: http://192.168.1.90:8000
Admin: http://127.0.0.1:8000/admin

Product Name handling:
- Reads store-specific columns such as `name Manukau`, `name St_Lukes`, etc.
- Stores Product Name in order_lines at upload time.
- Keeps a readable copy of the original Excel filename in uploads.
- Repairs old missing Product Names automatically when the Picking page opens.
- SKU matching handles Excel numeric .0 formatting.
