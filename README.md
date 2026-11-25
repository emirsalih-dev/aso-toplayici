# ASO Toplayici

> **2026-11-26'dan beri GitHub Actions'ta calisiyor** (GCP faturasi kapatilinca VM durdu).
> - Is: `.github/workflows/collect.yml`, gunde 3 kosu (TR 09:07/13:07/17:07), her biri en fazla 3000 uygulama detayi.
> - Arsiv: `db` release'indeki en yeni `data-*.db.gz` (son 3 kusak saklanir). Indir: `gh release download db -p 'data-*'`.
> - Son kosu: `son-kosu.txt`, ozet: `ozet.txt`.
> - Kelime/izleme listesi degisikligi: `keywords.txt` / `watchlist.txt` dosyasini duzenle, push et.
> - Asagidaki VM/WSL anlatimi tarihcedir.


Google Play'de 40 anahtar kelimenin **gunluk siralama arsivini** biriktirir.

> Bu arsiv sonradan satin alinamaz. Kacirilan gun geri doldurulamaz — 1 Eylul'de
> toplamadiysan, 1 Eylul verisi artik yok. Bu yuzden tek onemli sey: her gun calismasi.

## Nerede calisiyor (2026-10-14'ten beri: VM)

Toplayici artik **GCP VM `borsa-bot`** uzerinde (`us-central1-a`, proje
`borsa-beyni-bot-2609`, `~/aso-collector`, cron + guard.sh orada). Sebep: PC'deki
WSL kapaninca cron da duruyordu — 5 Ekim'den sonra hicbir kosu bitmedi, 7 gun
kalici eksik. VM'den ve ev IP'sinden ayni 6 kelime icin Play aramasi alindi:
ilk 10 birebir ayni (VM'den gelen sonuc guvenilir).

- Saglik ozeti: `aso-vm ./aso-durum.sh` (`eesy-gunluk aso` bunu cagirir)
- Herhangi bir komut: `aso-vm python3 collect.py stats`
- Arsivi yerele cekmek: `aso-pull` -> `~/aso-collector/vm-kopya.db`
- **Bu yerel klasor artik arsivdir, orada toplama calismaz.** Yerel crontab satirlari
  `# [VM] ...` ile kapatildi; yedek `~/.local/state/crontab.yedek.20261014-aso`.
- Kod degisikligi yapinca VM'e de kopyala (`gcloud compute scp`), yoksa iki kopya ayrisir.

## Kurulum

Gereken tek bagimlilik `google-play-scraper` (saf Python, kendi bagimliligi yok).

```bash
python3 -m pip install --user google-play-scraper
```

Bu makinede pip yoktu, wheel dogrudan kullanici site-packages'ina acildi:
`~/.local/lib/python3.12/site-packages/google_play_scraper` (surum 1.2.7).
Yeniden kurmak gerekirse ayni yontem:

```bash
cd /tmp && curl -sO https://files.pythonhosted.org/packages/33/f7/a23ef3cf8efc9ab3aee565971f59906811e6ce95475314ef7b18d02f30ba/google_play_scraper-1.2.7-py3-none-any.whl
python3 -c "import zipfile;zipfile.ZipFile('/tmp/google_play_scraper-1.2.7-py3-none-any.whl').extractall('$HOME/.local/lib/python3.12/site-packages')"
python3 -c "import google_play_scraper; print('ok')"
```

Baska kurulum adimi yok; veritabani ilk calismada kendi olusur.

## Kullanim

```bash
python3 collect.py collect                # gunluk toplama (cron bunu calistirir)
python3 collect.py collect --limit 800    # sadece 800 uygulama detayi cek (sure sinirli gunler icin)
python3 collect.py collect --fake         # aga cikmadan sahte veriyle test
python3 collect.py collect --no-backup    # kosu sonunda otomatik yedek alma
python3 collect.py backup                 # elle tutarli yedek (backups/ altina)
python3 collect.py backup --keep 10       # 10 kusak sakla (varsayilan 5)
python3 collect.py stats                  # arsiv ozeti
python3 collect.py backfill-check         # eksik gun var mi (cikis kodu 2 = eksik var)
python3 collect.py compare                # son iki gunu karsilastir
python3 collect.py compare 2026-09-04 2026-09-05   # belirli iki gun
python3 collect.py compare --keyword zikirmatik    # tek kelime
python3 collect.py compare --limit 30     # her listede 30 satir (varsayilan 15)
python3 collect.py text-analysis          # baslik/aciklama analizi + kazanilabilir kelimeler
python3 collect.py text-analysis --zayif-esik 1000   # daha dar 'zayif' tanimi
python3 collect.py text-analysis --keyword zikirmatik
```

Ayarlar `collect.py` basinda: `LANG`, `COUNTRY`, `SEARCH_HITS`, `DELAY_RANGE`,
`MAX_RETRIES`, `RETRY_BACKOFF`.

**DELAY_RANGE'i dusurme.** Istekler arasi 1.4–3.2 sn bekleme, engellenmemenin
tek garantisi. Toplama yavas olmali; nasil olsa gece calisiyor.

## Nasil calisir

Iki gecis, arasinda kucuk bir kayit adimi:

1. **Kelime taramasi** — her kelime icin `search(kw, n_hits=50)`. Sonuclar sira
   numarasiyla `rankings`'e yazilir, her uygulama `apps`'e eklenir.
   (Play her sorguda 50 sonuc vermez; "tesbih" icin 28 dondu. Normal.)
2. **Watchlist kaydi** — `watchlist.txt`'teki kendi paketlerimiz `apps`'e
   yazilir (ag istegi yok). Boylece hicbir kelimede siralanmasalar bile detay
   gecisinin normal yolundan gecerler. Bkz. [Watchlist](#watchlist-kendi-paketlerimiz).
3. **Detay taramasi** — `apps` icinde bugun enstantanesi olmayan her paket icin
   `app(pkg)`, sonuc `snapshots`'a yazilir. Sonunda `backups/` altina bir yedek
   dusulur (`--no-backup` ile kapatilir).

### Detay gecisinin sirasi — neden onemli

Detay gecisi su sirayla secer:

1. **watchlist paketleri** — `--limit` ne kadar kucuk olursa olsun once bunlar,
2. **son enstantanesi en eski olan** — hic cekilmemisler (NULL) en basta,
3. `last_seen DESC`, `package_id` — esitlik bozucu.

Ikinci anahtar bir hata duzeltmesi. Eskiden birinci anahtar `last_seen DESC`
idi; ama kelime taramasi o gun gordugu **tum** uygulamalarin `last_seen`'ini
ayni gune esitledigi icin bu alan fiilen sabit ve siralama `package_id`'ye,
yani **alfabeye** dusuyordu. Sonuc: `--limit N` her kosuda alfabetik ilk N
uygulamayi seciyor, listenin kuyrugu hicbir zaman sira almiyordu.

Gercek arsiv uzerinde 5 gunluk simulasyon (`--limit 200`, 2463 uygulama):

| | gun 1 ve gun 2 ayni mi | 5 gunde kapsanan | hic dokunulmayan |
|---|---|---|---|
| eski siralama | **evet** (her gun ayni 200) | 200 (%8) | 2263 |
| yeni siralama | hayir | 1000 (%41) | 1463 |

Yani `--limit` artik bir *dongu*: her kosu en uzun suredir dokunulmamis
uygulamalari ilerletir.

Onemli davranislar:

- **Idempotent** — ayni gun ikinci kez calistirmak cift kayit uretmez; tarih
  birincil anahtarin parcasi ve yazmalar `INSERT OR REPLACE`.
- **Devam edebilir** — yarim kalan calisma tekrar baslatilinca kaldigi yerden
  surer. Bugun alinmis kelimeler atlanir; detayda LEFT JOIN ile eksikler secilir.
- **Her adimda commit** — calisma ortasinda kesilse bile o ana kadarki is diskte.
- **Hata calismayi oldurmez** — tek uygulama hatasi loglanir, dongu devam eder.
- **Retry** — 3 deneme, 8s x deneme bekleme. `NotFoundError`'da retry yok:
  uygulama muhtemelen magazadan kaldirilmis, uyarilip gecilir.
- **Log** — hem `collect.log` dosyasina hem stdout'a, saat damgali.

Ilk gerçek calisma uzun surer: ~40 kelime + bulunan her uygulamanin detayi
(1000–1500 paket beklenir) x ~2.3 sn = **1 saat civari**. Sonraki gunlerde
uygulama havuzu ayni kaldigi icin sure benzer kalir. Cok uzarsa `--limit` ile
detay gecisini kis; **kelime gecisini asla kisma**, asil deger orada.

## Metin arsivi (app_texts) ve text-analysis

`app_texts` tablosu baslik/ozet/aciklamayi **sadece degistiginde** saklar.
Her gun tam metin yazmak 2400 uygulama x ~4KB = gunde ~10MB, yilda 3+ GB
ederdi; degisim bazli yazimda taban maliyet bir kerelik. Ek ag istegi yok —
metin zaten `collect` sirasinda cekilen `app()` cevabinda geliyordu, kaydedilmiyordu.

Asil kazanc: "rakip aciklamasini ne zaman degistirdi, sonrasinda sirasi ne
oldu" sorusu cevaplanabilir hale gelir. Bu ancak veri biriktikce ise yarar.

`text-analysis` dort bolum basar:

1. **Kendi uygulamalarimiz** (`watchlist.txt`) — siralandigi kelimeler, o
   kelimenin baslikta/ozette olup olmadigi, ilk 5'in ayni orani. Ayrica
   *basligimizda gectigi halde siralanmadigimiz* kelimeler.
2. **Kazanilabilir kelimeler** — ilk 5'inde kurulumu esigin altinda uygulama
   olan kelimeler. Yeni bir uygulamanin girebilecegi yerler.
3. **Bugun metnini degistiren uygulamalar** — rakip ASO hamleleri.
4. **Sira kovasi metrikleri** — ilk5 vs 6-10 metin istatistikleri.

2026-09-05 kesitinde 4. bolumde ilk5 ile 6-10 arasinda anlamli fark **cikmadi**;
aciklamadaki kelime tekrari da siralamayi aciklamiyor (bir kelimede #1'in
aciklamasinda kelime hic gecmezken #2'de 11 kez geciyordu). Kurulum sayisi da
uzun kuyrukta belirleyici degil: `dijital tesbih` listesinde 18 kurulumlu bir
uygulama #3, 13 milyon kurulumlu bir uygulama #22 idi. Tek gunluk kesitten
nedensellik cikmaz — dogru yontem tek bir seyi degistirip gunluk arsivde
haftalarca izlemek. Olcum gurultusu +-1-2 sira, anlamli sinyal ~5 sira ustu.

## Watchlist — kendi paketlerimiz

`watchlist.txt`: her satirda bir paket adi, `#` ile yorum. Paket adlari ilgili
projenin `android/app/build.gradle.kts` dosyasindaki `applicationId`'den alinir
— tahmin edilmez.

```
com.eesydev.zikirmatik3dtesbih     # Zikirmatik: Dijital Tesbih — yayinda
com.eesydev.sajda                  # Sajda — Play kapali test
com.aracdefterim.arac_defterim     # Garajim — Play Console bekliyor
com.eesydev.fis_takip              # Fisci — kapali test (Alpha)
com.eesydev.teklif90               # Teklif90 — gelistirme (yayinda degil)
```

Bu paketler **her kosuda** taranip arsivlenir: kelime siralamalarina girsinler
ya da girmesinler, gunluk `snapshots` + degistiginde `app_texts` kaydi duser.
Sebep, arsivin genel mantiginin aynisi — kendi uygulamamizin yayin gunune
kadarki gecmisi de sonradan satin alinamaz. Yayin gunu elde hazir bir taban
olur.

Detay gecisinde **onceliklidirler**: `--limit 12` ile kosulan bir testte 12
paketin ilk 5'i watchlist paketleriydi. Yayinda olmayan paket hata degildir —
Play `NotFoundError` doner, uyari loglanir, kosu devam eder (`teklif90` bu
durumda). Yeni tablo yok, sema degismedi; watchlist paketleri `apps` icine
`discovered_via='watchlist'` ile girer ve mevcut idempotent yoldan gecer.

## Olu kayitlar — Play'den kalkan uygulamalar

Kesfedilen bir uygulama Play'den kaldirildiginda `apps` icinde kalir ama artik
cekilemez: her kosuda bir `bulunamadi (kaldirilmis olabilir)` uyarisi ve bir
`apps_err` uretir. Tek bir olu kayit bile gunluk raporu kirletir — hata
sayaci sifirdan farkli oldugu surece gercek arizalar goze batmaz.

Cozum `apps` tablosundaki iki alan:

| Alan | Ne yapar |
|---|---|
| `miss_streak` | Ust uste kac kez bulunamadi. Basarili cekimde ya da kelime taramasinda yeniden gorulunce **sifirlanir** |
| `dead_since` | `miss_streak >= DEAD_AFTER` (3) olunca damgalanir; detay gecisi bu paketleri atlar |

Uc guvenlik supabi var:

- **Watchlist muaf.** Kendi paketlerimiz yayinlanmadigi icin bulunamaz; onlar
  olu degil "henuz dogmamis" sayilir, hicbir zaman damgalanmaz.
- **Dirilme.** Paket kelime taramasinda yeniden gorunurse `upsert_app` damgayi
  kaldirir — Play'e geri donen uygulama kendiliginden takibe girer.
- **Yeniden deneme.** Olu kayit `DEAD_RETRY_DAYS` (30) gun sonra bir kez daha
  denenir. Gene bulunamazsa damga tazelenir, yani ayda birden fazla denenmez.

`stats` komutu olu kayitlari listeler. Kolonlar ilk acilista `migrate()` ile
eklenir; goc aninda, detay gecisinin gercekten calistigi (`apps_ok > 0`) son 3
gunun hicbirinde enstantanesi olmayan paketler geriye donuk damgalanir.

## Yedek

```bash
python3 collect.py backup             # 5 kusak sakla (varsayilan)
python3 collect.py backup --keep 10
```

Yedekler repo **icinde** `backups/` altinda durur ve `.gitignore`'dadir —
tek klasoru kopyalamak arsivi tasimaya yetsin diye disarida degil, ama
`data.db` gibi surum kontrolune de girmez.

`cp data.db` **guvenli degil**: veritabani WAL modunda ve toplama kosusu her
uygulamadan sonra commit ediyor; duz kopya yazimin ortasina denk gelip yarim
islem iceren bir dosya uretebilir, ustelik `-wal` yanindan alinmazsa son gunler
eksik cikar. Bunun yerine SQLite'in **online backup API**'si
(`sqlite3.Connection.backup`) kullaniliyor: kopyayi sayfa sayfa, kaynak
uzerinde okuma kilidi tutarak alir — kosu suruyorken bile tutarli, tek dosyalik
bir veritabani uretir (`journal_mode=DELETE`'e cevrilir ki yaninda `-wal`
dosyasi tasimasin).

Davranislar:

- **Once dogrula, sonra yerlestir** — yedek gecici `.partial` adina yazilir,
  `PRAGMA integrity_check` ile acilabilirligi dogrulanir, ancak ondan sonra
  nihai adina tasinir. Klasorde asla yarim/bozuk bir yedek durmaz.
- **Basarisizlikta eski kusaklar korunur** — dogrulama gecmezse gecici dosya
  silinir ve rotasyon **calismaz**; elde her zaman saglam bir kusak kalir.
- **Rotasyon mtime'a gore** — ad siralamasina gore degil. Ayni saniyede iki
  yedek alinirsa ada `-2` soneki eklenir ve `"...49-2.db" < "...49.db"` oldugu
  icin ad siralamasi en yeni yedegi en eski sanip silebilirdi.
- **Kosu sonunda otomatik** — `collect` bittikten ve baglanti kapandiktan sonra.
  Gunun isi zaten kaydedilmistir; yedek hatasi kosuyu basarisiz saymaz, sadece
  log'a hata duser.

## Tablolar

| Tablo | Ne tutar |
|---|---|
| `apps` | Kesfedilen her paket: baslik, gelistirici, kategori, ilk/son gorulme, hangi kelimeyle bulundu (`keyword:...` ya da `watchlist`), olu takibi (`miss_streak`, `dead_since`) |
| `snapshots` | Uygulama basina **gunluk** enstantane: puan, oy/yorum sayisi, kurulum, surum, reklam/IAP, histogram |
| `rankings` | **Asil deger.** Gun x kelime x sira -> hangi uygulama kacinci sirada |
| `app_texts` | Baslik/ozet/aciklama — **sadece degistiginde** yeni satir |
| `runs` | Her calisma: baslangic/bitis, ok/hata sayaclari. `finished_at` bossa o calisma yarim kalmis |

`rankings` PK'si `(captured_on, keyword, country, lang, rank)`, `snapshots` PK'si
`(package_id, captured_on)` — cift kayit bu yuzden imkansiz.

## Haftalik kontrol (5 dakika)

Her pazartesi:

```bash
cd /home/esyzc/aso-collector
python3 collect.py backfill-check     # eksik gun cikarsa cron calismamis demektir
python3 collect.py stats              # gun basina satir sayisi sabit mi
tail -40 collect.log                  # hata/uyari var mi
ls -la backups/                       # son yedek taze mi (kosu sonunda dusuyor)
```

Bakilacaklar:

- **Eksik gun** — cron calismamis. Makine kapali miydi? WSL kapali miydi?
- **Gun basina satir sayisi dustu** — Play engellemeye baslamis olabilir; log'da
  arka arkaya `search(...) 3 denemede basarisiz` gorursen 1–2 gun bekle,
  `DELAY_RANGE`'i yukari cek (asagi degil).
- **Son calisma "YARIM"** — bitmeden kesilmis. Elle `python3 collect.py collect`
  calistir, kaldigi yerden devam eder.

## Ornek sorgu — 7 gunluk siralama degisimi

```sql
WITH bugun AS (SELECT package_id, rank, title FROM rankings
               WHERE keyword='tesbih' AND captured_on=date('now','localtime')),
     onceki AS (SELECT package_id, rank FROM rankings
                WHERE keyword='tesbih' AND captured_on=date('now','localtime','-6 days'))
SELECT b.rank, b.title, o.rank AS onceki, (o.rank-b.rank) AS degisim
FROM bugun b LEFT JOIN onceki o USING(package_id) ORDER BY b.rank LIMIT 20;
```

`degisim` pozitifse uygulama yukselmis. `onceki` NULL ise ilk 50'ye yeni girmis.

**`localtime` sart.** SQLite'ta `date('now')` UTC dondurur, Turkiye UTC+3'tur ve
`collect.py` gunu yerel saate gore damgalar. `localtime` yazmazsan gece 00:00-03:00
arasinda calistirdigin her sorgu sessizce bir onceki gunu gosterir. Ayni sebeple
toplama saatini de 00:00-03:00 araligina koyma.

## Zamanlama

Kurulu cron (`crontab -l`):

```cron
17 13 * * * flock -n .collect.lock python3 collect.py collect >> cron.log 2>&1
17 20 * * * flock -n .collect.lock python3 collect.py collect >> cron.log 2>&1
@reboot     sleep 60 && guard.sh
7 9-20 * * * guard.sh
```

(Gercek satirlarda tam yollar var; ozet icin kisaltildi.)

- **13:17** birincil kosu. Tam saatlerden kacin — o kalabaliktan uzak dur.
- **20:17** emniyet agi. Gun zaten alindiysa 0 kayit yazip cikar.
- **guard.sh** bekcisi: makine acilisinda ve 09-20 arasi saat basi "bugunun
  verisi tam mi" diye bakar, eksikse toplamayi baslatir. 2026-09-05'te iki
  slotta da makine kapali oldugu icin gun neredeyse kaciriliyordu; bekci tam
  bunun icin var. Uykudan uyanma ve gec acilis durumlarini da kurtarir.

Bekci `flock` kilidini cron isleriyle paylasir, iki toplama ust uste binmez.
Pencere 09:00-20:59 ile sinirli: tam kosu ~2s20dk suruyor ve gun damgasi
baslangictaki yerel gune gore atildigi icin gece yarisi civari baslatmak
UTC/yerel gun kaymasina denk gelir.

Bekcinin "eksik" karari: kelime taramasi yarim kalmissa, ya da uygulamalarin
%2'sinden fazlasinin bugunku enstantanesi yoksa. Tolerans, Play'den kaldirilmis
(kalici olarak cekilemeyen) birkac uygulama yuzunden her saat bosuna kosu
baslamasin diye.
