#!/usr/bin/env python3
"""ASO veri toplayici — Google Play siralama arsivi.

Amac: her gun ayni saatte calisip anahtar kelime siralamalarini ve uygulama
enstantanelerini SQLite'a yazmak. Tarihsel siralama verisi sonradan satin
alinamaz; sadece her gun toplayarak uretilir.

Komutlar:
    collect          gunluk toplama (--fake, --limit N, --no-backup)
    backup           data.db'nin tutarli yedegi (online backup API, --keep N)
    stats            veritabani ozeti
    backfill-check   rankings tablosunda eksik gun var mi
    compare          iki gunu karsilastir (--limit, --keyword)
    text-analysis    baslik/aciklama analizi + kazanilabilir kelimeler

watchlist.txt: kendi paketlerimiz. Her kosuda, hicbir anahtar kelimede
siralanmasalar bile taranip arsivlenir (bkz. pass_watchlist).
"""

import argparse
import hashlib
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta

# ---------------------------------------------------------------- sabitler
LANG = "tr"
COUNTRY = "tr"
SEARCH_HITS = 50
DELAY_RANGE = (1.4, 3.2)
MAX_RETRIES = 3
RETRY_BACKOFF = 8

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
KEYWORDS_PATH = os.path.join(BASE_DIR, "keywords.txt")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.txt")
LOG_PATH = os.path.join(BASE_DIR, "collect.log")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
BACKUP_KEEP = 5          # saklanacak kusak sayisi; eskisi dondurulur
DEAD_AFTER = 3           # ust uste bu kadar "bulunamadi" -> olu sayilir
DEAD_RETRY_DAYS = 30     # olu kayit bu kadar gun sonra bir kez yeniden denenir
BACKUP_PREFIX = "data-"
BACKUP_SUFFIX = ".db"

log = logging.getLogger("aso")


def setup_logging():
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


# ---------------------------------------------------------------- veritabani
SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    package_id     TEXT PRIMARY KEY,
    title          TEXT,
    developer      TEXT,
    genre          TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    discovered_via TEXT,
    miss_streak    INTEGER NOT NULL DEFAULT 0,
    dead_since     TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    package_id    TEXT,
    captured_on   TEXT,
    title         TEXT,
    score         REAL,
    ratings       INTEGER,
    reviews       INTEGER,
    installs      TEXT,
    real_installs INTEGER,
    price         REAL,
    currency      TEXT,
    free          INTEGER,
    offers_iap    INTEGER,
    ad_supported  INTEGER,
    version       TEXT,
    last_updated  TEXT,
    genre         TEXT,
    developer     TEXT,
    histogram     TEXT,
    PRIMARY KEY (package_id, captured_on)
);

CREATE TABLE IF NOT EXISTS rankings (
    captured_on TEXT,
    keyword     TEXT,
    country     TEXT,
    lang        TEXT,
    rank        INTEGER,
    package_id  TEXT,
    title       TEXT,
    score       REAL,
    PRIMARY KEY (captured_on, keyword, country, lang, rank)
);

CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT PRIMARY KEY,
    finished_at  TEXT,
    keywords_ok  INTEGER DEFAULT 0,
    keywords_err INTEGER DEFAULT 0,
    apps_ok      INTEGER DEFAULT 0,
    apps_err     INTEGER DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS app_texts (
    package_id   TEXT,
    captured_on  TEXT,
    title        TEXT,
    summary      TEXT,
    description  TEXT,
    text_hash    TEXT,
    PRIMARY KEY (package_id, captured_on)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_day      ON snapshots(captured_on);
CREATE INDEX IF NOT EXISTS idx_texts_pkg          ON app_texts(package_id, captured_on);
CREATE INDEX IF NOT EXISTS idx_rankings_kw_day    ON rankings(keyword, captured_on);
CREATE INDEX IF NOT EXISTS idx_rankings_pkg_day   ON rankings(package_id, captured_on);
"""


def open_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def migrate(conn):
    """Sema gocleri. CREATE TABLE IF NOT EXISTS var olan tabloyu degistirmez,
    bu yuzden yeni kolonlar burada ALTER ile eklenir."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(apps)")}
    yeni = "miss_streak" not in have
    if yeni:
        conn.execute("ALTER TABLE apps ADD COLUMN miss_streak INTEGER NOT NULL DEFAULT 0")
    if "dead_since" not in have:
        conn.execute("ALTER TABLE apps ADD COLUMN dead_since TEXT")
    if yeni:
        _olu_geriye_donuk(conn)


def _olu_geriye_donuk(conn):
    """Goc anindaki bir kerelik geriye donuk isaretleme.

    Kolonlar eklenmeden onceki kacirmalar sayilmadi; onlari koklemek yerine
    veriden turetiyoruz: detay gecisinin gercekten calistigi (apps_ok>0) son
    DEAD_AFTER gunun HICBIRINDE enstantanesi olmayan paket, o kadar kez
    denenip bulunamamis demektir. --limit ile kesilen kosular apps_ok=0
    yazmadigi icin "sirasi hic gelmedi" durumu bu kumeye giremez.

    Watchlist disarida: kendi paketlerimiz yayinlanmadigi icin bulunamaz,
    olu degil "henuz dogmamis" sayilirlar (bkz. pass_details).
    """
    gunler = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(started_at,1,10) g FROM runs "
        "WHERE apps_ok > 0 ORDER BY g DESC LIMIT ?", (DEAD_AFTER,))]
    if len(gunler) < DEAD_AFTER:
        return                      # yeterli gecmis yok, ileriye donuk sayilsin
    korumali = load_watchlist()
    ph = ",".join("?" * len(gunler))
    kh = ",".join("?" * len(korumali)) if korumali else "''"
    conn.execute(
        """UPDATE apps SET miss_streak = ?, dead_since = ?
            WHERE package_id NOT IN (%s)
              AND NOT EXISTS (SELECT 1 FROM snapshots s
                               WHERE s.package_id = apps.package_id
                                 AND s.captured_on IN (%s))""" % (kh, ph),
        [DEAD_AFTER, max(gunler)] + korumali + gunler,
    )


# ---------------------------------------------------------------- veri kaynagi
class Skipped(Exception):
    """Kalici hata (ornegin uygulama kaldirilmis) — retry yapma."""


class Failed(Exception):
    """Gecici hata, retry'lar tukendi."""


class LiveSource:
    """Gercek Google Play istemcisi. Import tembel: --fake modda hic yuklenmez."""

    name = "live"

    def __init__(self):
        from google_play_scraper import app as gps_app, search as gps_search
        from google_play_scraper.exceptions import NotFoundError

        self._app = gps_app
        self._search = gps_search
        self.not_found = NotFoundError

    def search(self, keyword):
        return self._search(keyword, n_hits=SEARCH_HITS, lang=LANG, country=COUNTRY)

    def app(self, package_id):
        return self._app(package_id, lang=LANG, country=COUNTRY)


class FakeSource:
    """Aga hic cikmayan sahte kaynak. Sonuclar kelime+gune gore deterministik."""

    name = "fake"

    def __init__(self, seed_day=None):
        self.seed_day = seed_day or date.today().isoformat()
        self.not_found = _FakeNotFound

    @staticmethod
    def _slug(text):
        out = "".join(c if c.isalnum() else "" for c in text.lower())
        return out or "kelime"

    def search(self, keyword):
        rnd = random.Random("search|%s|%s" % (keyword, self.seed_day))
        slug = self._slug(keyword)
        results = []
        for i in range(5):
            results.append(
                {
                    "appId": "com.fake.%s.app%d" % (slug, i),
                    "title": "%s uygulamasi %d" % (keyword.title(), i + 1),
                    "score": round(rnd.uniform(3.0, 4.9), 2),
                    "genre": "Tools",
                    "developer": "Sahte Yazilim %d" % (i % 3),
                    "installs": "%d+" % (10 ** (4 + i % 4)),
                    "price": 0,
                    "free": True,
                    "currency": "TRY",
                }
            )
        rnd.shuffle(results)
        return results

    def app(self, package_id):
        rnd = random.Random("app|%s|%s" % (package_id, self.seed_day))
        if package_id.endswith(".missing"):
            raise _FakeNotFound(package_id)
        ratings = rnd.randint(50, 500000)
        return {
            "title": "Sahte %s" % package_id.split(".")[-1],
            "developer": "Sahte Yazilim",
            "genre": "Tools",
            "score": round(rnd.uniform(3.0, 4.9), 2),
            "ratings": ratings,
            "reviews": int(ratings * rnd.uniform(0.05, 0.3)),
            "installs": "%d+" % (10 ** rnd.randint(3, 8)),
            "realInstalls": rnd.randint(1000, 50_000_000),
            "price": 0.0,
            "currency": "TRY",
            "free": True,
            "offersIAP": rnd.random() < 0.5,
            "adSupported": rnd.random() < 0.7,
            "version": "%d.%d.%d" % (rnd.randint(1, 9), rnd.randint(0, 9), rnd.randint(0, 20)),
            "updated": int(time.time()) - rnd.randint(0, 300) * 86400,
            "histogram": [rnd.randint(10, 999) for _ in range(5)],
        }


class _FakeNotFound(Exception):
    pass


# ---------------------------------------------------------------- yardimcilar
def polite_sleep(fake):
    if fake:
        return
    time.sleep(random.uniform(*DELAY_RANGE))


def with_retry(label, fn, arg, source, fake):
    """3 deneme, backoff 8s x deneme. NotFoundError'da retry yok."""
    backoff = 0 if fake else RETRY_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(arg)
        except source.not_found:
            raise Skipped("%s: bulunamadi (kaldirilmis olabilir)" % label)
        except Exception as exc:  # ag hatasi, parse hatasi, ne olursa
            if attempt >= MAX_RETRIES:
                raise Failed("%s: %d denemede basarisiz — %s: %s"
                             % (label, MAX_RETRIES, type(exc).__name__, exc))
            wait = backoff * attempt
            log.warning("%s: deneme %d/%d basarisiz (%s: %s) — %ds sonra tekrar",
                        label, attempt, MAX_RETRIES, type(exc).__name__, exc, wait)
            if wait:
                time.sleep(wait)


def load_keywords():
    if not os.path.exists(KEYWORDS_PATH):
        raise SystemExit("keywords.txt bulunamadi: %s" % KEYWORDS_PATH)
    words = []
    with open(KEYWORDS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in words:
                words.append(line)
    return words


def load_watchlist():
    """Kendi paketlerimiz — her satirda bir paket adi, # ile yorum.

    Dosya yoksa bos liste doner: watchlist tamamen istege bagli, yoklugu
    hicbir komutu kirmaz.
    """
    if not os.path.exists(WATCHLIST_PATH):
        return []
    out = []
    with open(WATCHLIST_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if line and line not in out:
                out.append(line)
    return out


def upsert_app(conn, package_id, today, title=None, developer=None, genre=None, via=None):
    conn.execute(
        """
        INSERT INTO apps (package_id, title, developer, genre, first_seen, last_seen, discovered_via)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(package_id) DO UPDATE SET
            title       = COALESCE(excluded.title, apps.title),
            developer   = COALESCE(excluded.developer, apps.developer),
            genre       = COALESCE(excluded.genre, apps.genre),
            last_seen   = excluded.last_seen,
            -- Paketi yeniden gormek (kelime taramasi ya da basarili detay)
            -- olu damgasini kaldirir: Play'e geri donmus demektir.
            miss_streak = 0,
            dead_since  = NULL
        """,
        (package_id, title, developer, genre, today, today, via),
    )


def mark_missing(conn, package_id, today, korumali=()):
    """Bulunamayan paketin kacirma sayacini artir, esigi gecerse olu damgala.

    Watchlist paketleri damgalanmaz: kapali testteki kendi uygulamamiz Play'de
    gorunmez ama yayina cikinca taranmasi gerekir.

    Zaten olu bir paket yeniden denenip gene bulunamadiysa dead_since bugune
    cekilir; boylece DEAD_RETRY_DAYS penceresi bastan baslar ve olu kayit
    ayda birden fazla denenmez.
    """
    if package_id in korumali:
        conn.execute("UPDATE apps SET miss_streak = miss_streak + 1 WHERE package_id = ?",
                     (package_id,))
        return False
    conn.execute(
        """UPDATE apps
              SET miss_streak = miss_streak + 1,
                  dead_since  = CASE WHEN miss_streak + 1 >= ? THEN ? ELSE dead_since END
            WHERE package_id = ?""",
        (DEAD_AFTER, today, package_id),
    )
    row = conn.execute("SELECT dead_since FROM apps WHERE package_id = ?",
                       (package_id,)).fetchone()
    return bool(row and row[0])


def as_int(value):
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_bool(value):
    return None if value is None else int(bool(value))


def ts_to_text(value):
    """updated alani unix timestamp gelir; okunur ISO tarihe cevir."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


# ---------------------------------------------------------------- collect
def cmd_collect(args):
    fake = args.fake
    today = date.today().isoformat()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = open_db()

    source = FakeSource() if fake else LiveSource()
    conn.execute("INSERT OR REPLACE INTO runs (started_at, note) VALUES (?, ?)",
                 (started_at, "mod=%s lang=%s country=%s" % (source.name, LANG, COUNTRY)))
    conn.commit()

    counters = {"keywords_ok": 0, "keywords_err": 0, "apps_ok": 0, "apps_err": 0}

    def save_run(finished=None, note_extra=None):
        note = "mod=%s lang=%s country=%s" % (source.name, LANG, COUNTRY)
        if note_extra:
            note += " | " + note_extra
        conn.execute(
            """UPDATE runs SET finished_at=?, keywords_ok=?, keywords_err=?,
                               apps_ok=?, apps_err=?, note=? WHERE started_at=?""",
            (finished, counters["keywords_ok"], counters["keywords_err"],
             counters["apps_ok"], counters["apps_err"], note, started_at),
        )
        conn.commit()

    log.info("=" * 62)
    log.info("Toplama basladi (%s) — gun=%s lang=%s country=%s", source.name, today, LANG, COUNTRY)

    try:
        rank_rows = pass_keywords(conn, source, today, fake, counters, save_run)
        izle = pass_watchlist(conn, today)
        snap_rows = pass_details(conn, source, today, fake, counters, save_run,
                                 args.limit, oncelik=izle)
    except KeyboardInterrupt:
        save_run(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "KESILDI (KeyboardInterrupt)")
        log.warning("Calisma kesildi — o ana kadarki is kaydedildi. Tekrar calistir, kaldigi yerden devam eder.")
        conn.close()
        return 130

    finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_run(finished_at)
    log.info("-" * 62)
    log.info("BITTI  kelime: %d ok / %d hata — %d siralama satiri yazildi",
             counters["keywords_ok"], counters["keywords_err"], rank_rows)
    log.info("       detay : %d ok / %d hata — %d enstantane yazildi",
             counters["apps_ok"], counters["apps_err"], snap_rows)
    log.info("       toplam yeni kayit: %d", rank_rows + snap_rows)
    conn.close()

    # Yedek en sona, baglanti kapandiktan sonra: gunun isi zaten kaydedildi,
    # burada bir aksilik cikarsa toplama basarili sayilir, sadece uyari duser.
    if not args.no_backup:
        try:
            yol, boyut, silinen, _ = backup_db(BACKUP_KEEP)
            log.info("       yedek: %s (%s, %d eski kusak silindi)",
                     os.path.basename(yol), human_size(boyut), silinen)
        except SystemExit as exc:
            log.error("Yedek alinamadi: %s", exc)
        except Exception:
            log.exception("Yedek alinamadi")
    return 0


def pass_keywords(conn, source, today, fake, counters, save_run):
    """1. gecis: kelime taramasi -> rankings + apps."""
    keywords = load_keywords()
    done = {r[0] for r in conn.execute(
        "SELECT DISTINCT keyword FROM rankings WHERE captured_on=? AND country=? AND lang=?",
        (today, COUNTRY, LANG))}
    todo = [k for k in keywords if k not in done]

    log.info("[1/2] Kelime taramasi: %d kelime, %d tanesi bugun zaten alinmis, %d yapilacak",
             len(keywords), len(done & set(keywords)), len(todo))
    if not todo:
        log.info("[1/2] Tum kelimeler bugun zaten alinmis — atlaniyor.")
        counters["keywords_ok"] += len(done & set(keywords))
        save_run()
        return 0

    written = 0
    for idx, kw in enumerate(todo, 1):
        try:
            results = with_retry("search(%s)" % kw, source.search, kw, source, fake)
        except Skipped as exc:
            counters["keywords_err"] += 1
            log.warning("[1/2] %s", exc)
            save_run()
            polite_sleep(fake)
            continue
        except Failed as exc:
            counters["keywords_err"] += 1
            log.error("[1/2] %s", exc)
            save_run()
            polite_sleep(fake)
            continue

        rows = 0
        for rank, item in enumerate(results or [], 1):
            pkg = item.get("appId")
            if not pkg:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO rankings
                   (captured_on, keyword, country, lang, rank, package_id, title, score)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (today, kw, COUNTRY, LANG, rank, pkg, item.get("title"), item.get("score")),
            )
            upsert_app(conn, pkg, today, item.get("title"), item.get("developer"),
                       item.get("genre"), "keyword:%s" % kw)
            rows += 1
        counters["keywords_ok"] += 1
        written += rows
        conn.commit()
        save_run()
        log.info("[1/2] %3d/%d  %-22s %2d sonuc", idx, len(todo), kw, rows)
        polite_sleep(fake)

    return written


def pass_watchlist(conn, today):
    """1.5. gecis: watchlist paketlerini `apps` tablosuna kaydet.

    Neden ayri bir cekme dongusu degil: kendi uygulamalarimiz `apps` icine
    girdigi anda detay gecisinin ZATEN idempotent olan yolundan gecerler —
    ayni INSERT OR REPLACE, ayni save_texts, ayni hata yonetimi. Burada tek
    yapilan onlari kayda sokmak; cekme isini pass_details `oncelik` listesiyle
    ustlenir ve `--limit` ne kadar kucuk olursa olsun onlari once alir.

    Boylece kendi paketlerimiz, hicbir anahtar kelimede siralanmasalar bile
    (yayinda olmayanlar dahil) her kosuda taranir ve arsivlenir. Yayinda
    olmayan paket detay gecisinde NotFoundError -> Skipped verir: uyari
    yazilir, kosu devam eder.

    Sema degismez, yeni tablo yok. Ayni paket zaten kayitliysa upsert sadece
    last_seen'i tazeler; discovered_via ilk kesif degerinde kalir.
    """
    izle = load_watchlist()
    if not izle:
        log.info("[1.5] watchlist.txt bos ya da yok — kendi paket taramasi atlandi.")
        return []
    yeni = [p for p in izle if not conn.execute(
        "SELECT 1 FROM apps WHERE package_id=?", (p,)).fetchone()]
    for pkg in izle:
        upsert_app(conn, pkg, today, via="watchlist")
    conn.commit()
    log.info("[1.5] Watchlist: %d paket izleniyor, %d tanesi arsive yeni eklendi%s",
             len(izle), len(yeni), (" (%s)" % ", ".join(yeni)) if yeni else "")
    return izle


def pass_details(conn, source, today, fake, counters, save_run, limit, oncelik=()):
    """2. gecis: bugun enstantanesi olmayan paketler -> snapshots.

    Siralama, --limit ile kesilen kosularda KIMIN ilerledigini belirler:

      1. oncelik (watchlist)  — kendi paketlerimiz her kosuda mutlaka cekilir
      2. son enstantane ASC   — NULL (hic cekilmemis) once, sonra en bayati
      3. last_seen DESC, id   — esitlik bozucu

    Eskiden birinci anahtar `a.last_seen DESC` idi. Kelime taramasi her gun
    goren tum uygulamalarin last_seen'ini ayni gune esitledigi icin bu alan
    fiilen sabittir ve siralama `a.package_id`'ye, yani ALFABEYE duser. Sonuc:
    `--limit N` her kosuda alfabetik ilk N uygulamayi secer; listenin kuyrugu
    hicbir zaman sira almaz. Bayatlik anahtari bunu donguye cevirir — her kosu
    en uzun suredir dokunulmamis uygulamalari ilerletir.
    """
    oncelik = [p for p in dict.fromkeys(oncelik)]
    onc_sql = ""
    if oncelik:
        onc_sql = "CASE WHEN a.package_id IN (%s) THEN 0 ELSE 1 END, " % \
                  ",".join("?" * len(oncelik))
    sql = """
        SELECT a.package_id FROM apps a
        LEFT JOIN snapshots s
               ON s.package_id = a.package_id AND s.captured_on = ?
        WHERE s.package_id IS NULL
          -- Olu kayitlar (Play'den kalkmis uygulamalar) her kosuda bir
          -- "bulunamadi" hatasi uretmesin diye atlanir; DEAD_RETRY_DAYS
          -- gecince bir kez yeniden denenir, geri donmusse dirilirler.
          AND (a.dead_since IS NULL OR a.dead_since <= date(?, ?))
        ORDER BY %s
                 (SELECT MAX(x.captured_on) FROM snapshots x
                   WHERE x.package_id = a.package_id) ASC,
                 a.last_seen DESC, a.package_id
    """ % onc_sql
    # Parametre sirasi SQL metnindeki gorunme sirasidir: once LEFT JOIN'in
    # gunu, sonra olu yeniden deneme penceresi, sonra ORDER BY oncelik listesi.
    params = [today, today, "-%d day" % DEAD_RETRY_DAYS] + oncelik
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    pending = [r[0] for r in conn.execute(sql, params)]

    total_apps = conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
    have_today = conn.execute("SELECT COUNT(*) FROM snapshots WHERE captured_on=?",
                              (today,)).fetchone()[0]
    olu_sayi = conn.execute("SELECT COUNT(*) FROM apps WHERE dead_since IS NOT NULL").fetchone()[0]
    log.info("[2/2] Detay taramasi: %d uygulama kayitli, %d tanesinin bugun enstantanesi var, %d cekilecek%s%s%s",
             total_apps, have_today, len(pending),
             " (--limit %d)" % limit if limit is not None else "",
             " — %d watchlist paketi onceliklendirildi" % len(oncelik) if oncelik else "",
             " — %d olu kayit atlandi" % olu_sayi if olu_sayi else "")
    if not pending:
        log.info("[2/2] Bugun eksik uygulama yok — atlaniyor.")
        save_run()
        return 0

    written = 0
    for idx, pkg in enumerate(pending, 1):
        try:
            info = with_retry("app(%s)" % pkg, source.app, pkg, source, fake)
        except Skipped as exc:
            counters["apps_err"] += 1
            olu = mark_missing(conn, pkg, today, korumali=oncelik)
            conn.commit()
            log.warning("[2/2] %s%s", exc,
                        " — OLU damgalandi, bundan sonra atlanacak" if olu else "")
            save_run()
            polite_sleep(fake)
            continue
        except Failed as exc:
            counters["apps_err"] += 1
            log.error("[2/2] %s", exc)
            save_run()
            polite_sleep(fake)
            continue

        hist = info.get("histogram")
        conn.execute(
            """INSERT OR REPLACE INTO snapshots
               (package_id, captured_on, title, score, ratings, reviews, installs,
                real_installs, price, currency, free, offers_iap, ad_supported,
                version, last_updated, genre, developer, histogram)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pkg, today, info.get("title"), info.get("score"),
                as_int(info.get("ratings")), as_int(info.get("reviews")),
                info.get("installs"), as_int(info.get("realInstalls")),
                info.get("price"), info.get("currency"), as_bool(info.get("free")),
                as_bool(info.get("offersIAP")), as_bool(info.get("adSupported")),
                info.get("version"), ts_to_text(info.get("updated")),
                info.get("genre"), info.get("developer"),
                json.dumps(hist, ensure_ascii=False) if hist is not None else None,
            ),
        )
        save_texts(conn, pkg, today, info)
        upsert_app(conn, pkg, today, info.get("title"), info.get("developer"), info.get("genre"))
        counters["apps_ok"] += 1
        written += 1
        conn.commit()
        save_run()
        if idx % 10 == 0 or idx == len(pending):
            log.info("[2/2] %d/%d enstantane yazildi (son: %s)", idx, len(pending), pkg)
        polite_sleep(fake)

    return written


def save_texts(conn, pkg, today, info):
    """Baslik/ozet/aciklamayi SADECE degistiginde sakla.

    Her gun tam metin yazmak ~2400 uygulama x ~4KB = gunde ~10MB eder, yilda
    3+ GB. Degisim bazli yazimda taban maliyet bir kerelik, sonrasi sadece
    metnini guncelleyen uygulamalar kadar. Yan fayda: "rakip aciklamasini ne
    zaman degistirdi" sorusu dogrudan cevaplanir hale gelir.
    """
    title = info.get("title") or ""
    summary = info.get("summary") or ""
    desc = info.get("description") or ""
    h = hashlib.sha1(("%s\x00%s\x00%s" % (title, summary, desc)).encode("utf-8")).hexdigest()
    son = conn.execute("""SELECT text_hash FROM app_texts WHERE package_id=?
                          ORDER BY captured_on DESC LIMIT 1""", (pkg,)).fetchone()
    if son and son[0] == h:
        return False
    conn.execute("""INSERT OR REPLACE INTO app_texts
                    (package_id, captured_on, title, summary, description, text_hash)
                    VALUES (?,?,?,?,?,?)""", (pkg, today, title, summary, desc, h))
    return True


# ---------------------------------------------------------------- yedek
def backup_paths():
    """Mevcut yedekler, eskiden yeniye.

    Siralama ADA gore DEGIL mtime'a gore: ayni saniye carpismasinda eklenen
    "-2" soneki ad siralamasini bozar ("...49-2.db" < "...49.db", cunku '-'
    '.'den kucuktur) ve rotasyon en yeni yedegi en eski sanip silebilir.
    """
    if not os.path.isdir(BACKUP_DIR):
        return []
    yollar = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
              if f.startswith(BACKUP_PREFIX) and f.endswith(BACKUP_SUFFIX)]
    return sorted(yollar, key=lambda p: (os.path.getmtime(p), p))


def backup_db(keep=BACKUP_KEEP):
    """data.db'nin tutarli bir yedegini al, dogrula, eski kusaklari dondur.

    `cp data.db` GUVENLI DEGIL: veritabani WAL modunda ve toplama kosusu her
    uygulamadan sonra commit ediyor; duz kopya, yaziminin ortasina denk gelip
    yarim islem iceren bir dosya uretebilir (ustelik -wal yanindan alinmazsa
    en son gunler eksik cikar). SQLite'in online backup API'si bunun icin var:
    kopyayi sayfa sayfa, kaynak uzerinde okuma kilidi tutarak alir ve tek
    dosyalik, tutarli bir veritabani uretir — kosu suruyorken bile.

    Yedek once gecici ada yazilir, PRAGMA integrity_check ile ACILABILIRLIGI
    dogrulanir, ancak ondan sonra nihai adina tasinir. Boylece klasorde asla
    yarim/bozuk bir yedek durmaz. Dogrulama basarisiz olursa gecici dosya
    silinir ve hata firlatilir; ESKI yedekler dondurulmez — elde her zaman
    saglam bir kusak kalir.

    Doner: (yol, boyut, silinen_eski_sayisi)
    """
    if not os.path.exists(DB_PATH):
        raise SystemExit("Veritabani yok, yedeklenecek bir sey yok: %s" % DB_PATH)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    damga = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = os.path.join(BACKUP_DIR, "%s%s%s" % (BACKUP_PREFIX, damga, BACKUP_SUFFIX))
    # Ayni saniye icinde iki yedek alinirsa (elle kosu + cron) ad carpisir ve
    # bir onceki kusak sessizce ezilirdi; -2, -3 diye ayristir.
    n = 1
    while os.path.exists(final):
        n += 1
        final = os.path.join(BACKUP_DIR, "%s%s-%d%s"
                             % (BACKUP_PREFIX, damga, n, BACKUP_SUFFIX))
    tmp = final + ".partial"
    for art in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(art):
            os.remove(art)

    src = sqlite3.connect(DB_PATH, timeout=60)
    try:
        dest = sqlite3.connect(tmp)
        try:
            src.backup(dest)
            # Yedek tek dosya olarak dursun: WAL basligi kopyalandigi icin
            # aksi halde yaninda -wal/-shm uretir.
            dest.execute("PRAGMA journal_mode=DELETE")
            dest.commit()
        finally:
            dest.close()
    finally:
        src.close()

    # Dogrulama: yedek gercekten acilabiliyor ve saglam mi?
    kontrol = sqlite3.connect(tmp, timeout=60)
    try:
        sonuc = kontrol.execute("PRAGMA integrity_check").fetchone()[0]
        satir = kontrol.execute("SELECT COUNT(*) FROM rankings").fetchone()[0]
    except Exception as exc:
        kontrol.close()
        os.remove(tmp)
        raise SystemExit("Yedek acilamadi, atildi: %s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            kontrol.close()
        except Exception:
            pass
    if sonuc != "ok":
        os.remove(tmp)
        raise SystemExit("Yedek integrity_check'ten gecemedi (%s) — atildi." % sonuc)

    os.replace(tmp, final)

    silinen = 0
    if keep and keep > 0:
        # `final` asla silinmez: rotasyonun az once alinan yedegi atmasi
        # (bir kez oldu) sessizce elde hicbir taze kusak birakmaz.
        for yol in backup_paths()[:-keep]:
            if os.path.abspath(yol) == os.path.abspath(final):
                continue
            os.remove(yol)
            silinen += 1
    return final, os.path.getsize(final), silinen, satir


def cmd_backup(args):
    yol, boyut, silinen, satir = backup_db(args.keep)
    print("Yedek alindi : %s" % yol)
    print("Boyut        : %s" % human_size(boyut))
    print("Dogrulama    : PRAGMA integrity_check = ok  (rankings %d satir)" % satir)
    print("Rotasyon     : %d kusak saklaniyor, %d eski yedek silindi"
          % (args.keep, silinen))
    print("-" * 62)
    print("Klasordeki yedekler (eskiden yeniye):")
    for y in backup_paths():
        print("  %-28s %s" % (os.path.basename(y), human_size(os.path.getsize(y))))
    return 0


# ---------------------------------------------------------------- stats
def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def cmd_stats(args):
    if not os.path.exists(DB_PATH):
        print("Veritabani yok: %s" % DB_PATH)
        return 1
    conn = open_db()
    q = lambda sql, p=(): conn.execute(sql, p).fetchone()

    apps = q("SELECT COUNT(*) FROM apps")[0]
    snaps = q("SELECT COUNT(*) FROM snapshots")[0]
    ranks = q("SELECT COUNT(*) FROM rankings")[0]
    kws = q("SELECT COUNT(DISTINCT keyword) FROM rankings")[0]
    r_days = q("SELECT MIN(captured_on), MAX(captured_on), COUNT(DISTINCT captured_on) FROM rankings")
    s_days = q("SELECT MIN(captured_on), MAX(captured_on), COUNT(DISTINCT captured_on) FROM snapshots")

    size = os.path.getsize(DB_PATH)
    for ext in ("-wal", "-shm"):
        if os.path.exists(DB_PATH + ext):
            size += os.path.getsize(DB_PATH + ext)

    print("=" * 62)
    print("ASO ARSIV OZETI   %s" % DB_PATH)
    print("=" * 62)
    print("Uygulama (apps)       : %d" % apps)
    print("Enstantane (snapshots): %d" % snaps)
    print("Siralama (rankings)   : %d" % ranks)
    print("Takip edilen kelime   : %d (keywords.txt: %d)" % (kws, len(load_keywords())))
    print("Siralama gun araligi  : %s .. %s  (%d gun)" % (r_days[0] or "-", r_days[1] or "-", r_days[2]))
    print("Enstantane gun arligi : %s .. %s  (%d gun)" % (s_days[0] or "-", s_days[1] or "-", s_days[2]))
    print("DB boyutu             : %s" % human_size(size))
    olu = conn.execute("""SELECT package_id, title, dead_since FROM apps
                           WHERE dead_since IS NOT NULL
                           ORDER BY dead_since DESC, package_id""").fetchall()
    if olu:
        print("Olu kayit (atlanan)   : %d" % len(olu))
        for pkg, title, since in olu[:10]:
            print("    %s  %s  (%s)" % (since, pkg, kisalt(title or "-", 34)))
        if len(olu) > 10:
            print("    ... +%d tane daha" % (len(olu) - 10))

    print("-" * 62)
    print("Gun basina siralama satiri (son 7 gun):")
    rows = conn.execute("""SELECT captured_on, COUNT(*), COUNT(DISTINCT keyword)
                           FROM rankings GROUP BY captured_on
                           ORDER BY captured_on DESC LIMIT 7""").fetchall()
    if not rows:
        print("  (kayit yok)")
    for day, cnt, kwcount in rows:
        print("  %s  %6d satir  %3d kelime" % (day, cnt, kwcount))

    print("-" * 62)
    print("Son 5 calisma:")
    rows = conn.execute("""SELECT started_at, finished_at, keywords_ok, keywords_err,
                                  apps_ok, apps_err, note
                           FROM runs ORDER BY started_at DESC LIMIT 5""").fetchall()
    if not rows:
        print("  (calisma kaydi yok)")
    for st, fi, ko, ke, ao, ae, note in rows:
        durum = "bitti  " if fi else "YARIM  "
        print("  %s  %s kelime %d/%d hata  uygulama %d/%d hata" % (st, durum, ko, ke, ao, ae))
        print("      -> %s%s" % ("bitis " + fi if fi else "bitis yok", "  [%s]" % note if note else ""))
    conn.close()
    return 0


# ---------------------------------------------------------------- backfill-check
def cmd_backfill_check(args):
    if not os.path.exists(DB_PATH):
        print("Veritabani yok: %s" % DB_PATH)
        return 1
    conn = open_db()
    days = [r[0] for r in conn.execute("SELECT DISTINCT captured_on FROM rankings ORDER BY captured_on")]
    if not days:
        print("rankings tablosu bos — henuz hic toplama yapilmamis.")
        conn.close()
        return 1

    first = date.fromisoformat(days[0])
    last = date.today()
    have = set(days)
    missing = []
    d = first
    while d <= last:
        if d.isoformat() not in have:
            missing.append(d.isoformat())
        d += timedelta(days=1)

    expected_kw = len(load_keywords())
    print("=" * 62)
    print("BACKFILL KONTROLU   %s .. %s (bugun)" % (days[0], last.isoformat()))
    print("=" * 62)
    print("Veri olan gun : %d" % len(days))
    print("EKSIK gun     : %d" % len(missing))
    if missing:
        for m in missing:
            print("  ! %s  (veri yok — geri doldurulamaz)" % m)
    else:
        print("  Eksik gun yok.")

    # Beklenen kelime sayisi gun gun hesaplanir: bir kelime, arsivde ilk goruldugu
    # gunden once "eksik" sayilmaz. Yoksa listeye yeni kelime eklenince butun
    # gecmis gunler sonsuza dek "eksik kelimeli" gorunurdu.
    ilk_gun = dict(conn.execute("SELECT keyword, MIN(captured_on) FROM rankings GROUP BY keyword"))
    kisik = []
    for day, k in conn.execute("""SELECT captured_on, COUNT(DISTINCT keyword)
                                  FROM rankings GROUP BY captured_on ORDER BY captured_on"""):
        beklenen = sum(1 for g in ilk_gun.values() if g <= day)
        if k < beklenen:
            kisik.append((day, k, beklenen))
    print("-" * 62)
    print("Eksik kelimeli gunler (bugun beklenen %d kelime):" % expected_kw)
    if not kisik:
        print("  Yok — her gun tam.")
    for day, k, beklenen in kisik:
        print("  ~ %s  sadece %d kelime (%d eksik)" % (day, k, beklenen - k))

    conn.close()
    return 2 if missing else 0


# ---------------------------------------------------------------- compare
def kisalt(s, n):
    s = s if s else "-"
    return s if len(s) <= n else s[:n - 2] + ".."


def bin_ayrac(n):
    if n is None:
        return "-"
    return "{:,}".format(int(n)).replace(",", ".")


def pick_days(conn, day1, day2):
    """Karsilastirilacak iki gunu sec. Varsayilan: veri olan son iki gun."""
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT captured_on FROM rankings ORDER BY captured_on")]
    if not days:
        raise SystemExit("rankings tablosu bos — karsilastirilacak gun yok.")
    if day1 and day2:
        eski, yeni = sorted([day1, day2])
    elif day1:
        eski, yeni = sorted([day1, days[-1]])
    else:
        if len(days) < 2:
            raise SystemExit("Sadece tek gun var (%s) — karsilastirma icin en az 2 gun gerekli."
                             % days[0])
        eski, yeni = days[-2], days[-1]
    if eski == yeni:
        raise SystemExit("Iki gun ayni (%s) — farkli gunler ver." % eski)
    for d in (eski, yeni):
        if d not in days:
            raise SystemExit("Bu gunun siralama verisi yok: %s\nMevcut gunler: %s"
                             % (d, ", ".join(days)))
    return eski, yeni


def load_ranks(conn, day, only_kw=None):
    """{kelime: {paket: (rank, baslik)}} — ayni paket birden fazla siradaysa en iyisi."""
    sql = "SELECT keyword, package_id, rank, title FROM rankings WHERE captured_on=?"
    params = [day]
    if only_kw:
        sql += " AND keyword=?"
        params.append(only_kw)
    out = {}
    for kw, pkg, rank, title in conn.execute(sql, params):
        bucket = out.setdefault(kw, {})
        if pkg not in bucket or rank < bucket[pkg][0]:
            bucket[pkg] = (rank, title)
    return out


def cmd_compare(args):
    if not os.path.exists(DB_PATH):
        print("Veritabani yok: %s" % DB_PATH)
        return 1
    conn = open_db()
    eski, yeni = pick_days(conn, args.gun1, args.gun2)
    lim = args.limit

    old = load_ranks(conn, eski, args.keyword)
    new = load_ranks(conn, yeni, args.keyword)

    print("=" * 78)
    print("KARSILASTIRMA   %s  ->  %s" % (eski, yeni))
    print("=" * 78)

    # --- kapsam: eksik kelime varsa hareketler yaniltici olur, once onu soyle
    say = lambda d, day: conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT keyword), COUNT(DISTINCT package_id) "
        "FROM rankings WHERE captured_on=?", (day,)).fetchone()
    for etiket, day in (("eski", eski), ("yeni", yeni)):
        satir, kws, pkgs = say(None, day)
        print("  %s %s : %5d satir  %3d kelime  %4d farkli uygulama"
              % (day, etiket.ljust(4), satir, kws, pkgs))
    if args.keyword:
        print("  ! sadece '%s' kelimesi karsilastiriliyor; ustteki sayilar gun geneli."
              % args.keyword)
    ortak_kw = sorted(set(old) & set(new))
    tek_eski = sorted(set(old) - set(new))
    tek_yeni = sorted(set(new) - set(old))
    if tek_eski or tek_yeni:
        print("  ! Kelime kapsami esit degil — karsilastirma sadece ortak %d kelime uzerinden."
              % len(ortak_kw))
        if tek_eski:
            print("    sadece %s: %s" % (eski, ", ".join(kisalt(k, 20) for k in tek_eski[:8])))
        if tek_yeni:
            print("    sadece %s: %s" % (yeni, ", ".join(kisalt(k, 20) for k in tek_yeni[:8])))

    # --- hareketler
    movers, girenler, cikanlar, zirve = [], [], [], []
    for kw in ortak_kw:
        o, n = old[kw], new[kw]
        for pkg, (r, t) in n.items():
            if pkg in o:
                d = o[pkg][0] - r          # pozitif = yukselmis
                if d:
                    movers.append((d, kw, pkg, t, o[pkg][0], r))
            else:
                girenler.append((r, kw, pkg, t))
        for pkg, (r, t) in o.items():
            if pkg not in n:
                cikanlar.append((r, kw, pkg, t))
        o1 = min(o.items(), key=lambda x: x[1][0])
        n1 = min(n.items(), key=lambda x: x[1][0])
        if o1[0] != n1[0]:
            zirve.append((kw, o1[1][1], n1[1][1]))

    def blok(baslik, rows, bicim, bos="  (yok)"):
        print("-" * 78)
        print(baslik)
        if not rows:
            print(bos)
            return
        for r in rows:
            print(bicim(r))

    yukselen = sorted([m for m in movers if m[0] > 0], key=lambda x: -x[0])[:lim]
    dusen = sorted([m for m in movers if m[0] < 0], key=lambda x: x[0])[:lim]
    fmt_m = lambda m: "  %+4d  %-22s %-32s %2d -> %2d" % (
        m[0], kisalt(m[1], 22), kisalt(m[3], 32), m[4], m[5])

    blok("EN COK YUKSELENLER (sira farki, + = yukari)", yukselen, fmt_m)
    blok("EN COK DUSENLER", dusen, fmt_m)

    girenler.sort(key=lambda x: x[0])
    cikanlar.sort(key=lambda x: x[0])
    blok("LISTEYE YENI GIRENLER (toplam %d)" % len(girenler), girenler[:lim],
         lambda g: "  #%-3d  %-22s %s" % (g[0], kisalt(g[1], 22), kisalt(g[3], 34)))
    blok("LISTEDEN CIKANLAR (toplam %d) — eski sirasi" % len(cikanlar), cikanlar[:lim],
         lambda c: "  #%-3d  %-22s %s" % (c[0], kisalt(c[1], 22), kisalt(c[3], 34)))
    blok("ZIRVE DEGISEN KELIMELER (toplam %d)" % len(zirve), zirve[:lim],
         lambda z: "  %-22s %s  ->  %s" % (kisalt(z[0], 22), kisalt(z[1], 24), kisalt(z[2], 24)))

    # --- enstantane metrikleri
    snap = conn.execute("""
        SELECT s2.package_id, s2.title,
               s1.score, s2.score, s1.ratings, s2.ratings,
               s1.real_installs, s2.real_installs
        FROM snapshots s1 JOIN snapshots s2 ON s1.package_id = s2.package_id
        WHERE s1.captured_on=? AND s2.captured_on=?""", (eski, yeni)).fetchall()
    print("-" * 78)
    print("ENSTANTANE (iki gunde de olcumu olan %d uygulama)" % len(snap))
    if snap:
        yorum = [(int((b or 0) - (a or 0)), pkg, t, a, b)
                 for pkg, t, _, _, a, b, _, _ in snap if (b or 0) != (a or 0)]
        # puani 0/None olan uygulama "0.000 -> 5.000" gibi sahte sicrama uretir
        puan = [(round(b - a, 3), pkg, t, a, b)
                for pkg, t, a, b, _, _, _, _ in snap if a and b and b != a]
        kur = [(int((b or 0) - (a or 0)), pkg, t, a, b)
               for pkg, t, _, _, _, _, a, b in snap if (b or 0) != (a or 0)]
        blok("  En cok yorum/puanlama alanlar", sorted(yorum, key=lambda x: -x[0])[:lim],
             lambda y: "    +%-9s %-34s %s -> %s"
                       % (bin_ayrac(y[0]), kisalt(y[2], 34), bin_ayrac(y[3]), bin_ayrac(y[4])),
             "    (degisim yok)")
        blok("  En cok kurulum artisi", sorted(kur, key=lambda x: -x[0])[:lim],
             lambda k: "    +%-11s %-34s %s -> %s"
                       % (bin_ayrac(k[0]), kisalt(k[2], 34), bin_ayrac(k[3]), bin_ayrac(k[4])),
             "    (degisim yok)")
        artan = sorted([p for p in puan if p[0] > 0], key=lambda x: -x[0])[:5]
        azalan = sorted([p for p in puan if p[0] < 0], key=lambda x: x[0])[:5]
        blok("  Puani en cok oynayanlar", artan + azalan,
             lambda p: "    %+.3f     %-34s %.3f -> %.3f"
                       % (p[0], kisalt(p[2], 34), p[3] or 0, p[4] or 0),
             "    (degisim yok)")

    yeni_app = conn.execute("SELECT COUNT(*) FROM apps WHERE first_seen=?", (yeni,)).fetchone()[0]
    print("-" * 78)
    print("OZET  yukselen %d / dusen %d / sabit %d  |  giren %d / cikan %d  |  zirve degisimi %d"
          % (len([m for m in movers if m[0] > 0]), len([m for m in movers if m[0] < 0]),
             sum(len(set(old[k]) & set(new[k])) for k in ortak_kw) - len(movers),
             len(girenler), len(cikanlar), len(zirve)))
    print("      arsive ilk kez giren uygulama: %d" % yeni_app)
    conn.close()
    return 0


# ---------------------------------------------------------------- text-analysis
TR_FOLD = str.maketrans("şŞıİğĞüÜöÖçÇ", "sSiIgGuUoOcC")


def fold(s):
    """Turkce karakterleri ASCII'ye indir — keywords.txt ASCII, basliklar degil."""
    return (s or "").translate(TR_FOLD).lower()


def son_metinler(conn, day):
    """{paket: (baslik, ozet, aciklama)} — gune kadarki EN SON metin kaydi."""
    rows = conn.execute("""
        SELECT t.package_id, t.title, t.summary, t.description
        FROM app_texts t
        WHERE t.captured_on = (SELECT MAX(x.captured_on) FROM app_texts x
                               WHERE x.package_id = t.package_id AND x.captured_on <= ?)
    """, (day,)).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def cmd_text_analysis(args):
    if not os.path.exists(DB_PATH):
        print("Veritabani yok: %s" % DB_PATH)
        return 1
    conn = open_db()
    day = args.gun or conn.execute(
        "SELECT MAX(captured_on) FROM rankings").fetchone()[0]
    if not day:
        print("rankings bos.")
        return 1
    metin = son_metinler(conn, day)
    kws = [k[0] for k in conn.execute(
        "SELECT DISTINCT keyword FROM rankings WHERE captured_on=? ORDER BY keyword", (day,))]
    if args.keyword:
        kws = [k for k in kws if k == args.keyword]
        if not kws:
            print("Bu gun bu kelime yok: %s" % args.keyword)
            return 1

    print("=" * 82)
    print("METIN ANALIZI   %s" % day)
    print("=" * 82)
    pkg_gun = [r[0] for r in conn.execute(
        "SELECT DISTINCT package_id FROM rankings WHERE captured_on=?", (day,))]
    kapsam = sum(1 for p in pkg_gun if p in metin)
    print("  siralanan uygulama: %d   metni olan: %d (%.0f%%)"
          % (len(pkg_gun), kapsam, 100.0 * kapsam / max(len(pkg_gun), 1)))
    if kapsam < len(pkg_gun):
        print("  ! metni olmayan uygulamalar asagidaki metin bolumlerinde sayilmaz.")

    def top(kw, n):
        return conn.execute("""SELECT k.rank, k.package_id, s.title, s.real_installs, s.ratings
                               FROM rankings k LEFT JOIN snapshots s
                                 ON s.package_id=k.package_id AND s.captured_on=k.captured_on
                               WHERE k.captured_on=? AND k.keyword=? AND k.rank<=?
                               ORDER BY k.rank""", (day, kw, n)).fetchall()

    # ---------------------------------------------------------- 1) kendi uygulamalarimiz
    izle = load_watchlist()
    print("-" * 82)
    print("1) KENDI UYGULAMALARIMIZ (watchlist.txt: %d paket)" % len(izle))
    if not izle:
        print("  watchlist.txt bos — kendi paketlerini ekle.")
    for pkg in izle:
        satir = conn.execute("""SELECT keyword, rank FROM rankings
                                WHERE captured_on=? AND package_id=? ORDER BY rank""",
                             (day, pkg)).fetchall()
        t = metin.get(pkg)
        ad = (t[0] if t else None) or pkg
        print("\n  %s  (%s)" % (ad, pkg))
        if not t:
            print("    ! metin kaydi yok — bir toplama kosusu sonrasi dolar.")
        if not satir:
            print("    hicbir kelimede siralanmiyor.")
        for kw, rank in satir:
            ust = top(kw, 5)
            oran = lambda f: 100.0 * sum(1 for r in ust if r[1] in metin
                                         and fold(kw) in fold(metin[r[1]][f])) / max(len(ust), 1)
            bende = lambda f: "VAR" if t and fold(kw) in fold(t[f]) else "yok"
            print("    #%-3d %-24s baslikta:%-4s (ilk5 %3.0f%%)  ozette:%-4s (ilk5 %3.0f%%)"
                  % (rank, kisalt(kw, 24), bende(0), oran(0), bende(1), oran(1)))
        # baslikta gecen ama siralanmadigi kelimeler — en somut kacirilmis firsat
        if t:
            var = set(k for k, _ in satir)
            kacan = [k for k in kws if k not in var and fold(k) in fold(t[0])]
            if kacan:
                print("    ! basligimizda gectigi halde SIRALANMADIGIMIZ kelimeler:")
                for k in kacan:
                    n = conn.execute("""SELECT COUNT(*) FROM rankings
                                        WHERE captured_on=? AND keyword=?""", (day, k)).fetchone()[0]
                    print("        %-26s (listede %d uygulama var)" % (k, n))

    # ---------------------------------------------------------- 2) kazanilabilir kelimeler
    print("-" * 82)
    print("2) KAZANILABILIR KELIMELER — ilk 5'te kurulumu %s altinda uygulama olanlar"
          % bin_ayrac(args.zayif_esik))
    bulgu = []
    for kw in kws:
        ust = top(kw, 5)
        zayif = [r for r in ust if (r[3] or 0) < args.zayif_esik]
        if zayif:
            bulgu.append((len(zayif), kw, ust))
    bulgu.sort(key=lambda x: -x[0])
    if not bulgu:
        print("  Yok — tum kelimelerin ilk 5'i guclu.")
    for n, kw, ust in bulgu[:args.limit]:
        print("  %-26s ilk5'te %d zayif  | en ust zayif: %s"
              % (kisalt(kw, 26), n,
                 kisalt(next(r[2] for r in ust if (r[3] or 0) < args.zayif_esik), 30)))
    if len(bulgu) > args.limit:
        print("  ... toplam %d kelime (--limit ile artir)" % len(bulgu))

    # ---------------------------------------------------------- 3) metin degisiklikleri
    print("-" * 82)
    print("3) BUGUN METNINI DEGISTIREN UYGULAMALAR")
    degisen = conn.execute("""
        SELECT t.package_id, t.title, t.summary, t.description
        FROM app_texts t
        WHERE t.captured_on = ?
          AND EXISTS (SELECT 1 FROM app_texts x WHERE x.package_id=t.package_id
                      AND x.captured_on < ?)""", (day, day)).fetchall()
    if not degisen:
        print("  Yok. (Ilk gun tum metinler taban olarak yazilir, degisim sayilmaz.)")
    for pkg, ti, su, de in degisen[:args.limit]:
        onc = conn.execute("""SELECT title, summary, description FROM app_texts
                              WHERE package_id=? AND captured_on<? 
                              ORDER BY captured_on DESC LIMIT 1""", (pkg, day)).fetchone()
        nerede = []
        if onc[0] != ti:
            nerede.append("BASLIK: %s -> %s" % (kisalt(onc[0], 24), kisalt(ti, 24)))
        if onc[1] != su:
            nerede.append("OZET degisti")
        if onc[2] != de:
            nerede.append("aciklama %+d karakter" % (len(de or "") - len(onc[2] or "")))
        print("  %-34s %s" % (kisalt(ti, 34), "; ".join(nerede)))

    # ---------------------------------------------------------- 4) sira kovasi metrikleri
    print("-" * 82)
    print("4) SIRA KOVASI METRIKLERI (ilk5 vs 6-10) — fark yoksa metin ayirt edici degildir")
    print("     kova     n  aciklama  ozet  kw-tekrar  ilk170'te%  ozette%  baslikta%")
    kova = {"1-5": [], "6-10": []}
    for kw in kws:
        for r in top(kw, 10):
            t = metin.get(r[1])
            if not t:
                continue
            d, s_, ti = t[2] or "", t[1] or "", t[0] or ""
            kova["1-5" if r[0] <= 5 else "6-10"].append(dict(
                uz=len(d), oz=len(s_), tk=fold(d).count(fold(kw)),
                b170=1 if fold(kw) in fold(d)[:170] else 0,
                ozk=1 if fold(kw) in fold(s_) else 0,
                bak=1 if fold(kw) in fold(ti) else 0))
    for k in ("1-5", "6-10"):
        v = kova[k]
        if not v:
            continue
        m = lambda f: sum(x[f] for x in v) / float(len(v))
        print("     %-6s %4d %9.0f %5.0f %10.1f %10.0f%% %7.0f%% %9.0f%%"
              % (k, len(v), m("uz"), m("oz"), m("tk"), 100 * m("b170"),
                 100 * m("ozk"), 100 * m("bak")))
    if not kova["1-5"]:
        print("     (metin verisi yok — once bir toplama kosusu gerekiyor)")
    conn.close()
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="ASO veri toplayici (Google Play)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="gunluk toplama")
    c.add_argument("--fake", action="store_true", help="aga cikma, sahte veri uret, beklemeleri sifirla")
    c.add_argument("--limit", type=int, default=None, metavar="N", help="en fazla N uygulama detayi cek")
    c.add_argument("--no-backup", dest="no_backup", action="store_true",
                   help="kosu sonunda otomatik yedek alma")
    c.set_defaults(func=cmd_collect)

    y = sub.add_parser("backup", help="data.db'nin tutarli yedegini al (online backup API)")
    y.add_argument("--keep", type=int, default=BACKUP_KEEP, metavar="N",
                   help="saklanacak kusak sayisi (varsayilan %d)" % BACKUP_KEEP)
    y.set_defaults(func=cmd_backup)

    s = sub.add_parser("stats", help="veritabani ozeti")
    s.set_defaults(func=cmd_stats)

    b = sub.add_parser("backfill-check", help="rankings tablosunda eksik gun var mi")
    b.set_defaults(func=cmd_backfill_check)

    k = sub.add_parser("compare", help="iki gunu karsilastir (varsayilan: son iki gun)")
    k.add_argument("gun1", nargs="?", default=None, metavar="GUN1",
                   help="YYYY-AA-GG — tek verilirse son gunle karsilastirilir")
    k.add_argument("gun2", nargs="?", default=None, metavar="GUN2")
    k.add_argument("--limit", type=int, default=15, metavar="N",
                   help="her listede en fazla N satir (varsayilan 15)")
    k.add_argument("--keyword", default=None, metavar="KELIME",
                   help="sadece bu kelimeyi karsilastir")
    k.set_defaults(func=cmd_compare)

    ta = sub.add_parser("text-analysis", help="baslik/aciklama analizi + kazanilabilir kelimeler")
    ta.add_argument("gun", nargs="?", default=None, metavar="GUN",
                    help="YYYY-AA-GG (varsayilan: son toplama gunu)")
    ta.add_argument("--keyword", default=None, metavar="KELIME", help="sadece bu kelime")
    ta.add_argument("--limit", type=int, default=20, metavar="N", help="liste uzunlugu (varsayilan 20)")
    ta.add_argument("--zayif-esik", dest="zayif_esik", type=int, default=10000, metavar="N",
                    help="bu kurulumun altindaki uygulama 'zayif' sayilir (varsayilan 10000)")
    ta.set_defaults(func=cmd_text_analysis)

    args = ap.parse_args()
    setup_logging()
    try:
        return args.func(args)
    except Exception:
        log.exception("Beklenmeyen hata — calisma durdu")
        return 1


if __name__ == "__main__":
    sys.exit(main())
