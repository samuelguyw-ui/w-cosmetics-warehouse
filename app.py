from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
import html
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime, timezone
import sqlite3, secrets, hashlib, shutil, uuid, json
import pandas as pd

BASE = Path(__file__).resolve().parent
DB = BASE / "warehouse.db"
UPLOADS = BASE / "uploads"
EXPORTS = BASE / "exports"
UPLOADS.mkdir(exist_ok=True)
EXPORTS.mkdir(exist_ok=True)

app = FastAPI(title="W Cosmetics Warehouse V24")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

NZ_STORES = ["Newmarket", "Sylvia Park", "Manukau", "Riccarton", "202 Queen St", "St Lukes", "Botany"]

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS employees(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        area TEXT NOT NULL,
        role TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stores(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        area TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(name, area)
    );

    CREATE TABLE IF NOT EXISTS orders(
        id TEXT PRIMARY KEY,
        order_no TEXT NOT NULL,
        filename TEXT,
        area TEXT NOT NULL,
        store TEXT NOT NULL,
        status TEXT NOT NULL,
        assigned_to TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS order_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        sku TEXT NOT NULL,
        product_name TEXT,
        required INTEGER NOT NULL,
        soh TEXT,
        bin TEXT,
        picked INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT,
        line_id INTEGER,
        employee_id TEXT,
        event_type TEXT,
        qty INTEGER,
        value TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS product_master(
        sku TEXT PRIMARY KEY,
        product_name TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """)
    # Shortage re-entry fields. Kept as migrations so existing V29 databases continue to work.
    existing_cols={r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
    if "is_shortage" not in existing_cols:
        c.execute("ALTER TABLE orders ADD COLUMN is_shortage INTEGER NOT NULL DEFAULT 0")
    if "source_order_id" not in existing_cols:
        c.execute("ALTER TABLE orders ADD COLUMN source_order_id TEXT")
    if "source_order_no" not in existing_cols:
        c.execute("ALTER TABLE orders ADD COLUMN source_order_no TEXT")
    for s in NZ_STORES:
        c.execute("INSERT OR IGNORE INTO stores(name,area) VALUES(?,?)", (s, "NZ"))
    if not c.execute("SELECT 1 FROM employees WHERE id='ADMIN'").fetchone():
        c.execute(
            "INSERT INTO employees VALUES(?,?,?,?,?,?,?)",
            ("ADMIN", "Administrator", password_hash("admin123"), "ALL", "SUPERADMIN", 1, now())
        )
    else:
        # V23: the original ADMIN account is the global administrator.
        c.execute("UPDATE employees SET role='SUPERADMIN', area='ALL' WHERE id='ADMIN' AND role='ADMIN' AND area='ALL'")
    c.commit()
    c.close()

init_db()

def current_user(request: Request):
    # DT50X fallback: also accept the session token in the URL query string.
    # Some older Android browsers do not reliably persist cookies between
    # navigations, so every server-side PDA page can continue the same session.
    token = request.cookies.get("session")
    if not token:
        token = request.query_params.get("token")
    if not token:
        auth = request.headers.get("Authorization","")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        return None
    c = db()
    u = c.execute("""
        SELECT e.* FROM employees e
        JOIN sessions s ON s.employee_id=e.id
        WHERE s.token=? AND e.active=1
    """, (token,)).fetchone()
    c.close()
    return u

def session_token(request: Request):
    return request.cookies.get("session") or request.query_params.get("token") or (
        request.headers.get("Authorization","")[7:].strip()
        if request.headers.get("Authorization","").lower().startswith("bearer ") else ""
    )

def redirect_with_token(path: str, token: str):
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path + sep + "token=" + token, status_code=303)

def require_user(request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Login required")
    return u

def require_admin(request):
    u = require_user(request)
    if u["role"] not in ("ADMIN", "SUPERADMIN"):
        raise HTTPException(403, "Admin only")
    return u

def is_global_admin(u):
    return u["role"] == "SUPERADMIN" or (u["role"] == "ADMIN" and str(u["area"]).upper() == "ALL")

def admin_can_access_area(u, area):
    return is_global_admin(u) or str(u["area"]).upper() == str(area).upper()

def normalize(x):
    return str(x).strip().lower().replace("_","").replace(" ","").replace("-","")

def parse_excel(path: Path):
    raw = pd.read_excel(path, header=None)
    if raw.shape[1] < 2:
        raise ValueError("Excel B1 store name was not found.")

    # Some of the existing W Cosmetics files put the dynamic name-column
    # label in B1, for example: "name St_Lukes".
    # Convert that to the actual store name before matching the Store table.
    raw_store = str(raw.iloc[0,1]).strip()
    store = raw_store
    if normalize(store).startswith("name"):
        store = store[4:].strip(" _-:")

    df = pd.read_excel(path)
    df.columns = [str(x).strip() for x in df.columns]

    sku_col = next((x for x in df.columns if normalize(x) in {"sku","productcode","code","pbcode"}), None)
    if not sku_col:
        raise ValueError("SKU column not found.")

    qty_col = next((x for x in df.columns if normalize(x).startswith("reorderquantity")), None)
    if not qty_col:
        qty_col = next((x for x in df.columns if normalize(x) in {"qty","quantity","required"}), None)
    if not qty_col:
        raise ValueError("Quantity column not found.")

    # Product name detection. Prefer the store-specific W Cosmetics column,
    # then common product-name/description columns. This is deliberately broad
    # because exports have used several header spellings over time.
    nstore = normalize(store)
    name_candidates = [x for x in df.columns if nstore and nstore in normalize(x) and ("name" in normalize(x) or "product" in normalize(x))]
    name_col = name_candidates[0] if name_candidates else None
    if not name_col:
        name_col = next((x for x in df.columns if normalize(x) in {"name","productname","product","description","productdescription","itemname","skuname","skuname","title","desc"}), None)
    if not name_col:
        name_col = next((x for x in df.columns if ("product" in normalize(x) and "name" in normalize(x)) or "description" in normalize(x)), None)

    soh_col = next((x for x in df.columns if normalize(x) == "soh"), None)
    bin_col = next((x for x in df.columns if normalize(x) == "bin"), None)
    # W Cosmetics files commonly put the product name immediately after SKU.
    # If the header spelling is unusual, use the first sensible non-numeric text
    # column that is not quantity/SOH/BIN. This prevents Product Name being lost.
    if not name_col:
        excluded={sku_col,qty_col,soh_col,bin_col}
        for col in df.columns:
            if col in excluded: continue
            vals=df[col].dropna().astype(str).str.strip()
            if len(vals) and vals.ne("").any():
                sample=vals[vals!=""].head(20)
                if any(len(v)>2 and not v.replace(".","",1).isdigit() for v in sample):
                    name_col=col; break

    rows = []
    for _, r in df.iterrows():
        sku = str(r.get(sku_col, "")).strip()
        if not sku or sku.lower() == "nan":
            continue
        try:
            qty = int(float(r.get(qty_col, 0)))
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        product_name = "" if not name_col else str(r.get(name_col, "" )).strip()
        if product_name.lower() in {"nan", "none", "null", "-"}: product_name = ""
        # Last-resort fallback for files where the store-specific name header
        # is missing but a description/title column contains the product name.
        if not product_name:
            fallback_cols=[]
            for col in df.columns:
                nc=normalize(col)
                if col in {sku_col,qty_col,soh_col,bin_col}: continue
                if any(k in nc for k in ("description","productname","itemname","title","desc","product")):
                    fallback_cols.append(col)
            for col in fallback_cols:
                v=str(r.get(col,"" )).strip()
                if v and v.lower() not in {"nan","none","null","-"}:
                    product_name=v; break
        soh = "" if not soh_col else str(r.get(soh_col, ""))
        if soh.lower() in {"nan", "none", "null"}: soh = ""
        bin_code = "" if not bin_col else str(r.get(bin_col, ""))
        if bin_code.lower() in {"nan", "none", "null"}: bin_code = ""
        rows.append((sku, product_name, qty, soh, bin_code))
    if not rows:
        raise ValueError("No positive quantity picking lines found.")
    return store, rows

@app.get("/", response_class=HTMLResponse)
def pda_page(request: Request):
    u = current_user(request)
    token = session_token(request)
    if not u:
        return HTMLResponse((BASE/"templates/pda.html").read_text(encoding="utf-8"))
    c=db()
    if is_global_admin(u):
        stores = c.execute("SELECT * FROM stores WHERE active=1 ORDER BY area,name").fetchall()
    else:
        stores = c.execute("SELECT * FROM stores WHERE active=1 AND area=? ORDER BY name", (u["area"],)).fetchall()
    c.close()
    opts=''.join(f'<option value="{x["id"]}">{html.escape(str(x["name"]))}</option>' for x in stores)
    area = "ALL" if is_global_admin(u) else str(u["area"])
    page=f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>W Cosmetics PDA V21</title><link rel="stylesheet" href="/static/style.css"></head><body><main>
<header><b>W COSMETICS</b><small>PDA PICKING · V26 ONLINE</small></header>
<div class="card"><h2>{html.escape(str(u["name"]))} · {html.escape(str(u["id"]))}</h2><div class="muted">AREA: {html.escape(area)}</div></div>
<div class="card"><h2>SELECT STORE</h2>
<form action="/pda/store" method="get"><input type="hidden" name="token" value="{html.escape(token,quote=True)}"><select name="store_id" required style="width:100%;padding:16px;font-size:20px;border-radius:10px;border:1px solid #bbb;background:white"><option value="">-- Select Store --</option>{opts}</select><button type="submit">CONTINUE</button></form></div>
</main></body></html>"""
    return HTMLResponse(page)

@app.get("/pda/", response_class=HTMLResponse)
def pda_alias(request: Request):
    tok = session_token(request)
    return RedirectResponse("/?token=" + tok if tok else "/", status_code=303)

def admin_shell(template_name, request):
    u=current_user(request)
    if not u or u["role"] not in ("ADMIN","SUPERADMIN"):
        return None, u
    template=(BASE/"templates"/template_name).read_text(encoding="utf-8")
    role_label="GLOBAL ADMIN · NZ + AU" if is_global_admin(u) else f"AREA ADMIN · {html.escape(str(u['area']))}"
    areas=["NZ","AU"] if is_global_admin(u) else [str(u["area"]).upper()]
    area_opts="".join(f'<option value="{a}">{a} AREA</option>' for a in areas)
    roles=["PICKER","MANAGER","ADMIN"] + (["SUPERADMIN"] if is_global_admin(u) else [])
    role_opts="".join(f'<option value="{r}">{r}</option>' for r in roles)
    replacements={"__ADMIN_NAME__":html.escape(str(u["name"])),"__ADMIN_ID__":html.escape(str(u["id"])),"__ADMIN_AREA__":html.escape(role_label),"__ADMIN_SCOPE__":"ALL" if is_global_admin(u) else str(u["area"]),"__AREA_OPTIONS__":area_opts,"__ROLE_OPTIONS__":role_opts}
    for k,v in replacements.items(): template=template.replace(k,v)
    return template,u

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    template,u=admin_shell("admin_dashboard.html",request)
    if template is None:
        if not u: return HTMLResponse((BASE/"templates/admin_login.html").read_text(encoding="utf-8"))
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>Admin access required</h2>",status_code=403)
    return HTMLResponse(template)

@app.get("/admin/upload", response_class=HTMLResponse)
def admin_upload_page(request: Request):
    template,u=admin_shell("admin_upload.html",request)
    if template is None: return HTMLResponse("Admin login required",status_code=403)
    return HTMLResponse(template)

@app.get("/admin/stores", response_class=HTMLResponse)
def admin_stores_page(request: Request):
    template,u=admin_shell("admin_stores.html",request)
    if template is None: return HTMLResponse("Admin login required",status_code=403)
    return HTMLResponse(template)

@app.get("/admin/employees", response_class=HTMLResponse)
def admin_employees_page(request: Request):
    template,u=admin_shell("admin_employees.html",request)
    if template is None: return HTMLResponse("Admin login required",status_code=403)
    return HTMLResponse(template)

@app.post("/admin/login")
def admin_login(request: Request, employee_id: str=Form(...), password: str=Form(...)):
    eid=employee_id.strip()
    c=db(); u=c.execute("SELECT * FROM employees WHERE id=? AND active=1",(eid,)).fetchone()
    if not u or u["role"] not in ("ADMIN","SUPERADMIN") or u["password_hash"]!=password_hash(password):
        c.close()
        return HTMLResponse((BASE/"templates/admin_login.html").read_text(encoding="utf-8").replace("__ERROR__","Invalid admin ID or password."),status_code=401)
    token=secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(?,?,?)",(token,eid,now())); c.commit(); c.close()
    response=RedirectResponse("/admin",status_code=303)
    response.set_cookie(key="session",value=token,httponly=True,samesite="lax",secure=False,max_age=60*60*12,path="/")
    return response

@app.get("/health")
def health():
    return {"ok": True, "version": "V25"}

@app.post("/login")
def browser_login(request: Request, employee_id: str = Form(...), password: str = Form(...)):
    eid = employee_id.strip()
    c = db()
    u = c.execute("SELECT * FROM employees WHERE id=? AND active=1", (eid,)).fetchone()
    if not u or u["password_hash"] != password_hash(password):
        c.close()
        return HTMLResponse("""
        <html><body style="font-family:Arial;padding:30px">
        <h2>Login failed</h2><p>Invalid Employee ID or password.</p>
        <a href="/">Back to login</a>
        </body></html>
        """, status_code=401)
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(?,?,?)", (token, eid, now()))
    c.commit()
    c.close()
    target = "/admin" if u["role"] in ("ADMIN","SUPERADMIN") else f"/?token={token}"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        key="session", value=token, httponly=True, samesite="lax",
        secure=False, max_age=60*60*12, path="/"
    )
    return response

@app.post("/api/login")
def login(payload: dict):
    eid = str(payload.get("id","")).strip()
    pw = str(payload.get("password",""))
    c = db()
    u = c.execute("SELECT * FROM employees WHERE id=? AND active=1", (eid,)).fetchone()
    if not u or u["password_hash"] != password_hash(pw):
        c.close()
        raise HTTPException(401, "Invalid Employee ID or password")
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions VALUES(?,?,?)", (token, eid, now()))
    c.commit()
    c.close()
    return {"ok": True, "token": token, "id": u["id"], "name": u["name"], "area": u["area"], "role": u["role"]}

@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        c = db()
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()
        c.close()
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    u = require_user(request)
    return {"id":u["id"],"name":u["name"],"area":u["area"],"role":u["role"]}

@app.get("/api/stores")
def stores(request: Request):
    u = require_user(request)
    c = db()
    if is_global_admin(u):
        rows = c.execute("SELECT * FROM stores WHERE active=1 ORDER BY area,name").fetchall()
    else:
        rows = c.execute("SELECT * FROM stores WHERE active=1 AND area=? ORDER BY name", (u["area"],)).fetchall()
    c.close()
    return [dict(x) for x in rows]


@app.get("/pda/store", response_class=HTMLResponse)
def pda_store_selected(request: Request, store_id: int):
    return pda_store_page(request, store_id)

@app.get("/pda/store/{store_id}", response_class=HTMLResponse)
def pda_store_page(request: Request, store_id: int):
    u=require_user(request); tok=session_token(request)
    c=db()
    if is_global_admin(u):
        store=c.execute("SELECT * FROM stores WHERE id=? AND active=1",(store_id,)).fetchone()
    else:
        store=c.execute("SELECT * FROM stores WHERE id=? AND area=? AND active=1",(store_id,u["area"])).fetchone()
    if not store:
        c.close(); raise HTTPException(404,"Store not found or not available for your Area")
    rows=c.execute("""SELECT o.*,COUNT(l.id) lines,COALESCE(SUM(l.required),0) units,COALESCE(SUM(l.picked),0) picked
        FROM orders o LEFT JOIN order_lines l ON l.order_id=o.id
        WHERE o.store=? AND (o.status='WAITING' OR (o.status='PICKING' AND o.assigned_to=?))
        GROUP BY o.id ORDER BY o.created_at""",(store["name"],u["id"])).fetchall()
    c.close()
    cards=[]
    for o in rows:
        shortage_badge=(f'<span class="short-badge">SHORTAGE · FROM {html.escape(str(o["source_order_no"]))}</span>' if o["is_shortage"] else '')
        cards.append(f"""<div class="order">{shortage_badge}<h3>{html.escape(str(o["order_no"]))}</h3><small>{o["lines"]} lines · {o["picked"]}/{o["units"]} units · {html.escape(str(o["status"]))}</small><form action="/pda/order/{o["id"]}/claim" method="post"><input type="hidden" name="token" value="{html.escape(tok,quote=True)}"><button type="submit">SELECT ORDER</button></form></div>""")
    body=''.join(cards) or '<div class="card">No available orders for this store.</div>'
    page=f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>W Cosmetics PDA V21</title><link rel="stylesheet" href="/static/style.css"></head><body><main>
<header><b>W COSMETICS</b><small>PDA PICKING · V26 ONLINE</small></header>
<div class="bar"><a class="navlink" href="/?token={html.escape(tok,quote=True)}">← STORES</a><b>{html.escape(str(store["name"]))}</b></div>
<div class="card"><b>{html.escape(str(u["name"]))} · {html.escape(str(u["id"]))}</b><div class="muted">AREA: {html.escape(str(u["area"]))}</div></div>
<div class="card"><h2>ORDER POOL</h2>{body}</div></main></body></html>"""
    return HTMLResponse(page)

@app.post("/pda/order/{oid}/claim")
def pda_claim_form(request: Request, oid: str, token: str = Form("")):
    u=require_user(request); tok=token or session_token(request)
    c=db(); o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o:
        c.close(); raise HTTPException(404,"Order not found")
    if not is_global_admin(u) and o["area"]!=u["area"]:
        c.close(); raise HTTPException(403,"Wrong area")
    if o["status"]=="WAITING":
        c.execute("UPDATE orders SET status='PICKING',assigned_to=?,started_at=? WHERE id=? AND status='WAITING'",(u["id"],now(),oid))
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not is_global_admin(u) and o["assigned_to"]!=u["id"]:
        c.close(); raise HTTPException(409,"Order is already assigned to another picker")
    c.commit(); c.close()
    return RedirectResponse("/pda/pick/"+oid+"?token="+tok, status_code=303)

def _pick_access(c,u,oid):
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o: raise HTTPException(404,"Order not found")
    if not is_global_admin(u) and (o["area"]!=u["area"] or o["assigned_to"]!=u["id"]):
        raise HTTPException(403,"Access denied")
    return o

def _clean_sku_value(x):
    v=str(x or "").strip().replace("\r","").replace("\n","").replace("\t","")
    if v.lower() in {"nan","none","null"}: return ""
    # Excel numeric cells can become 123456789.0; remove only a trailing .0.
    if v.endswith(".0") and v[:-2].isdigit(): v=v[:-2]
    return v

def _valid_product_name(x):
    v=str(x or "").strip()
    return bool(v) and v.lower() not in {"nan","none","null","-","product name not available","productname not available"}

def _excel_product_map(src: Path, target_store=""):
    """Read an order Excel and return {normalized SKU: product name}.
    W Cosmetics files use a store-specific header such as `name Manukau`.
    The implementation intentionally supports that exact format plus common
    alternatives and, as a final fallback, the first text column after SKU.
    """
    try:
        df=pd.read_excel(src, dtype=object)
    except Exception:
        return {}
    if df.empty: return {}
    df.columns=[str(x).strip() for x in df.columns]
    cols=list(df.columns)
    sku_col=next((x for x in cols if normalize(x) in {"sku","productcode","code","pbcode"}),None)
    if not sku_col: return {}
    nstore=normalize(target_store)
    name_cols=[]
    if nstore:
        name_cols += [x for x in cols if nstore in normalize(x) and ("name" in normalize(x) or "product" in normalize(x))]
    name_cols += [x for x in cols if normalize(x) in {"name","productname","product","description","productdescription","itemname","skuname","title","desc"}]
    try: idx=cols.index(sku_col)
    except ValueError: idx=-1
    if idx>=0 and idx+1<len(cols): name_cols.append(cols[idx+1])
    # Deduplicate columns while preserving priority.
    seen=set(); name_cols=[x for x in name_cols if not (x in seen or seen.add(x))]
    # Never use quantity/SOH/BIN as product names.
    excluded={sku_col}
    for x in cols:
        if normalize(x).startswith("reorderquantity") or normalize(x) in {"qty","quantity","required","soh","bin"}: excluded.add(x)
    out={}
    for _,r in df.iterrows():
        sku=_clean_sku_value(r.get(sku_col,""))
        if not sku: continue
        val=""
        for col in name_cols:
            if col in excluded: continue
            candidate=str(r.get(col,"" )).strip()
            if _valid_product_name(candidate): val=candidate; break
        if not val:
            # Last resort: first non-numeric text column after SKU.
            for col in cols[idx+1:] if idx>=0 else cols:
                if col in excluded: continue
                candidate=str(r.get(col,"" )).strip()
                if _valid_product_name(candidate) and not candidate.replace(".","",1).isdigit():
                    val=candidate; break
        if val: out[sku]=val
    return out

def find_product_name_for_line(order_row, sku, store):
    target=_clean_sku_value(sku)
    if not target: return ""
    # 1) Product master, but ignore old placeholder values.
    c=db(); m=c.execute("SELECT product_name FROM product_master WHERE sku=?",(target,)).fetchone(); c.close()
    if m and _valid_product_name(m["product_name"]): return str(m["product_name"]).strip()

    # 2) Search the exact stored order file first, then every uploaded Excel.
    candidates=[]
    original=str(order_row["filename"] or "").strip()
    if original:
        candidates += [UPLOADS/original, UPLOADS/Path(original).name]
    candidates += [UPLOADS/f'{order_row["id"]}.xlsx', UPLOADS/f'{order_row["id"]}.xls']
    candidates += sorted(UPLOADS.glob("*.xlsx"))+sorted(UPLOADS.glob("*.xls"))
    seen=set()
    for src in candidates:
        src=Path(src)
        try: key=str(src.resolve())
        except Exception: key=str(src)
        if key in seen or not src.exists(): continue
        seen.add(key)
        mapping=_excel_product_map(src, str(store or ""))
        if target in mapping: return mapping[target]
    return ""

@app.get("/pda/pick/{oid}", response_class=HTMLResponse)
def pda_pick_page(request: Request, oid: str):
    u=require_user(request); tok=session_token(request); c=db()
    o=_pick_access(c,u,oid)
    line=c.execute("SELECT * FROM order_lines WHERE order_id=? AND skipped=0 AND picked<required ORDER BY id LIMIT 1",(oid,)).fetchone()
    total=c.execute("SELECT COUNT(*) n FROM order_lines WHERE order_id=?",(oid,)).fetchone()["n"]
    done=c.execute("SELECT COUNT(*) n FROM order_lines WHERE order_id=? AND (skipped=1 OR picked>=required)",(oid,)).fetchone()["n"]
    if not line:
        c.close()
        page='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta http-equiv="Cache-Control" content="no-store"><title>W Cosmetics Picking</title><link rel="stylesheet" href="/static/style.css"></head><body><main>
<header><b>W COSMETICS</b><small>PDA PICKING · V26 ONLINE</small></header>
<div class="topbar"><a class="navlink" href="/?token=__TOKEN__">← STORES</a><span class="area-pill">__AREA__ · __STORE__</span></div>
<div class="card complete"><div class="complete-icon">✓</div><h2>ALL PICKING COMPLETE</h2><p>__DONE__ / __TOTAL__ lines completed</p></div>
<form action="/pda/pick/__OID__/finish" method="post"><input type="hidden" name="token" value="__TOKEN__"><button type="submit">FINISH PICKING</button></form>
</main></body></html>'''
        page=page.replace('__TOKEN__',html.escape(tok,quote=True)).replace('__AREA__',html.escape(str(o['area']))).replace('__STORE__',html.escape(str(o['store']))).replace('__DONE__',str(done)).replace('__TOTAL__',str(total)).replace('__OID__',html.escape(oid,quote=True))
        return HTMLResponse(page)

    pname=str(line["product_name"] or "").strip()
    if pname.lower() in {"nan","none","null","product name not available"}: pname=""
    if not pname:
        pname=find_product_name_for_line(o,line["sku"],o["store"])
        if pname:
            c.execute("UPDATE order_lines SET product_name=? WHERE id=?",(pname,line["id"])); c.commit()
    c.close()
    if pname:
        # Keep the recovered value permanently for future orders/PDA sessions.
        cc=db()
        cc.execute("INSERT INTO product_master(sku,product_name,updated_at) VALUES(?,?,?) ON CONFLICT(sku) DO UPDATE SET product_name=excluded.product_name, updated_at=excluded.updated_at",(str(line["sku"]).strip(),pname,now()))
        cc.commit(); cc.close()
    if not pname:
        # Final deterministic recovery pass from every Excel currently stored.
        # This is deliberately executed on the Picking request so old orders
        # can be repaired without re-uploading or recreating the order.
        pname=find_product_name_for_line(o,line["sku"],o["store"])
        if pname:
            c2=db(); c2.execute("UPDATE order_lines SET product_name=? WHERE id=?",(pname,line["id"]))
            c2.execute("INSERT INTO product_master(sku,product_name,updated_at) VALUES(?,?,?) ON CONFLICT(sku) DO UPDATE SET product_name=excluded.product_name, updated_at=excluded.updated_at",(_clean_sku_value(line["sku"]),pname,now())); c2.commit(); c2.close()
    pname=pname or "Product name not available"
    expected_bin=str(line["bin"] or "").strip()
    scan_error=request.query_params.get("error")=="wrong"
    scan_ok=request.query_params.get("scan")=="ok"
    bulk_qty=request.query_params.get("bulk","")
    bulk_error=request.query_params.get("bulk_error","")
    if bulk_error=="wrong":
        error_html='<div class="scan-error">✕ BULK SCAN WRONG SKU — nothing was added</div>'
    elif bulk_error=="qty":
        error_html='<div class="scan-error">✕ BULK QUANTITY INVALID — check the remaining quantity</div>'
    elif bulk_qty:
        error_html=f'<div class="scan-ok">✓ BULK SCAN ACCEPTED · +{html.escape(str(bulk_qty))} PICKED</div>'
    elif scan_error:
        error_html='<div class="scan-error">✕ WRONG SKU — please scan the correct product</div>'
    elif scan_ok:
        error_html='<div class="scan-ok">✓ SCAN ACCEPTED · +1 PICKED</div>'
    else:
        error_html='' 
    page=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta http-equiv="Cache-Control" content="no-store"><title>W Cosmetics Picking</title><link rel="stylesheet" href="/static/style.css"></head><body data-remaining="__REMAIN__"><main>
<header><b>W COSMETICS</b><small>PDA PICKING · V26 ONLINE</small></header>
<div class="topbar"><a class="navlink" href="/?token=__TOKEN__">← STORES</a><span class="area-pill">__AREA__ · __STORE__</span></div>
<div class="order-head"><div><span>ORDER</span><strong>__ORDER__</strong></div><div class="line-count"><span>LINES</span><strong>__DONE__/__TOTAL__</strong></div></div>
<div class="product-card">
<div class="eyebrow">PRODUCT</div><h1 class="product-name">__PNAME__</h1>
<div class="sku-row"><span>SKU</span><strong>__SKU__</strong></div>
__BIN_HTML__
<div class="qty-grid"><div><span>REQUIRED</span><strong>__REQ__</strong></div><div class="picked-box"><span>PICKED</span><strong>__PICKED__</strong></div><div><span>REMAINING</span><strong>__REMAIN__</strong></div></div>
</div>
<div class="scan-card" id="scannerCard">
<div class="scan-label">SCAN SKU</div>
<div class="scan-help"><b>Normal scan = 1 unit.</b> Scan the SKU with the DT50X trigger. No BIN scan and no submit button.</div>
__ERROR_HTML__
<form id="scanForm" action="/pda/pick/__OID__/scan-sku" method="post" autocomplete="off">
<input type="hidden" name="token" value="__TOKEN__"><input type="hidden" name="line_id" value="__LINE_ID__">
<input type="hidden" id="bulkMode" name="bulk_mode" value="0"><input type="hidden" id="bulkQuantity" name="quantity" value="1">
<input id="scanInput" class="scan-input" name="code" autofocus inputmode="numeric" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="SCAN SKU NOW">
</form>
<div class="bulk-toggle"><label><input id="bulkCheck" type="checkbox"> <b>BULK SCAN</b></label><span id="bulkStatus">OFF · 1 UNIT</span></div>
<div id="scanState" class="scan-state">READY · PRESS DT50X SCAN TRIGGER</div>
</div>
<form action="/pda/pick/__OID__/short" method="post" onsubmit="return confirm('Confirm SHORT PICK for this line? The remaining quantity will be recorded as shortage.');"><input type="hidden" name="token" value="__TOKEN__"><input type="hidden" name="line_id" value="__LINE_ID__"><button class="warn" type="submit">SHORT PICK</button></form>
<form action="/pda/pick/__OID__/finish" method="post" onsubmit="if(!confirm('Are you sure you want to FINISH PICKING this order? The order will be marked COMPLETED and sent to Admin for export.')) return false; this.querySelector('[name=confirm]').value='yes'; return true;"><input type="hidden" name="token" value="__TOKEN__"><input type="hidden" name="confirm" value=""><button class="finish-btn" type="submit">FINISH PICKING</button></form>
<script src="/static/pda_scan.js"></script>
</main></body></html>'''
    bin_html=f'<div class="bin-info"><span>BIN</span><strong>{html.escape(expected_bin)}</strong></div>' if expected_bin else ''
    vals={'__TOKEN__':html.escape(tok,quote=True),'__AREA__':html.escape(str(o['area'])),'__STORE__':html.escape(str(o['store'])),'__ORDER__':html.escape(str(o['order_no'])),'__DONE__':str(done),'__TOTAL__':str(total),'__PNAME__':html.escape(pname),'__SKU__':html.escape(str(line['sku'])),'__BIN_HTML__':bin_html,'__REQ__':str(int(line['required'])),'__PICKED__':str(int(line['picked'])),'__REMAIN__':str(int(line['required'])-int(line['picked'])),'__ERROR_HTML__':error_html,'__OID__':html.escape(oid,quote=True),'__LINE_ID__':str(line['id'])}
    for k,v in vals.items(): page=page.replace(k,v)
    return HTMLResponse(page)

def _scan_line(c,u,oid,line_id):
    l=c.execute("SELECT l.*,o.area,o.assigned_to,o.store FROM order_lines l JOIN orders o ON o.id=l.order_id WHERE l.id=? AND l.order_id=?",(line_id,oid)).fetchone()
    if not l: raise HTTPException(404,"Line not found")
    if not is_global_admin(u) and (l["area"]!=u["area"] or l["assigned_to"]!=u["id"]): raise HTTPException(403,"Access denied")
    return l

@app.post("/pda/pick/{oid}/scan-sku")
def pda_scan_sku(request: Request, oid: str, line_id: int=Form(...), code: str=Form(...), token: str=Form(""), bulk_mode: str=Form("0"), quantity: int=Form(1)):
    u=require_user(request); tok=token or session_token(request); c=db(); l=_scan_line(c,u,oid,line_id)
    def clean_scan(x):
        v=str(x or "").replace("\r","").replace("\n","").replace("\t","").strip()
        if v.endswith(".0") and v[:-2].isdigit(): v=v[:-2]
        return v
    scanned=clean_scan(code); expected=clean_scan(l["sku"])
    remaining=max(0,int(l["required"])-int(l["picked"]))
    is_bulk=str(bulk_mode).lower() in {"1","true","yes","on"}
    qty=max(1,int(quantity or 1)) if is_bulk else 1
    if is_bulk and qty>remaining:
        c.close(); return RedirectResponse(f"/pda/pick/{oid}?token={tok}&bulk_error=qty",status_code=303)
    if scanned!=expected:
        c.execute("INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at) VALUES(?,?,?,?,?,?,?)",(oid,line_id,u["id"],"WRONG_SCAN",0,scanned,now())); c.commit(); c.close()
        return RedirectResponse(f"/pda/pick/{oid}?token={tok}&bulk_error=wrong" if is_bulk else f"/pda/pick/{oid}?token={tok}&error=wrong",status_code=303)
    new_picked=min(int(l["required"]),int(l["picked"])+qty)
    c.execute("UPDATE order_lines SET picked=? WHERE id=?",(new_picked,line_id))
    c.execute("INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at) VALUES(?,?,?,?,?,?,?)",(oid,line_id,u["id"],"BULK_SCAN" if is_bulk else "SCAN",qty,scanned,now()))
    c.commit(); c.close()
    return RedirectResponse(f"/pda/pick/{oid}?token={tok}&bulk={qty}" if is_bulk else f"/pda/pick/{oid}?token={tok}&scan=ok",status_code=303)

@app.post("/pda/pick/{oid}/bulk-scan")
def pda_bulk_scan(request: Request, oid: str, line_id: int=Form(...), code: str=Form(...), quantity: int=Form(...), token: str=Form("")):
    u=require_user(request); tok=token or session_token(request); c=db(); l=_scan_line(c,u,oid,line_id)
    def clean_scan(x):
        v=str(x or "").replace("\r","").replace("\n","").replace("\t","").strip()
        if v.endswith(".0") and v[:-2].isdigit(): v=v[:-2]
        return v
    scanned=clean_scan(code); expected=clean_scan(l["sku"]); qty=max(0,int(quantity)); remaining=max(0,int(l["required"])-int(l["picked"]))
    if qty<1 or qty>remaining:
        c.close(); return RedirectResponse(f"/pda/pick/{oid}?token={tok}&bulk_error=qty",status_code=303)
    if scanned!=expected:
        c.execute("INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at) VALUES(?,?,?,?,?,?,?)",(oid,line_id,u["id"],"WRONG_SCAN",0,scanned,now())); c.commit(); c.close()
        return RedirectResponse(f"/pda/pick/{oid}?token={tok}&bulk_error=wrong",status_code=303)
    new_picked=int(l["picked"])+qty
    c.execute("UPDATE order_lines SET picked=? WHERE id=?",(new_picked,line_id))
    c.execute("INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at) VALUES(?,?,?,?,?,?,?)",(oid,line_id,u["id"],"BULK_SCAN",qty,scanned,now()))
    c.commit(); c.close()
    return RedirectResponse(f"/pda/pick/{oid}?token={tok}&bulk={qty}",status_code=303)

@app.post("/pda/pick/{oid}/short")
def pda_short(request: Request, oid: str, line_id: int=Form(...), token: str=Form("")):
    u=require_user(request); tok=token or session_token(request); c=db(); l=_scan_line(c,u,oid,line_id); remaining=max(0,int(l["required"])-int(l["picked"]))
    c.execute("UPDATE order_lines SET skipped=1 WHERE id=?",(line_id,)); c.execute("INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at) VALUES(?,?,?,?,?,?,?)",(oid,line_id,u["id"],"SHORT_PICK",remaining,"Stock unavailable",now())); c.commit(); c.close()
    return RedirectResponse(f"/pda/pick/{oid}?token={tok}",status_code=303)

@app.post("/pda/pick/{oid}/finish")
def pda_finish(request: Request, oid: str, token: str=Form(""), confirm: str=Form("")):
    u=require_user(request); tok=token or session_token(request); c=db(); o=_pick_access(c,u,oid)
    # Finish is immediate. Any remaining quantity becomes a new WAITING shortage order
    # and goes straight back into the PDA order pool. The new order records exactly
    # which completed order it came from.
    lines=c.execute("SELECT * FROM order_lines WHERE order_id=? ORDER BY id",(oid,)).fetchall()
    shortages=[]
    for l in lines:
        remaining=max(0,int(l["required"])-int(l["picked"]))
        if remaining>0:
            shortages.append((l,remaining))
    completed_at=now()
    c.execute("UPDATE orders SET status='COMPLETED',completed_at=? WHERE id=?",(completed_at,oid))
    if shortages:
        existing=c.execute("SELECT id FROM orders WHERE source_order_id=? AND is_shortage=1 AND status IN ('WAITING','PICKING') LIMIT 1",(oid,)).fetchone()
        if not existing:
            # Make a stable, human-readable shortage order number and avoid collisions.
            base=f"{o['order_no']}-SHORTAGE"
            shortage_no=base
            n=2
            while c.execute("SELECT 1 FROM orders WHERE order_no=?",(shortage_no,)).fetchone():
                shortage_no=f"{base}-{n}"; n+=1
            sid=uuid.uuid4().hex
            root_id=o["source_order_id"] if o["is_shortage"] and o["source_order_id"] else oid
            root_no=o["source_order_no"] if o["is_shortage"] and o["source_order_no"] else o["order_no"]
            c.execute("""INSERT INTO orders(id,order_no,filename,area,store,status,assigned_to,created_at,started_at,completed_at,is_shortage,source_order_id,source_order_no)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (sid,shortage_no,o["filename"],o["area"],o["store"],"WAITING",None,completed_at,None,None,1,root_id,root_no))
            for l,remaining in shortages:
                c.execute("""INSERT INTO order_lines(order_id,sku,product_name,required,soh,bin,picked,skipped)
                             VALUES(?,?,?,?,?,?,0,0)""",
                          (sid,str(l["sku"]),l["product_name"],remaining,l["soh"],l["bin"]))
    c.commit(); c.close()
    return RedirectResponse(f"/?token={tok}&finished=1",status_code=303)

@app.post("/api/admin/backfill-product-names")
def backfill_product_names(request: Request):
    admin=require_admin(request)
    c=db()
    if is_global_admin(admin):
        orders=c.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    else:
        orders=c.execute("SELECT * FROM orders WHERE area=? ORDER BY created_at DESC",(admin["area"],)).fetchall()
    updated=0
    for o in orders:
        blanks=c.execute("SELECT * FROM order_lines WHERE order_id=? AND (product_name IS NULL OR TRIM(product_name)='' OR lower(trim(product_name))='product name not available')",(o["id"],)).fetchall()
        for line in blanks:
            pname=find_product_name_for_line(o,line["sku"],o["store"])
            if pname:
                c.execute("UPDATE order_lines SET product_name=? WHERE id=?",(pname,line["id"]))
                c.execute("INSERT INTO product_master(sku,product_name,updated_at) VALUES(?,?,?) ON CONFLICT(sku) DO UPDATE SET product_name=excluded.product_name, updated_at=excluded.updated_at",(str(line["sku"]).strip(),pname,now()))
                updated+=1
    c.commit();c.close()
    return {"ok":True,"updated":updated}

@app.post("/api/admin/stores")
def add_store(request: Request, payload: dict):
    admin=require_admin(request)
    name=str(payload.get("name","" )).strip()
    area=str(payload.get("area","" )).strip().upper()
    if not name or area not in ("NZ","AU"):
        raise HTTPException(400,"Store name and NZ/AU area are required")
    if not admin_can_access_area(admin, area):
        raise HTTPException(403,"Your admin account cannot manage this Area")
    c=db()
    try:
        c.execute("INSERT INTO stores(name,area) VALUES(?,?)",(name,area))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); raise HTTPException(400,"Store already exists")
    c.close(); return {"ok":True}

@app.post("/api/admin/orders")
async def upload_order(request: Request, area: str = Form(...), file: UploadFile = File(...)):
    admin = require_admin(request)
    area = area.upper()
    if area not in ("NZ","AU"):
        raise HTTPException(400,"Invalid area")
    if not admin_can_access_area(admin, area):
        raise HTTPException(403, "Your admin account cannot manage this Area")
    if not file.filename.lower().endswith((".xlsx",".xls")):
        raise HTTPException(400,"Excel file required")

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(400,"Order filename is required")
    c_check=db()
    duplicate=c_check.execute("SELECT order_no,area,status FROM orders WHERE lower(trim(filename))=lower(trim(?)) LIMIT 1",(filename,)).fetchone()
    c_check.close()
    if duplicate:
        raise HTTPException(409,f"Order file already uploaded: {filename}")

    temp = BASE / f"_tmp_{uuid.uuid4().hex}.xlsx"
    try:
        with temp.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        store, rows = parse_excel(temp)

        c=db()
        # Resolve the Excel store label to the canonical Store Management name.
        # This handles "St_Lukes" vs "St Lukes" and similar formatting differences.
        candidates = c.execute(
            "SELECT * FROM stores WHERE area=? AND active=1 ORDER BY name",
            (area,)
        ).fetchall()
        matched = next((x for x in candidates if normalize(x["name"]) == normalize(store)), None)

        if not matched:
            c.close()
            raise HTTPException(
                400,
                f"Store '{store}' from Excel B1 is not configured in {area}. "
                f"Add the actual store name in Store Management."
            )

        store = matched["name"]

        oid=str(uuid.uuid4())
        order_no=f"{area}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"
        shutil.copy2(temp, UPLOADS/f"{oid}.xlsx")
        # Also keep a readable copy under the original filename. This makes
        # product-name recovery deterministic for existing orders.
        safe_original=Path(filename).name
        if safe_original and not (UPLOADS/safe_original).exists():
            shutil.copy2(temp, UPLOADS/safe_original)
        c.execute("""INSERT INTO orders
            (id,order_no,filename,area,store,status,assigned_to,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (oid,order_no,file.filename,area,store,"WAITING",None,now()))
        for sku,pname,qty,soh,bin_code in rows:
            c.execute("""INSERT INTO order_lines
                (order_id,sku,product_name,required,soh,bin)
                VALUES(?,?,?,?,?,?)""",(oid,sku,pname,qty,soh,bin_code))
            if str(pname or "").strip():
                c.execute("INSERT INTO product_master(sku,product_name,updated_at) VALUES(?,?,?) ON CONFLICT(sku) DO UPDATE SET product_name=excluded.product_name, updated_at=excluded.updated_at",(str(sku).strip(),str(pname).strip(),now()))
        c.commit(); c.close()
        return {"ok":True,"order_id":oid,"order_no":order_no,"area":area,"store":store,
                "lines":len(rows),"units":sum(x[2] for x in rows)}
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass

@app.post("/api/admin/orders/{oid}/delete")
def delete_order(request: Request, oid: str):
    admin=require_admin(request)
    c=db()
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o:
        c.close(); raise HTTPException(404,"Order not found")
    if not admin_can_access_area(admin,o["area"]):
        c.close(); raise HTTPException(403,"Wrong area")
    c.execute("DELETE FROM events WHERE order_id=?",(oid,))
    c.execute("DELETE FROM order_lines WHERE order_id=?",(oid,))
    c.execute("DELETE FROM orders WHERE id=?",(oid,))
    c.commit(); c.close()
    try:
        stored=UPLOADS/f"{oid}.xlsx"
        if stored.exists(): stored.unlink()
    except Exception:
        pass
    return {"ok":True,"deleted":oid}

@app.get("/api/orders")
def list_orders(request: Request):
    u=require_user(request)
    c=db()
    where=""; args=[]
    if not is_global_admin(u):
        where="WHERE o.area=?"; args=[u["area"]]
    rows=c.execute(f"""
        SELECT o.*, COUNT(l.id) AS lines, COALESCE(SUM(l.required),0) AS units,
               COALESCE(SUM(l.picked),0) AS picked,
               COALESCE(SUM(CASE WHEN l.picked>=l.required OR l.skipped=1 THEN 1 ELSE 0 END),0) AS done_lines,
               COALESCE(SUM(CASE WHEN l.picked<l.required THEN 1 ELSE 0 END),0) AS open_lines,
               COALESCE(SUM(CASE WHEN l.skipped=1 AND l.picked<l.required THEN 1 ELSE 0 END),0) AS shortage_lines
        FROM orders o LEFT JOIN order_lines l ON l.order_id=o.id
        {where}
        GROUP BY o.id ORDER BY o.created_at DESC
    """,args).fetchall()
    c.close()
    return [dict(x) for x in rows]

@app.post("/api/order/{oid}/claim")
def claim_order(request: Request, oid: str):
    u=require_user(request)
    c=db()
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o:
        c.close(); raise HTTPException(404,"Order not found")
    if not is_global_admin(u) and o["area"]!=u["area"]:
        c.close(); raise HTTPException(403,"Wrong area")
    if o["status"]=="WAITING":
        c.execute("""UPDATE orders SET status='PICKING',assigned_to=?,started_at=?
                     WHERE id=? AND status='WAITING'""",(u["id"],now(),oid))
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not is_global_admin(u) and o["assigned_to"] not in (None,u["id"]):
        c.close(); raise HTTPException(409,"Order is already assigned to another picker")
    c.commit(); c.close()
    return {"ok":True}

@app.get("/api/order/{oid}")
def get_order(request: Request, oid: str):
    u=require_user(request)
    c=db()
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o:
        c.close(); raise HTTPException(404,"Order not found")
    if not is_global_admin(u) and (o["area"]!=u["area"] or o["assigned_to"] not in (None,u["id"])):
        c.close(); raise HTTPException(403,"Access denied")
    lines=c.execute("""
        SELECT * FROM order_lines
        WHERE order_id=? AND skipped=0 AND picked<required
        ORDER BY id
    """,(oid,)).fetchall()
    c.close()
    return {"order":dict(o),"lines":[dict(x) for x in lines]}

@app.post("/api/verify-bin")
def verify_bin(request: Request, payload: dict):
    u=require_user(request)
    c=db()
    l=c.execute("""SELECT l.*,o.area,o.assigned_to FROM order_lines l
                   JOIN orders o ON o.id=l.order_id
                   WHERE l.id=? AND l.order_id=?""",(payload["line_id"],payload["order_id"])).fetchone()
    c.close()
    if not l: raise HTTPException(404,"Line not found")
    if not is_global_admin(u) and (l["area"]!=u["area"] or l["assigned_to"]!=u["id"]):
        raise HTTPException(403,"Access denied")
    expected=str(l["bin"] or "").strip().upper()
    code=str(payload.get("code","")).strip().upper()
    if expected in ("","NAN","NONE"):
        return {"ok":True}
    return {"ok":code==expected,"expected":expected}

@app.post("/api/scan")
def scan(request: Request, payload: dict):
    u=require_user(request)
    c=db()
    l=c.execute("""SELECT l.*,o.area,o.assigned_to FROM order_lines l
                   JOIN orders o ON o.id=l.order_id
                   WHERE l.id=? AND l.order_id=?""",(payload["line_id"],payload["order_id"])).fetchone()
    if not l:
        c.close(); raise HTTPException(404,"Line not found")
    if not is_global_admin(u) and (l["area"]!=u["area"] or l["assigned_to"]!=u["id"]):
        c.close(); raise HTTPException(403,"Access denied")

    code=str(payload.get("code","")).strip()
    if code != str(l["sku"]).strip():
        c.execute("""INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at)
                     VALUES(?,?,?,?,?,?,?)""",(l["order_id"],l["id"],u["id"],"WRONG_SCAN",0,code,now()))
        c.commit();c.close()
        return {"ok":False,"scanned":code,"expected":str(l["sku"])}

    new_picked=l["picked"]+1
    if new_picked>l["required"]:
        c.close(); return {"ok":False,"complete":True}
    c.execute("UPDATE order_lines SET picked=? WHERE id=?",(new_picked,l["id"]))
    c.execute("""INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at)
                 VALUES(?,?,?,?,?,?,?)""",(l["order_id"],l["id"],u["id"],"SCAN",1,code,now()))
    c.commit();c.close()
    return {"ok":True,"picked":new_picked,"remaining":l["required"]-new_picked}

@app.post("/api/short-pick")
def short_pick(request: Request, payload: dict):
    u=require_user(request)
    c=db()
    l=c.execute("""SELECT l.*,o.area,o.assigned_to FROM order_lines l JOIN orders o ON o.id=l.order_id
                   WHERE l.id=? AND l.order_id=?""",(payload["line_id"],payload["order_id"])).fetchone()
    if not l:c.close();raise HTTPException(404,"Line not found")
    if not is_global_admin(u) and (l["area"]!=u["area"] or l["assigned_to"]!=u["id"]):
        c.close();raise HTTPException(403,"Access denied")
    remaining=max(0,l["required"]-l["picked"])
    c.execute("UPDATE order_lines SET skipped=1 WHERE id=?",(l["id"],))
    c.execute("""INSERT INTO events(order_id,line_id,employee_id,event_type,qty,value,created_at)
                 VALUES(?,?,?,?,?,?,?)""",(l["order_id"],l["id"],u["id"],"SHORT_PICK",remaining,str(payload.get("reason","Other")),now()))
    c.commit();c.close()
    return {"ok":True,"shortage":remaining}

@app.post("/api/order/{oid}/finish")
def finish(request: Request, oid: str):
    u=require_user(request)
    c=db()
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o:c.close();raise HTTPException(404,"Order not found")
    if not is_global_admin(u) and o["assigned_to"]!=u["id"]:
        c.close();raise HTTPException(403,"Not assigned")
    # Finish Picking is immediate; remaining quantities are treated as shortage.
    c.execute("UPDATE orders SET status='COMPLETED',completed_at=? WHERE id=?",(now(),oid))
    c.commit();c.close()
    return {"ok":True}

def _export_rows(request: Request, oid: str):
    u=require_admin(request)
    c=db()
    o=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o:
        c.close(); raise HTTPException(404,"Order not found")
    if not is_global_admin(u) and o["area"]!=u["area"]:
        c.close(); raise HTTPException(403,"Wrong area")
    lines=c.execute("SELECT * FROM order_lines WHERE order_id=? ORDER BY id",(oid,)).fetchall()
    c.close()
    transfers=[]; shortages=[]
    for l in lines:
        picked=int(l["picked"]); remaining=max(0,int(l["required"])-picked)
        if picked>0:
            transfers.append({
                "*Transfer Number":o["order_no"],
                "*Source Warehouse Code":"Warehouse",
                "*Destination Warehouse Code":o["store"],
                "Comments":"",
                "*Product Code":str(l["sku"]),
                "*Transfer Quantity":picked,
                "Line Comments":""
            })
        if remaining>0:
            shortages.append({
                "sku":str(l["sku"]),
                f"name {o['store']}":l["product_name"],
                f"reorder_quantity_{o['store']}":remaining,
                "SOH":l["soh"],
                "BIN":l["bin"]
            })
    return o, transfers, shortages

@app.get("/api/export/{oid}/warehouse-transfer")
def export_warehouse_transfer(request: Request, oid: str):
    o, transfers, shortages=_export_rows(request,oid)
    out=EXPORTS/f"{o['order_no']}_WarehouseTransfer.xlsx"
    cols=["*Transfer Number","*Source Warehouse Code","*Destination Warehouse Code","Comments","*Product Code","*Transfer Quantity","Line Comments"]
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        pd.DataFrame(transfers,columns=cols).to_excel(writer,index=False,sheet_name="WarehouseTransfer")
        ws=writer.book["WarehouseTransfer"]
        for cell in ws["E"][1:]: cell.number_format="@"
    return FileResponse(out,filename=out.name,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.get("/api/export/{oid}/shortage")
def export_shortage(request: Request, oid: str):
    o, transfers, shortages=_export_rows(request,oid)
    out=EXPORTS/f"{o['order_no']}_Shortage.xlsx"
    cols=["sku",f"name {o['store']}",f"reorder_quantity_{o['store']}","SOH","BIN"]
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        pd.DataFrame(shortages,columns=cols).to_excel(writer,index=False,sheet_name="Shortage")
        ws=writer.book["Shortage"]
        for cell in ws["A"][1:]: cell.number_format="@"
    return FileResponse(out,filename=out.name,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.get("/api/admin/employees")
def get_employees(request: Request):
    u=require_admin(request)
    c=db()
    if is_global_admin(u):
        rows=c.execute("SELECT id,name,area,role,active,created_at FROM employees ORDER BY area,id").fetchall()
    else:
        rows=c.execute("SELECT id,name,area,role,active,created_at FROM employees WHERE area=? ORDER BY id",(u["area"],)).fetchall()
    c.close(); return [dict(x) for x in rows]

@app.post("/api/admin/employees")
def create_employee(request: Request, payload: dict):
    admin=require_admin(request)
    eid=str(payload.get("id","" )).strip()
    name=str(payload.get("name","" )).strip()
    pw=str(payload.get("password",""))
    area=str(payload.get("area","" )).upper()
    role=str(payload.get("role","PICKER" )).upper()
    allowed_roles={"PICKER","MANAGER","ADMIN"}
    if is_global_admin(admin):
        allowed_roles.add("SUPERADMIN")
    if not eid or not name or len(pw)<6 or area not in ("NZ","AU") or role not in allowed_roles:
        raise HTTPException(400,"Invalid employee data")
    if not admin_can_access_area(admin,area):
        raise HTTPException(403,"You can only create employees in your Area")
    if role=="SUPERADMIN" and not is_global_admin(admin):
        raise HTTPException(403,"Only the Global Admin can create a Global Admin")
    c=db()
    try:
        c.execute("INSERT INTO employees VALUES(?,?,?,?,?,?,?)",(eid,name,password_hash(pw),area,role,1,now())); c.commit()
    except sqlite3.IntegrityError:
        c.close(); raise HTTPException(400,"Employee ID already exists")
    c.close(); return {"ok":True}

@app.post("/api/admin/employees/{eid}/toggle")
def toggle_employee(request: Request, eid: str):
    admin=require_admin(request)
    c=db(); target=c.execute("SELECT * FROM employees WHERE id=?",(eid,)).fetchone()
    if not target: c.close(); raise HTTPException(404,"Employee not found")
    if target["role"]=="SUPERADMIN" and not is_global_admin(admin):
        c.close(); raise HTTPException(403,"Only the Global Admin can manage Global Admin accounts")
    if not admin_can_access_area(admin,target["area"]):
        c.close(); raise HTTPException(403,"Access denied")
    c.execute("UPDATE employees SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(eid,));c.commit();c.close()
    return {"ok":True}

@app.post("/api/admin/employees/{eid}/password")
def reset_password(request: Request, eid: str, payload: dict):
    admin=require_admin(request)
    pw=str(payload.get("password",""))
    if len(pw)<6: raise HTTPException(400,"Password must be at least 6 characters")
    c=db(); target=c.execute("SELECT * FROM employees WHERE id=?",(eid,)).fetchone()
    if not target: c.close(); raise HTTPException(404,"Employee not found")
    if target["role"]=="SUPERADMIN" and not is_global_admin(admin):
        c.close(); raise HTTPException(403,"Only the Global Admin can reset Global Admin passwords")
    if not admin_can_access_area(admin,target["area"]):
        c.close(); raise HTTPException(403,"Access denied")
    c.execute("UPDATE employees SET password_hash=? WHERE id=?",(password_hash(pw),eid));c.commit();c.close()
    return {"ok":True}
