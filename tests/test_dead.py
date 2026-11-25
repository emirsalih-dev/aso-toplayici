"""Olu kayit mekanizmasi testleri — gercek data.db'ye dokunmaz."""
import os, sys, sqlite3, tempfile, importlib.util
from datetime import date, timedelta

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collect.py")
spec = importlib.util.spec_from_file_location("collect", SRC)
C = importlib.util.module_from_spec(spec); spec.loader.exec_module(C)

tmp = tempfile.mkdtemp()
C.DB_PATH = os.path.join(tmp, "t.db")
C.WATCHLIST_PATH = os.path.join(tmp, "watchlist.txt")
open(C.WATCHLIST_PATH, "w").write("com.biz.kapalitest  # bizim, yayinda degil\n")

today = date.today().isoformat()
ok = fail = 0
def check(ad, kosul):
    global ok, fail
    if kosul: ok += 1; print("  ok   ", ad)
    else:     fail += 1; print("  HATA ", ad)

print("== 1. yeni DB kolonlarla dogar ==")
conn = C.open_db()
cols = {r[1] for r in conn.execute("PRAGMA table_info(apps)")}
check("miss_streak + dead_since var", {"miss_streak","dead_since"} <= cols)

print("== 2. ust uste kacirma olu damgalar ==")
for pkg in ("com.rakip.kalkti", "com.biz.kapalitest", "com.rakip.saglam"):
    C.upsert_app(conn, pkg, today)
korumali = C.load_watchlist()
sonuc = [C.mark_missing(conn, "com.rakip.kalkti", today, korumali) for _ in range(C.DEAD_AFTER)]
check("esikten once olu degil", sonuc[:-1] == [False]*(C.DEAD_AFTER-1))
check("esikte olu damgalandi", sonuc[-1] is True)
for _ in range(5):
    C.mark_missing(conn, "com.biz.kapalitest", today, korumali)
d = conn.execute("select miss_streak,dead_since from apps where package_id='com.biz.kapalitest'").fetchone()
check("watchlist paketi olu damgalanmaz", d[1] is None and d[0] == 5)

print("== 3. olu paket detay taramasindan cikar ==")
sayac = {"keywords_ok":0,"keywords_err":0,"apps_ok":0,"apps_err":0}
src = C.FakeSource()
C.pass_details(conn, src, today, True, sayac, lambda *a, **k: None, None, oncelik=korumali)
kalan = [r[0] for r in conn.execute("select package_id from snapshots where captured_on=?", (today,))]
check("olu paket cekilmedi", "com.rakip.kalkti" not in kalan)
check("saglam paket cekildi", "com.rakip.saglam" in kalan)
check("watchlist paketi yine denendi", "com.biz.kapalitest" in kalan)

print("== 4. yeniden gorulmek dirilitir ==")
C.upsert_app(conn, "com.rakip.kalkti", today, title="Geri dondu")
d = conn.execute("select miss_streak,dead_since from apps where package_id='com.rakip.kalkti'").fetchone()
check("dirilme: streak 0, dead_since NULL", d == (0, None))

print("== 5. olu kayit DEAD_RETRY_DAYS sonra yeniden denenir ==")
eski = (date.today() - timedelta(days=C.DEAD_RETRY_DAYS + 1)).isoformat()
conn.execute("update apps set dead_since=?, miss_streak=? where package_id='com.rakip.kalkti'", (eski, C.DEAD_AFTER))
conn.execute("delete from snapshots where package_id='com.rakip.kalkti'")
conn.commit()
sql_params = [today, today, "-%d day" % C.DEAD_RETRY_DAYS]
rows = [r[0] for r in conn.execute("""
    SELECT a.package_id FROM apps a LEFT JOIN snapshots s
      ON s.package_id=a.package_id AND s.captured_on=?
    WHERE s.package_id IS NULL
      AND (a.dead_since IS NULL OR a.dead_since <= date(?, ?))""", sql_params)]
check("penceresi dolan olu tekrar sirada", "com.rakip.kalkti" in rows)
conn.execute("update apps set dead_since=? where package_id='com.rakip.kalkti'", (today,))
conn.commit()
rows = [r[0] for r in conn.execute("""
    SELECT a.package_id FROM apps a LEFT JOIN snapshots s
      ON s.package_id=a.package_id AND s.captured_on=?
    WHERE s.package_id IS NULL
      AND (a.dead_since IS NULL OR a.dead_since <= date(?, ?))""", sql_params)]
check("taze olu yine atlanir", "com.rakip.kalkti" not in rows)
conn.close()

print("\n== 6. eski semali DB gocu + geriye donuk isaretleme ==")
C.DB_PATH = os.path.join(tmp, "eski.db")
c2 = sqlite3.connect(C.DB_PATH)
c2.executescript("""
CREATE TABLE apps (package_id TEXT PRIMARY KEY, title TEXT, developer TEXT, genre TEXT,
                   first_seen TEXT, last_seen TEXT, discovered_via TEXT);
CREATE TABLE snapshots (package_id TEXT, captured_on TEXT, PRIMARY KEY(package_id,captured_on));
CREATE TABLE runs (started_at TEXT PRIMARY KEY, finished_at TEXT, keywords_ok INTEGER,
                   keywords_err INTEGER, apps_ok INTEGER, apps_err INTEGER, note TEXT);
""")
gunler = [(date.today()-timedelta(days=i)).isoformat() for i in range(3)]
for g in gunler:
    c2.execute("insert into runs (started_at,apps_ok) values (?,100)", (g+" 10:00:00",))
for pkg in ("com.olu.paket","com.canli.paket","com.biz.kapalitest"):
    c2.execute("insert into apps (package_id,first_seen,last_seen) values (?,?,?)", (pkg,gunler[-1],gunler[0]))
for g in gunler:                      # canli paketin her gun enstantanesi var
    c2.execute("insert into snapshots values (?,?)", ("com.canli.paket", g))
c2.commit(); c2.close()

conn = C.open_db()
r = dict((x[0], x[1:]) for x in conn.execute("select package_id,miss_streak,dead_since from apps"))
check("kacirandan olu uretildi", r["com.olu.paket"][1] is not None)
check("canli paket dokunulmadi", r["com.canli.paket"][1] is None)
check("watchlist goc sirasinda korundu", r["com.biz.kapalitest"][1] is None)
conn.close()

print("\n%d gecti, %d kaldi" % (ok, fail))
sys.exit(1 if fail else 0)
