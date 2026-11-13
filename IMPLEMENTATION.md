# IMPLEMENTATION.md — excel-etl

Dokumen hidup: status, rencana per fase, keputusan, temuan, log.
Perbarui setiap selesai satu item (lihat AGENTS.md §7).

## Status

- Fase 1-5 selesai, Fase 6: 6.1 (`tools.py`), 6.2 (`README.md`) dan ringkasan serah (`HANDOVER.md`) selesai. P6 dijalankan tiap fase (Fase 3-6 dikerjakan sendiri, tanpa subagent).
- Temuan P6 Fase 3-4, 3-5 dan 6 sudah dibersihkan (§Temuan P6 ... dibersihkan 2026-11-07); yang sengaja tersisa ada di bagian itu.
- Berikutnya: **serah `HANDOVER.md` ke rekan; 6.3 uji skema tool lewat LiteLLM ke >= 2 provider bersama rekan.**
- **Aturan kerja (dari user): BALANSITAS hanya satu contoh, bukan patokan bentuk.** Node berbasis pola umum dan config eksplisit; tes memakai beberapa bentuk sintetis berbeda; sampel hanya sanity.

## Berikutnya

1. Lihat "Planned" di bawah (plugin coding agent). 6.3 bersama rekan. Setelah itu: temuan lama P6 Fase 1/2 di §Temuan (error cell sudah diperingatkan; `_fit` streaming tersisa).
2. Pertanyaan terbuka di bagian bawah (batas dataset per job, pemilik target schema Snowflake, retensi versi).

## Planned (belum dikerjakan, urut)

**hemat token agent (L1-L5)** (usul 2026-11-13, dari log 16 job nyata): tool yang paling boros adalah
   `save_spec` (33% karakter hasil tool), `node_catalog` (19%, 99 panggilan), `run_pipeline` (7%), dan
   loop error (`unpivot` gagal konversi, "not saved"). Docstring 22 tool hanya ~1k token: tidak dikerjakan.
   L1 `catalog()` ringkas memuat field config + `node_catalog("a,b")`; L2 hasil `save_spec` hanya schema step
   yang baru/berubah; L3 `RunReport.text(full=False)` untuk tool (gagal: hanya node gagal + schema inputnya;
   peringatan identik dirangkum); L4 petunjuk untuk error konversi/kolom hilang di `engine.py`;
   L5 tool `edit_spec(patch_json, note)`: ganti/tambah/hapus step lewat jalur simpan yang sama dengan
   `save_spec`. Ukuran: satu tes skenario membandingkan karakter hasil tool.

**target-driven `synth`** (usul 2026-11-12): S0-S4 selesai (lihat Log). Lanjut S5 (belajar
   `col:` -> glossary + jalur `auto` bila `no_spec`), lalu S6 opsional (tanya user dulu).
   Rencana lengkap di §Rencana target-driven.

(Contoh ekspresi di katalog dan node `window` selesai 2026-11-08. Fase 7 (rencana muat ke Snowflake, Databricks, BigQuery) selesai 2026-11-09 kecuali uji di akun sungguhan; lihat Log.)

0. **Sumber multi-sheet (`name_regex`)** (usul 2026-11-11, PRIORITAS). Rencana lengkap di
   §Rencana multi-sheet di bawah. Kerjakan fase M1 -> M4 berurutan, satu fase per sesi.
1. **Plugin coding agent** (ingatkan user). Maki: folder terpisah (`integrations/maki/`), tanpa
   menyentuh `excel_etl/`. Jembatan satu file Python (satu tool per proses, state di
   `FileStore`, `file_id` = path di disk), `register_tool` Lua untuk 21 tool agent, perintah slash
   untuk sisi host (`add_file`, `answer`, `approve`, `reload_job`) agar agent tidak bisa
   menyetujui sendiri. Terukur: tiap panggilan sekitar 0,7 s (impor duckdb 0,26 s); bila terasa,
   pertimbangkan proses yang tetap hidup. Perlu diputuskan: hanya Maki atau banyak agent
   (server tipis), dan di mana batas `AGENTS.md` ("bukan MCP server") berlaku: untuk `excel_etl/`.
2. **Tool pembangun spec (spec builder)** (usul 2026-11-11, dari uji etl-e2e-lab). Masalah: agent
   menulis seluruh JSON spec sendiri lalu `save_spec`; model lemah salah nama field, menyalin nama
   dari contoh, salah bentuk `header_anchor`, tiap salah = satu putaran gagal (maks 5). Usul: tool
   bertahap yang mengubah DRAF spec di state (bukan versi): `spec_add_source(region_id, name)`
   (isi `sheet`, `header_anchor`, kolom dan tipe dari region yang terdeteksi, bukan dari ketikan
   agent), `spec_add_step(id, node, inputs, config_json)` (config dicek langsung lewat skema node
   dan `infer_schema`, error menunjuk field), `spec_add_output(name, input, format)`,
   `spec_add_check(...)`, `spec_remove(part, id)`, `spec_draft()` (lihat draf + skema keluaran
   tiap step), `spec_commit(note)` = `save_spec` atas draf. `save_spec(spec_json)` tetap ada
   (model kuat, edit massal). Batas: tool hanya merakit dan memeriksa; keputusan isi tetap dari
   agent (jangan ada tool "buat spec otomatis"). Cek selesai: tes alur builder menghasilkan spec
   = `golden_spec.json` lab; tes setiap kesalahan umum (field salah, input tak dikenal, kolom tak
   ada) ditolak per langkah dengan pesan yang menunjuk field; uji live etl-e2e-lab dengan model
   non-frontier: jumlah `save_spec` gagal turun dibanding tanpa builder (catat angka sebelum dan
   sesudah). Perlu diputuskan: draf disimpan di `JobState` (ikut versi state) atau terpisah;
   jumlah tool agent naik 22 -> ~28 (cek batas tool provider pada 6.3).
3. **Menunggu pihak lain:** 6.3 (uji skema lewat LiteLLM, bersama rekan), tes di Linux
   (CI Ubuntu sudah dipasang), uji muat di akun Snowflake, Databricks dan BigQuery sungguhan
   (SQL belum pernah dijalankan di platform aslinya), pemilik schema target.

## Rencana target-driven (`synth`) — S0-S4 selesai 2026-11-12, lanjut S5

Ditulis untuk dikerjakan model mana pun, satu fase per sesi. Baca seluruh bagian ini dulu.
Ragu = tanya user, jangan menebak (AGENTS.md §7).

### Masalah
User ingin menyatakan **data hasil yang diinginkan** (kolom, tipe, grain) lewat form, lalu
sistem membuat spec sendiri **tanpa agent LLM**. Sekarang spec hanya lahir dari agent (chat)
atau ketik JSON. `auto.py` hanya menjalankan spec yang sudah ada.

### Tujuan (perilaku yang diminta)
1. User mengisi **Target** (form di lab, bukan JSON): daftar kolom keluaran + grain + aturan.
2. `synthesize(target, workbook)` (deterministik, tanpa LLM) mencari region, mencocokkan kolom,
   merakit beberapa kandidat spec, menjalankannya, memberi skor, dan mengembalikan yang terbaik
   + alasan tiap langkah + pratinjau.
3. Bila ragu (dua kolom cocok mirip, tak ada kandidat lolos) -> **pertanyaan form** (pilih
   salah satu), bukan agent. Jawaban dikirim balik sebagai `answers`, synth diulang.
4. Spec yang disetujui di-publish ke folder specs yang sama; `auto` memakainya seperti biasa.
5. Agent hanya cadangan terakhir, di sisi lab (host), dengan konteks sempit. Library tidak
   pernah memanggil LLM.

### Keputusan (sudah diputuskan, jangan diubah tanpa tanya user)
- Satu modul baru `excel_etl/synth.py`. Tidak ada dependency baru (fuzzy = `difflib` stdlib).
- `synth` HANYA merakit spec dari node yang sudah ada lalu memanggil `validate`/`run` yang ada.
  Tidak ada jalur eksekusi kedua. Spec hasil = `PipelineSpec` biasa (bisa dibaca/diubah agent).
- Urutan langkah kanonik, tetap (membatasi ruang pencarian):
  `source -> fill_down -> drop_rows -> unpivot|unpivot_groups -> rename -> cast -> filter -> select`.
  Langkah yang tak perlu dilewati. Tidak ada join di versi 1.
- Kandidat region hanya role `source`/`mixed` dari proposal (derived TIDAK pernah dipilih).
- Bentuk long/wide TIDAK disimpan sebagai field: diturunkan dari ada/tidaknya kolom target
  `from_header` (state minimal).
- Ekspresi filter dari user lewat `sqlexpr` (batas trust sama dengan input agent).
- Generik: tidak ada nama kolom/sheet BALANSITAS di kode atau aturan tebakan
  (lihat §Status, aturan user). Pola total/subtotal = regex yang bisa diganti di Target.

### Kontrak (tulis persis ini, nama publik stabil)

```python
# excel_etl/synth.py
class TargetColumn(_Model):  # _Model = extra="forbid"
    name: Ident  # nama kolom keluaran
    type: Literal["text", "int", "float", "date", "datetime", "bool"]
    hints: list[str] = []  # teks header lain yang boleh cocok ("Qty", "Jumlah")
    from_header: bool = False  # nilai diambil dari LABEL header (bulan, pabrik), bukan sel
    required: bool = True


class Target(_Model):
    name: Ident  # jadi nama output + nama spec saat publish
    columns: list[TargetColumn] = Field(min_length=1)
    grain: list[str] = Field(min_length=1)  # subset nama columns; satu baris = satu kombinasi
    total_regex: str = r"(?i)^\s*(sub\s*)?(total|jumlah|grand total)\b"
    filters: list[str] = []  # ekspresi SQL skalar, dicek sqlexpr
    # validator: grain ⊆ columns; nama unik (casefold); maks 1 kolom from_header per
    # unpivot sederhana; kolom nilai (bukan grain, bukan from_header) >= 1 bila ada from_header;
    # total_regex compile; tiap filter lolos sqlexpr.


class Question(_Model):
    id: str  # deterministik: "col:<target col>" / "region"
    text: str
    options: list[str]  # header asli / region id


class Candidate(_Model):
    region: str  # "F1.S3.R1"
    spec: str  # PipelineSpec JSON (dump_spec)
    score: float  # 0..1
    reasons: list[str]  # satu kalimat per langkah, untuk user
    problems: list[str]  # kenapa skor turun
    preview: list[dict] = []  # <= 20 baris output, hanya kandidat terbaik


class SynthReport(_Model):
    status: Literal["ok", "ask", "none"]  # ok = best lolos ambang; ask = ada Question; none = gagal
    best: Candidate | None = None
    others: list[Candidate] = []  # maks 2
    questions: list[Question] = []

    def text(self) -> str: ...


def synthesize(
    target: Target,
    wb: Workbook,
    file_id: str = "F1",
    answers: dict[str, str] | None = None,
    knowledge: Knowledge | None = None,
) -> SynthReport: ...
```

`answers`: `{"col:Qty": "Ton", "region": "F1.S3.R1"}`. Jawaban menang atas skor.
`knowledge.glossary` (istilah -> header) dipakai sebagai hint tambahan.

### Algoritma (deterministik, urutan tetap)
1. **Region kandidat**: refactor `encode._proposals` agar ada fungsi terstruktur
   `proposal_regions(wb, file_id) -> list[Proposal]` (region id, sheet, `Region`, role, pola
   `name_regex` bila sheet berulang). `_proposals` memformat teks DARI fungsi ini (satu sumber
   kebenaran; tes `test_propose.py` harus tetap hijau tanpa diubah).
2. **Kolom region**: `column_names(g, region)` + tipe per kolom (pakai fungsi tipe yang sama
   dengan `source._type`, jangan tulis ulang).
3. **Cocokkan** tiap kolom target non-`from_header` ke header, skor:
   jawaban user = 1.0; casefold sama dengan `name` = 1.0; sama dengan hint/glossary = 0.95;
   leaf header (`A|B` -> `B`) sama = 0.9; `difflib.SequenceMatcher` ratio x 0.8 bila >= 0.75.
   Tipe tak cocok (`type_fits`) -> skor x 0.5. Satu header hanya untuk satu kolom target
   (greedy dari skor tertinggi, tie-break urutan kolom = deterministik).
   **Ragu** bila skor 2 header teratas beda < 0.1 dan keduanya >= 0.6 -> `Question("col:X")`.
4. **from_header**: kolom yang tidak terpakai untuk grain/atribut dan bertipe angka adalah
   kandidat "kolom berulang".
   - Region punya `groups` (ColumnGroup) dan target punya >= 2 kolom nilai -> `unpivot_groups`
     (pattern dari label group, measure = kolom nilai target).
   - Selain itu -> `unpivot` dengan `columns` = kolom berulang, `name_column` = kolom from_header,
     `value_column` = satu kolom nilai target.
5. **fill_down**: kolom grain bertipe text yang punya sel kosong di tengah data (merged / label
   di baris pertama blok). **drop_rows**: `drop_regex = target.total_regex` pada kolom grain
   text pertama, plus `blank=True`. Cek di S0 apakah total_rows region sudah dibuang saat baca;
   bila ya, drop_rows hanya untuk label yang lolos.
6. **rename** header -> nama target; **cast** ke tipe target (`strict=False`); **filter** per
   ekspresi; **select** urutan kolom target. Output `{target.name: {input: <step terakhir>}}`.
   Checks otomatis: `not_null` grain, `unique` grain.
7. **Variasi kandidat** (maks 3 per region, maks 5 region teratas menurut skor pencocokan):
   dengan/tanpa fill_down, unpivot vs unpivot_groups bila keduanya mungkin. Tidak lebih.
8. **Evaluasi**: `validate(spec, {"main": wb})`; gagal -> kandidat dibuang dengan alasan.
   Lolos -> `run` ke `tempfile.TemporaryDirectory()`, baca parquet dengan duckdb:
   `coverage` = kolom target required yang terpetakan / total required;
   `fill` = 1 - rata-rata rasio NULL kolom target (cast gagal = NULL);
**S0 — Verifikasi asumsi (SELESAI 2026-11-12).** Probe `probe_synth.py` sudah dihapus.
- [x] **`read_region` TIDAK membuang `Region.total_rows`.** Pada fixture, `total_rows=(3,)`
  tetapi baris `Total` tetap muncul di relation. `drop_rows` wajib bila pola total cocok.
- [x] Header 2 tingkat menjadi `Plant A|Ton`, `Plant A|Rate`, ...; satu `ColumnGroup` berisi
  `labels=('Ton','Rate')`, `parents=('Plant A','Plant B')`, `period=2`, `count=2`.
- [x] Lima kali `run` + baca parquet di `TemporaryDirectory` berhasil di Windows: 0,367 s total.
- [x] BALANSITAS: proposal 19,016 s (447 region proposal); **budget 15 s BELUM lolos**.
  Pembagian waktu: lineage 4,083 s; `_shape_groups` 10,573 s; semua region 1,541 s (3825 region).
  S1 wajib memakai cache `wb.memo` untuk `proposal_regions` dan membuat `_shape_groups` menyimpan
  hasilnya (sekarang dipanggil ulang tanpa memo). Jangan menghapus grouping multi-sheet.
  Batas S1: panggilan proposal kedua < 1 s; panggilan pertama BALANSITAS diukur ulang, target
  < 15 s. Bila masih > 15 s, catat dan tanya user sebelum optimasi lain.
- [x] `Ident` menerima `hasil`, `hasil_etl`, `Kuantitas`; menolak `Hasil ETL` dan `Tahun-2026`.
  UI menampilkan aturan identifier; jangan mengganti tipe menjadi string bebas.
### Fase (urut; satu fase per sesi; tiap fase: P2-P5 + §6 verifikasi + Log)

**S0 — Verifikasi asumsi (WAJIB, tanpa kode produksi).** Skrip probe (nama `probe_synth.py`,
JANGAN `ast.py`/nama modul stdlib), hapus setelah selesai. Catat jawaban di sini:
- [ ] Apakah `read_region` membuang `Region.total_rows`? (menentukan langkah 5)
- [ ] Bentuk `column_names` untuk header 2 tingkat (`PARENT|LEAF`) dan isi `ColumnGroup`.
- [ ] `run` ke direktori temp lalu baca parquet di Windows: aman? (gotcha 16: unlink dir)
- [ ] Waktu `proposal_regions` + 5 kali `run` pada BALANSITAS (budget: synth < 15 s).
- [ ] Apakah `Ident` cocok untuk nama target dari user (spasi? huruf Indonesia?).

**S1 — Target + pencocokan (tanpa spec) — SELESAI 2026-11-12.** `synth.py`: model Target/TargetColumn/Question,
`proposal_regions` (refactor encode), fungsi `match_columns(target, names, types, answers,
knowledge) -> (mapping, scores, questions)`.
Tes `tests/test_synth.py`: validator Target (grain bukan kolom, nama dobel beda huruf, filter
berbahaya `read_text(...)` ditolak, regex rusak); pencocokan exact/hint/leaf/fuzzy; tipe salah
menurunkan skor; header dipakai sekali; ragu -> Question; jawaban menang; deterministik
(urutan kolom diacak di input -> hasil sama). `test_propose.py` hijau tanpa perubahan.

**S2 — Rakit spec — SELESAI 2026-11-12.** `build_spec(target, proposal, mapping) -> PipelineSpec` mengikuti
langkah 4-6 + `reasons`. Tes: fixture sintetis (lihat bawah) menghasilkan spec yang
`validate` ok; spec fixture (b) setara `etl-e2e-lab/tests/golden_spec.json` secara struktur
(node sama, urutan sama).

**S3 — Evaluasi + skor + `synthesize` — SELESAI 2026-11-12.** Langkah 7-9. Tes per fixture: status yang diharapkan;
fixture derived tak pernah terpilih; fixture ambigu -> `ask` dengan `col:`; dengan `answers`
-> `ok`; skor deterministik (jalankan 2x sama). Sampel BALANSITAS (`-m sample`) hanya sanity:
waktu < 15 s, tidak crash.

**S4 — Lab — SELESAI 2026-11-12.** Endpoint: `POST /jobs/{id}/synth` (bukan `/synth` upload:
file sudah ada di job; `answers` untuk menjawab `col:`), `POST /jobs/{id}/synth/publish`
(spec + target -> `LAB_DATA/specs/<name>.json` + `LAB_DATA/targets/<name>.json`). UI `index.html`
tab "Target":
tabel baris kolom (name, type select, hints, from_header checkbox, grain checkbox, required),
isian total_regex & filters, tombol Jalankan; hasil: reasons, problems, tabel preview, pertanyaan
sebagai `<select>` + tombol Jawab (kirim ulang dengan answers), tombol Publish.
`aria-label` di tiap kontrol dinamis (aksesibilitas). Tes `tests/test_server.py`: alur synth ->
ask -> answer -> ok -> publish -> `/auto` memakai spec itu. Jalankan uvicorn dengan
`--reload --reload-dir lab` (gotcha etl-e2e-lab).

**S5 — Belajar + jalur auto.** Jawaban `col:` disimpan ke `knowledge.glossary` (hint -> header)
lewat host, supaya file berikutnya tidak bertanya lagi. `auto()` tetap seperti sekarang;
lab: bila `auto` = `no_spec` dan ada `targets/*.json`, coba `synthesize` per target, status `ok`
-> run, selain itu tampilkan pertanyaan. Library `auto.py` tidak diubah kecuali user minta.

**S6 — (opsional, tanya user dulu) cadangan agent.** Hanya di lab: status `none`/`ask` yang
tak dijawab -> tombol "Minta agent" yang mengirim Target + 3 kandidat + problems (bukan workbook
utuh) sebagai pesan pertama chat. Ukur token vs alur chat biasa.

### Fixture sintetis (openpyxl, di `tests/test_synth.py`; JANGAN meniru BALANSITAS)
a. Tabel datar rapi: Tanggal | Produk | Qty -> target sama, nama beda huruf.
b. Wide bulan + kolom region merged + baris `Total` (= `messy_report` lab) -> target long
   Region, Item, Month(from_header), Qty.
c. Header 2 tingkat blok berulang `PlantA|Ton, PlantA|Rate, PlantB|Ton ...` -> target
   Tanggal, Plant(from_header), Ton, Rate (unpivot_groups).
d. Dua sheet sama bentuk (`2025`, `2026`) -> satu source `name_regex`.
e. Sheet ringkasan berformula atas sheet (a) -> tidak pernah terpilih.
f. Header ambigu `Qty` dan `Qty Plan`, target `Quantity` -> `ask`.
g. Tidak ada yang cocok -> `none` dengan problems jelas.

### Jangan (jebakan yang sudah pernah terjadi, lihat memory gotchas)
- Jangan tulis kode lewat bash heredoc (terpotong diam-diam). Pakai tool write/edit; setelah
  tulis: `grep -c ""` + ruff.
- `edit_lines`: baca ulang setelah tiap edit; `ruff format` mengubah file -> baca ulang.
- duckdb: `fetchall()[0][0]`, bukan `fetchone()`; urutan baris tak dijamin tanpa ORDER BY.
- Bind kosong tidak mengevaluasi konstanta: skor HARUS dari `run`, bukan `validate` saja.
- Nama kolom beda huruf saja ditolak spec; casefold di pencocokan.
- Jangan tambahkan tool agent baru di `tools.py` untuk fitur ini (bukan permukaan agent).
- Tes yang tidak bisa gagal (`or True`) dilarang; mutasi cek: ubah ambang/bobot, tes harus merah.

### Pertanyaan terbuka (tanyakan user sebelum S4/S6)
- Ambang `ok` 0.85 dan bobot skor: angka awal, sesuaikan setelah S3 dengan data nyata.
- Spec hasil synth perlu approval HITL (gate lab) atau cukup klik Publish?
- Bila dua spec cocok di `auto` (synth vs agent), mana yang menang (sekarang: urutan nama)?

## Rencana multi-sheet (`name_regex`)

Ditulis 2026-11-11 agar bisa dikerjakan model mana pun. Kerjakan SATU fase per sesi, urut
M1 -> M4. Tiap fase: baca bagian fase itu saja + file yang disebut, kerjakan, jalankan "Cek
selesai", centang, isi Log. Jangan lompat fase. Ragu = tanya user.

### Masalah

Workbook harian (contoh nyata: "Stok Pupuk Subsidi Per Kabupaten", 161 sheet) punya satu
sheet per tanggal (`2026-11-08`, `2026-11-07`, ...) dengan tabel berbentuk sama, dan sheet baru
muncul tiap hari. `SheetRef` (`excel_etl/pipeline.py:55`) hanya menunjuk SATU sheet. Akibat
terukur di etl-e2e-lab job `stok_kab`: agent menulis satu source + langkah per sheet, spec
membengkak tiap versi (v1 3 sheet, v3 5 sheet), 22 menit tanpa selesai. Spec seperti itu juga
tidak bisa dipakai `auto.py` untuk file besok (sheet baru tidak ikut).

### Tujuan (perilaku yang diminta)

Satu source membaca SEMUA sheet yang namanya cocok pola, dengan aturan region yang sama, lalu
menumpuknya jadi satu tabel + satu kolom berisi nama sheet asal:

```json
"sources": {"stok": {
  "file_role": "main",
  "sheet": {"name_regex": "[0-9]{4}-[0-9]{2}-[0-9]{2}", "sheet_column": "tanggal"},
  "region": {"header_anchor": ["PROVINSI", "KABUPATEN"], "header_rows": 2}
}}
```

Contoh di atas hanya ilustrasi. ATURAN USER: jangan masukkan nama/kolom file contoh ke kode.

### Keputusan (sudah diputuskan, jangan diubah tanpa tanya user)

| # | Keputusan | Alasan |
|---|---|---|
| K1 | Field baru di `SheetRef`: `name_regex: str \| None`, `sheet_column: str \| None`. | Satu tempat; tidak ada node baru. |
| K2 | `name_regex` dicocokkan dengan `re.fullmatch` pada nama sheet APA ADANYA (tanpa `text_key`). | `$`/`search` menerima sebagian nama; normalisasi membuat pola tak terduga. |
| K3 | `name_regex` TIDAK boleh digabung dengan `name`, `name_like`, `fallback_contains`. `sheet_column` WAJIB bila ada `name_regex` dan DILARANG bila tidak. | Satu cara menemukan sheet per source; state lebih sedikit. |
| K4 | Urutan sheet = urutan di workbook. Urutan baris = per sheet, lalu urutan baris di sheet. | Deterministik (AGENTS §4). |
| K5 | Kolom yang tidak ada di sebagian sheet = NULL (`UNION ALL BY NAME`). Tipe beda antar sheet = tipe bersama duckdb (biasanya VARCHAR). | Tidak ada data hilang, tidak menebak. |
| K6 | Satu sheet cocok-pola yang tabelnya tidak ketemu = GAGAL (run: issue; rebind: drift + daftar sheet). Tidak dilewati diam-diam. | Data hilang tanpa ketahuan lebih buruk dari run gagal. `# ponytail:` opsi `skip_bad_sheets` bila user minta. |
| K7 | 0 sheet cocok = missing (rebind) / issue (run). Jumlah sheet bertambah/berkurang BUKAN drift. | Sheet harian baru = normal. |
| K8 | `sheet_column` bertabrakan dengan nama kolom tabel (casefold) = error yang menyebut keduanya. | duckdb case-insensitive (gotcha 16). |
| K9 | Tidak ada node baru, tidak ada dependency baru, tidak ada perubahan di `auto.py`. | YAGNI; Auto otomatis ikut bisa. |

### Fase M1 — Spec + baca + run (inti) — SELESAI 2026-11-11

File yang disentuh: `excel_etl/pipeline.py`, `excel_etl/nodes/source.py`, `excel_etl/engine.py`,
tes baru `tests/test_multi_sheet.py`. Tidak ada file lain.

1. `pipeline.py` `SheetRef` (baris ~55): tambah dua field (K1). Ubah validator
   `_some_way_to_find_it` agar menegakkan K3, dan `re.compile(name_regex)` (regex rusak =
   `ValueError` dengan pesan berisi pola, sama gaya `ColumnExpect._one_matcher`).
   `sheet_column`: tidak kosong setelah strip, tidak berawalan `__` (dicadangkan, lihat
   `nodes/relops.py:52 check_reserved`), maks 100 char.
2. `nodes/source.py`:
   - `How` tambah `"name_regex"`.
   - Pecah `locate` jadi `_find_sheet` (tetap) + fungsi baru
     `_locate_in(wb, source, sheet: str, how: How) -> Location` (isi badan `locate` saat ini
     sesudah `_find_sheet`). `locate` = `_find_sheet` lalu `_locate_in`. Perilaku lama sama
     persis (semua tes lama hijau).
   - `locate` untuk source dengan `name_regex` harus MENOLAK (`NodeError("... use
     locate_all ...")`), supaya pemanggil tidak diam-diam membaca satu sheet.
   - Fungsi baru `locate_all(wb, source) -> list[Location]`: sheet di `wb.sheets` yang
     `re.fullmatch(name_regex, s.name)`, urut workbook (K4). 0 cocok -> `NodeError` berisi pola
     + `few(names, 8)`. Panggil `_locate_in` per sheet; kumpulkan `NodeError` per sheet; bila ada
     yang gagal -> SATU `NodeError` berisi jumlah + `few([f"{sheet}: {msg}"], 5)` (K6).
     Untuk source biasa, `locate_all` = `[locate(wb, source)]` (satu jalur pemanggil).
   - Fungsi baru `read_locations(locs, source, con, name) -> Relation`:
     - Bila `name_regex` kosong: `read_location(locs[0], ...)` (tanpa kolom sheet).
     - Bila ada: cek K8 terhadap gabungan nama kolom. Baca tiap lokasi ke tabel sementara
       `__{name}_{i}` lewat `read_location`, lalu satu `CREATE TABLE name AS` yang
       `UNION ALL BY NAME` dari `SELECT '<sheet>' AS sheet_column, {i} AS __k,
       row_number() OVER () AS __r, * FROM __{name}_{i}`, `ORDER BY __k, __r`, lalu buang
       `__k`, `__r` (`SELECT * EXCLUDE (...)`). Kolom sheet paling kiri. Nama sheet sebagai
       literal: escape `'` -> `''` (atau parameter). `DROP` tabel sementara di `finally`.
     - Kenapa `__k/__r` + ORDER BY: urutan UNION tidak dijamin (gotcha 16). Jangan dihapus.
3. `engine.py` `_load_sources` (baris ~182): ganti `locate` + `read_location` dengan
   `locate_all` + `read_locations`. `loc.errors` dan `loc.below` -> jumlahkan semua lokasi.
   `NodeStat` untuk source multi: tambahkan    `note=f"{n} sheets: {first}..{last}"` (field `note` ada di `NodeStat`, engine.py:71).
4. `read_region` (source.py:214) ikut lewat `locate_all`/`read_locations`.

Tes `tests/test_multi_sheet.py` (workbook sintetis `make_xlsx` dari `conftest.py`; JANGAN pakai
nama/kolom file contoh; tiap tes satu perilaku; jalankan lewat `engine.run`/`validate` publik):
- 3 sheet cocok (`d1`,`d2`,`d3`) + 1 sheet tak cocok (`notes`): hasil = baris d1 lalu d2 lalu d3,
  kolom pertama = nama sheet.
- Pola `d[0-9]` TIDAK mencocokkan `xd1` maupun `d1x` (fullmatch, K2).
- Sheet cocok dengan kolom ekstra di satu sheet -> kolom itu NULL di sheet lain (K5).
- Kolom angka di satu sheet, teks di sheet lain -> run tetap ok, tipe VARCHAR (K5).
- Satu sheet cocok tanpa header anchor -> run `failed`, issue menyebut nama sheet itu (K6).
- 0 sheet cocok -> run `failed`, pesan menyebut pola (K7).
- `sheet_column` = nama kolom tabel beda huruf besar/kecil -> gagal dengan pesan (K8).
- Validasi spec: `name_regex` + `name` -> SpecError; `name_regex` tanpa `sheet_column` ->
  SpecError; `sheet_column` tanpa `name_regex` -> SpecError; regex rusak `"("` -> SpecError.
- Header 2 baris (`PARENT|LEAF`) di tiap sheet terbaca sama.
- Nama sheet berisi `'` (mis. `d'1`) tidak merusak SQL.
- Urutan stabil: 3 sheet x 3000 baris, hasil urut persis (deteksi bug urutan; gotcha 16).
- Source biasa (tanpa `name_regex`): tes lama `test_engine.py`, `test_read_region.py`,
  `test_rebind.py` tetap hijau TANPA diubah.

Cek selesai M1:
```bash
# di excel-etl/
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```
Lalu mutasi manual (cp backup, ubah, pytest harus GAGAL, cp balik; cek `grep -c` bahwa
mutasi kena): (a) `fullmatch` -> `search`; (b) hapus `ORDER BY __k, __r`; (c) lewati sheet gagal
alih-alih raise. Tiap mutasi harus ditangkap minimal satu tes. Catat hasil di Log.

### Fase M2 — rebind + petunjuk agent — SELESAI 2026-11-11

File: `excel_etl/rebind.py`, `excel_etl/tools.py` (hanya `save_spec` dan docstring),
`excel_etl/nodes/source.py` (bila perlu), `../etl-e2e-lab/lab/prompt.py`,
`../etl-e2e-lab/lab/static/index.html` (satu baris label, ~667). Tes: tambah di
`tests/test_rebind.py` dan `tests/test_tools.py`.

1. `rebind.rebind` (rebind.py:125): pakai `locate_all`. `NodeError` -> `missing` (0 cocok)
   atau `drift` (ada sheet gagal, K6) dengan `diff` berisi daftar sheet gagal. Bedakan dengan
   menangkap jumlah sheet cocok dulu (fungsi kecil `matching_sheets(wb, source)` di
   source.py, dipakai `locate_all` juga, supaya satu sumber kebenaran).
2. `BindRecord` untuk multi: `sheet` = sheet cocok PERTAMA, `header_row`/`first_col` dari
   sheet pertama, `columns` = gabungan nama kolom semua sheet urut kemunculan, field baru
   `sheets: int = 1`. `_judge`: untuk source `name_regex` JANGAN bandingkan nama sheet
   (nama berganti tiap hari = normal); bandingkan header/kolom seperti biasa. Jumlah sheet
   berubah TIDAK dicatat di `diff` sama sekali (K7), agar file harian berstatus `exact`.
3. `tools.save_spec`: setelah validasi ok, bila >= 3 source punya `file_role` sama DAN
   `region.header_anchor` sama (casefold) DAN tanpa `name_regex` -> tambah baris peringatan ke
   `text`: `"N sources read the same table shape from different sheets; one source with
   sheet.name_regex + sheet_column reads them all"`. Peringatan, bukan penolakan.
4. Docstring `SheetRef` / bagian spec di `node_catalog` atau README spec (cari di mana agent
   membaca bentuk spec: `grep -n "name_like" excel_etl README.md DESIGN.md`) tambah satu contoh
   `name_regex`.
5. Lab: `prompt.py` satu kalimat: "Many sheets with the same table (one per day/month): use ONE
   source with sheet.name_regex and sheet_column, never one source per sheet." `index.html`
   label sumber: `s.sheet.name || s.sheet.name_like || s.sheet.name_regex || "?"`.

Tes M2: file sama -> `exact`; file dengan 1 sheet tambahan cocok -> `exact`; satu sheet cocok
rusak -> `drift` dan diff menyebut sheet itu; 0 cocok -> `missing`; kolom baru di semua sheet ->
`shifted`/`drift` sama seperti source biasa; peringatan `save_spec` muncul untuk 3 source
sebentuk dan tidak muncul untuk 2. Cek selesai = sama dengan M1 + tes lab:
`cd ../etl-e2e-lab && .venv/Scripts/python.exe -m pytest tests -m "not live" -q`.

### Fase M3 — peta workbook mengelompokkan sheet sebentuk — SELESAI 2026-11-11

File: `excel_etl/inspect/encode.py` (`workbook_map` ~116, `propose_datasets` ~854,
`_proposals` ~793). Tes di `tests/test_encode.py` / `tests/test_propose.py`.

1. Fungsi baru `_shape_groups(wb) -> list[tuple[list[int], str]]` (indeks sheet, pola):
   kunci bentuk sebuah sheet = (jumlah header_rows, tuple `text_key` teks header) dari region
   ber-header PERTAMA di sheet (pakai `detect_regions` + `column_names`, sudah di-memo per
   sheet; JANGAN scan ulang semua sel, gotcha 2 perf). Kelompok = >= 3 sheet dengan kunci sama.
2. Pola nama: ganti tiap run digit dengan `[0-9]{n}` (n = panjang run), escape sisanya
   (`re.escape`). Bila semua nama di kelompok menghasilkan pola sama -> pakai itu; bila tidak
   -> pola = `None` dan baris menyebut "names differ". Cek: pola harus `fullmatch` SEMUA nama
   kelompok dan TIDAK mencocokkan sheet lain di luar kelompok; bila mencocokkan sheet lain ->
   `None`.
3. `workbook_map`: sheet anggota kelompok (kecuali yang pertama) tidak dicetak satu-satu;
   baris sheet pertama diberi akhiran `| +157 same shape: F1.S5..F1.S161, name_regex
   [0-9]{4}-[0-9]{2}-[0-9]{2}`. Paging (`offset/limit`, `total`) tetap dihitung atas sheet
   asli supaya ID stabil (AGENTS §4). `propose_datasets`: sama, satu proposal per kelompok.
4. Budget: tes dengan 150 sheet sebentuk -> output `workbook_map` < 1.500 char (assert, chars/4
   aturan AGENTS §6).

Tes M3: 5 sheet sebentuk `m01..m05` + 2 sheet lain -> 1 baris kelompok + 2 baris; nama tak
berpola (`alpha, beta, gamma` sebentuk) -> kelompok dengan "names differ"; 2 sheet sebentuk ->
TIDAK dikelompokkan; pola yang ikut cocok ke sheet luar kelompok -> `None`; ID sheet tidak
berubah dibanding sebelum M3 (`F1.S7` tetap sheet ke-7).

### Fase M4 — uji nyata + dokumen — SELESAI 2026-11-11

1. Lab: job baru, upload file harian asli, minta agent "satu tabel untuk semua sheet tanggal".
   Catat: jumlah langkah spec, jumlah `save_spec` gagal, waktu total. Target: spec <= 10
   langkah, selesai < 5 menit. Publish, lalu uji `POST /auto` dengan file yang sama -> `ok`.
2. Perbarui `DESIGN.md` (bagian contoh spec ~141), `README.md` (bentuk spec), `HANDOVER.md`
   bila menyebut `SheetRef`. Isi Log + §Keputusan (salin K1-K9 singkat).
3. P6 (AGENTS §8) atas diff M1-M3.

### Jangan (jebakan yang sudah pernah terjadi)

- Jangan tulis kode lewat bash heredoc (terpotong diam-diam, gotcha 11/14). Pakai tool edit,
  lalu `ruff`.
- Jangan pakai `re.search`/`$` untuk pola nama (K2; gotcha 20: `$` menerima `\n`).
- Jangan andalkan urutan `UNION`/`JOIN` duckdb tanpa `ORDER BY` (gotcha 16).
- Jangan bandingkan nama kolom tanpa casefold (gotcha 18).
- Jangan ubah tes lama agar hijau; bila tes lama gagal, perilaku lama rusak.
- Jangan tambah node, dependency, atau opsi yang tidak tertulis di sini.

## Struktur target

Diperbarui 2026-11-08 agar sama dengan isi repo. Repo di GitHub bernama `Moyung-ETL`; paket Python
dan folder kerjanya `excel-etl` (impor `excel_etl`).

```
excel-etl/
  AGENTS.md  DESIGN.md  IMPLEMENTATION.md  README.md  HANDOVER.md
  pyproject.toml
  .github/workflows/ci.yml   # ruff + pytest di Linux dan Windows
  excel_etl/
    __init__.py
    workbook.py        # handle file: calamine + openpyxl lazy, cache sha256, sel error
    inspect/
      regions.py       # deteksi region/header/total (termasuk tabel semua-teks)
      encode.py        # tampilan teks untuk LLM (map, regions, render, profile, find_text,
                       #  compare_sheets, propose_datasets)
      lineage.py       # formula -> graf sheet, peran source/derived/mixed
    nodes/
      base.py          # Node, @node, build, catalog, describe, NodeError
      sqlexpr.py       # validasi ekspresi SQL skalar (daftar tolak fungsi)
      source.py        # locate, read_location, read_region: tabel dari header anchor
      general.py       # select, rename, cast, filter, derive, fill_missing, sort, dedupe
      combine.py       # join, union, group_by, pivot
      relops.py        # pembantu node multi-input (need, apply, shape)
      excel.py         # drop_rows, fill_down, unpivot, unpivot_groups
      labels.py        # parse_hierarchy, split_header_token
    pipeline.py        # PipelineSpec, SheetRef/RegionRef/Source/Step/Output, parse_spec
    engine.py          # validate, run, ValidationReport, RunReport, PipelineError
    rebind.py          # rebind, BindRecord, BindReport
    versions.py        # diff_specs, SpecHistory
    hitl.py            # HitlPolicy, HitlState, ask_human, answer, approve, gate
    knowledge.py       # Knowledge, lookup_knowledge
    quality.py         # checks: not_null, unique, range, accepted_values, reconcile
    state.py           # JobState, Job, StateStore, FileStore, FileProvider, open_files
    tools.py           # JobTools: permukaan publik untuk agent dan host
  tests/
    conftest.py        # builder workbook sintetis, marker sample
    test_*.py
```

Struktur boleh menyusut (gabung file) bila isinya kecil. Jangan menambah file di luar
daftar tanpa alasan.

## Fase 1 — Workbook + inspect inti

Tujuan: agent bisa memahami bentuk workbook dengan token minimal.

- [x] 1.1 Scaffold: `pyproject.toml` (uv, py3.12, deps §AGENTS 3, ruff, pytest marker
      `sample`), `tests/conftest.py` dengan helper workbook sintetis.
- [x] 1.2 `workbook.py`: `Workbook.open(path|bytes)`, sha256, daftar sheet + visibility,
      grid nilai (calamine), merged, `openpyxl` lazy per sheet (formula, style dasar:
      bold/fill/border, number format), cache.
- [x] 1.3 `workbook_map(file)`: tabel ringkas per sheet (id, nama, size terpakai,
      visible, jumlah region, % formula, tebakan peran). Target 72 sheet <= 2k token.
- [x] 1.4 `regions.py`: deteksi region + noise di atas tabel + header multi-level +
      merged header + total row + orientasi + pola grup kolom berulang
      (`[Ton, Formula] x N`).
- [x] 1.5 `sheet_regions(sheet)`: render hasil 1.4 sebagai teks.
- [x] 1.6 `render_range(ref, range, mode=compressed|raw)`: anchor + ringkasan
      tipe/rentang + inverted index teks; raw dengan penanda merged; budget token +
      paginasi.
- [x] 1.7 Test sintetis untuk 1.2–1.6 + test `sample` pada BALANSITAS:
      `(N) PKG` compressed < 1.000 token; `Per Bulan` terdeteksi >= 3 region.

Selesai bila: semua test hijau, angka token dicatat di §Temuan.

## Fase 2 — Inspect lanjutan

- [x] 2.1 `lineage.py`: parse referensi formula (`Sheet!A1`, `'Nama Sheet'!A1:B2`,
      defined names) -> graf sheet->sheet; peran `source`/`derived`/`mixed` per region.
- [x] 2.2 `profile_region`: per kolom tipe, null %, distinct, min/max, contoh.
- [x] 2.3 `find_text(file, query)`: case-insensitive, normalisasi spasi/unicode.
- [x] 2.4 `compare_sheets(a, b)`: kemiripan struktur + nilai (deteksi `X (2)`).
- [x] 2.5 `propose_datasets(files)`: usulan dataset dari region `source`/`mixed`, turunan
      dijadikan target rekonsiliasi. Output untuk dipilih manusia.

## Fase 3 — Node + pipeline

- [x] 3.1 `PipelineSpec` (pydantic) + parse dari JSON string (YAML ditunda, lihat §Keputusan).
- [x] 3.2 Registry `@node`, kontrak `infer_schema` + `run` (duckdb relation).
- [x] 3.3 Node Excel-spesifik: `read_region` (source), `drop_rows`, `fill_down`, `unpivot`, `unpivot_groups`, `parse_hierarchy`, `split_header_token`. `flatten_header` = bagian `read_region`.
- [x] 3.4 Node umum + validasi ekspresi SQL skalar: `check_expression`, `check_type`, `bind`; `select rename cast filter derive fill_missing sort dedupe join union group_by pivot`.
- [x] 3.5 `validate(spec)` tanpa data; `run(spec)` -> output parquet/csv + RunReport (`engine.py`).
- [x] 3.6 Multi-file per job (join/union antar `F1`, `F2`) — tercakup oleh `file_role` per source + tes dua file (join, union layout sama) di `tests/test_engine.py`.

## Fase 4 — Recycle

- [x] 4.1 Anchor binding: sheet (`name_like`, `fallback_contains`), region
      (`header_anchor`, `header_rows`, `end`), kontrak kolom (`match`, `match_regex`,
      `group`, `required`).
- [x] 4.2 `rebind(spec, files, previous=None)` -> BindReport (`exact`/`shifted`/`drift`/`missing`) + diff (`rebind.py`). `previous` = `records()` laporan lama, disimpan host.
- [x] 4.3 Versi spec + diff saat repair (`versions.py`: `diff_specs`, `SpecHistory`).
- [x] 4.4 Test: file sintetis bergeser baris/kolom, kolom grup bertambah, sheet ganti nama.

## Fase 5 — HITL, Knowledge, Quality, State

- [x] 5.1 `Knowledge` + `lookup_knowledge` (`knowledge.py`).
- [x] 5.2 `HitlPolicy`, `PendingQuestion`, `ask_human`, `answer` -> qa_log.
      `run` menolak bila checkpoint `required` belum approved.
- [x] 5.3 `quality.py`: assert + rekonsiliasi dengan toleransi.
- [x] 5.4 `state.py`: `JobState`, `Job.from_state` / `Job.open(store=)`,
      `StateStore` protocol, `FileStore` (optimistic lock `expected_version`),
      `FileProvider`.

## Temuan P6 Fase 3-5 dan Fase 6 (dibersihkan 2026-11-07)

Selesai: konversi error jadi dict dan semua tool yang disebut (`tools.py`), `after_run`/`on_rebind_drift` dipasang di `run_pipeline`/`rebind_sources`, `lookup_knowledge` jadi tool, import privat lintas modul jadi publik, `FileStore` aman antar proses (file kunci `O_EXCL`, kunci basi diambil alih setelah 30 s, `os.replace` dan `load` diulang bila Windows menolak sesaat), file state ber-BOM terbaca, `Job.reload` + tool host `reload_job`, `save_spec`/`validate_spec`/`rebind_sources`/`run_pipeline` hanya membuka role yang dipakai spec, `JobTools(per_version=True)` menulis ke `out_dir/v<N>`, `truncated` selalu membawa kunci `next_offset`, `accepted_values` pada kolom angka dibanding sebagai angka (1 = 1.0 = 1.00), `NodeStat.note` mencatat alasan baris berubah per node, `reconcile` punya `where` per sisi (baris TOTAL sheet vs baris di atasnya), error cell Excel dihitung dan diperingatkan, memo `text_keys` ditutup (ukurannya dibatasi isi workbook yang sudah di memori dan hidup sebesar cache LRU).

Tersisa (disengaja):

- `reconcile` tidak mengisi diri dari baris TOTAL yang dideteksi `detect_regions`: agent menulis `where` sendiri (satu aturan eksplisit, bukan tebakan label).
- `Job.update` ~40 ms pada state 150 KB (300 versi spec): deepcopy + validasi; host yang memangkas versi lama menghilangkannya.
- Tidak ada batas jumlah source/step per spec (3000 source = validate 6 s).
- Contoh LiteLLM belum diuji (6.3).
- `_fit` memuat seluruh body sebelum memotong (hanya `compare_sheets` besar; di bawah budget 24k char tidak terasa).

## Fase 6 — Serah ke host

- [x] 6.1 `tools.py` final: `JobTools(job, provider, out_dir)`; 21 tool agent (`agent_tools()`) + 3 tool host (`add_file`, `answer`, `approve`).
- [x] 6.2 Contoh pembungkus ADK `FunctionTool` (`README.md`; bukan dependency).
- [ ] 6.3 Uji schema tool lewat LiteLLM ke >= 2 provider (oleh rekan / bersama).

## Keputusan

| Tanggal | Keputusan | Alasan |
|---|---|---|
| 2026-11-05 | Library Python murni, bukan framework/plugin/MCP | host (PINTER) yang mengorkestrasi |
| 2026-11-05 | calamine untuk nilai + openpyxl lazy untuk formula/style | 1,0 s vs 16 s (15 MB, 72 sheet) |
| 2026-11-05 | duckdb sebagai engine + SQL skalar sebagai expression | volume bervariasi, tanpa expression language sendiri |
| 2026-11-05 | Spec pakai anchor semantik untuk recycle | tiap file beda bentuk, alamat sel tidak stabil |
| 2026-11-05 | Default: dataset dari sheet source; derived untuk rekonsiliasi | cegah hitung dobel |
| 2026-11-06 | propose_datasets: seksi derived level-sheet (edge lineage), bukan level-region | resolusi region->source butuh kerja per sel; presisinya baru berguna di fase quality (rekonsiliasi), ditunda ke sana |
| 2026-11-05 | State: dua mode, stateless (host pegang JSON) atau `StateStore` | fleksibel untuk host |
| 2026-11-05 | HITL via `ask_human` + `HitlPolicy` config | penempatan checkpoint fleksibel tanpa ubah kode |
| 2026-11-05 | Tolak `sheetwise` sebagai dependency, port idenya | lihat §Temuan |
| 2026-11-05 | Tolak `SheetBrain` sebagai dependency, ambil pola U-E-V + sandbox | terikat OpenAI/tiktoken/multimodal |
| 2026-11-06 | Spec diterima sebagai JSON saja, tanpa `pyyaml` | AGENTS.md batasi dependency; agent LLM menulis JSON andal, YAML = dependency baru untuk kenyamanan. Tambah bila host butuh (parse YAML -> dict -> `PipelineSpec.model_validate`). `SpecError` memberi hint bila input terlihat YAML. |
| 2026-11-06 | Output `run` ditulis ke `.part` lalu `os.replace` hanya bila semua sukses | run gagal tak menyisakan file setengah jadi; output lama yang bagus tetap terganti hanya saat run sukses |
| 2026-11-06 | join/union/dedupe/unpivot_groups mengurutkan eksplisit lewat posisi baris (`row_number() OVER ()`) | duckdb tidak menjamin urutan (terukur: join many-to-many 300k baris, UNION distinct urutan hash) |
| 2026-11-11 | Multi-sheet: `name_regex` + `sheet_column` di `SheetRef`; fullmatch, eksklusif dari matcher lain, kolom asal wajib | satu source untuk tabel berulang; nama dan asal baris deterministik |
| 2026-11-11 | Urutan workbook/baris eksplisit; union by name; tabrakan kolom casefold ditolak | tidak kehilangan data atau bergantung pada planner duckdb |
| 2026-11-11 | Sheet cocok yang rusak gagal; 0 cocok missing; jumlah sheet berubah bukan drift | fail-fast untuk data hilang, file harian baru tetap dapat auto-run |
| 2026-11-11 | Tidak ada node/dependency/perubahan auto; map dan proposal mengelompokkan >=3 sheet sebentuk | fitur dipakai ulang oleh alur lama dan output agent tetap kecil |

## Temuan

**File referensi** `(2026) BALANSITAS AMONIAK, UREA, NPK, DAN BAHAN BAKU 3.0.xlsx`
(15,1 MB; di luar repo):
- 72 sheet (4 hidden: `Outstanding PKT 2026`, `Shipping Out Curah (U) PKT`,
  `supplyinout`, `supplyinout (2)`; dicek calamine + openpyxl), 542.358 sel formula,
  14.010 defined names, 0 ListObject.
- Baca: calamine semua nilai 1,0 s; openpyxl `data_only` 16,0 s; openpyxl formula 18,1 s.
  openpyxl membuang conditional-formatting extension & gambar WMF (warning).
- Pola: header 2 level (bulan x anper), hierarki baris (`1.0`/`1.1`/`1.2.1`), grup kolom
  berulang (`Phonska N [Ton, Formula]`), noise atas tabel (link DASHBOARD, KPI),
  sheet turunan (`BALANS NPK`, `REKAP BALANS`), versi duplikat (`X (2)`), banyak
  tabel dalam satu sheet (`Per Bulan`: header bulan di D4, CN4, D57).
- `(N) PKG` berisi tanggal sampai 2027-12-31 (805 baris), bukan hanya 2026.

**sheetwise 2.8.0** (via `pd.read_excel(header=None)`):

| Sheet | Ukuran | Output | ~Token | Kompresi |
|---|---|---|---|---|
| `(N) PKG` | 807x159, 50k sel | 136.724 char | ~34.000 | 1,2x |
| `Per Bulan` | 88x193, 8,4k sel | 61.377 char | ~15.300 | 1,0x |

Masalah: tanggal & angka didaftar per sel, struktur/region/merged/header hilang,
multi-tabel tercampur, `PKG` dianggap `Biz_Ticker`. Berguna: inverted index
(nilai sama di banyak sel = sinyal duplikat/turunan). Script: `%TEMP%\swtest.py`.

**Versi terpasang (uv.lock)**: python-calamine 0.9.1, openpyxl 3.1.5, duckdb 2.0.0,
pydantic 2.14.3. `SheetVisibleEnum` calamine = enum Rust, tanpa `.name`; bandingkan
langsung dengan `SheetVisibleEnum.Visible/Hidden/VeryHidden` (juga tidak hashable,
jadi tidak bisa jadi key dict). openpyxl read_only: `EmptyCell.font is None`; sheet
baru di-parse saat `iter_rows`, jadi filter warning harus membungkus loop.
`workbook_map` BALANSITAS: 3.140 char (~785 token), 2,1 s. Persen formula dihitung
dari scan regex XML sheet (`Workbook.formula_cells`, 0,76 s seluruh workbook), bukan
openpyxl; cocok persis dengan openpyxl di `(N) PKG` (36.368 sel). Total 542.366, beda 8
dari angka lama 542.358 (belum diselidiki, kemungkinan array formula).
`Workbook.meta("(N) PKG")` ~3,9 s (load openpyxl + 1 sheet); `values` jauh di bawah 1 s. Kontrak calamine dipaku
di `tests/test_calamine_contract.py`.

`detect_regions` (1.4) di BALANSITAS: `(N) PKG` A4 header [4,5], grup `[Ton, Formula] x 9`
dengan parent Phonska 1..; `Per Bulan` 4 region berheader (A4, BZ4, FK4, A58), grup bulan x
anper (urutan anper BZ beda dari A); `RKAP 2026` B3 header [3,4], 5 total; `REKAP BALANS`
7 tabel berulang, noise `Periode|Jul`. Semua sheet: 3.825 region (banyak blok kecil tanpa
header: catatan/KPI di samping tabel), deteksi <=0,16 s/sheet setelah `values` di-cache;
`workbook_map` sampel ~4,6 s (sebelumnya 2,1 s). Baris dashboard angka di atas header
(`(N) PKG` baris 2) diatasi: data mulai di run >=3 baris data.
`sheet_regions` (1.5) BALANSITAS: `(N) PKG` 1.485 char (18 tabel, 123 blok tanpa header
diringkas), `Per Bulan` 792 char, `(U) PKT` 3.606 char (~900 token, 87 tabel; terbesar).

`render_range` (1.6) compressed, seluruh sheet BALANSITAS (chars/4): `(N) PKG` 985 token
(sheetwise ~34.000, ~35x lebih kecil), `Per Bulan` 953, `RKAP 2026` 1.230, `(U) PKT` 3.140.
Yang menekan ukuran: teks unik digabung per baris (`r4: A=TANGGAL|...`) atau per kolom
(`col A: 37=JAN|...`); teks berulang jadi inverted index (maks 3 posisi + `+N`); run angka
per kolom digabung bila rentang baris sama; formula tanpa cache ikut run (`+=?`); kolom
serba-0 satu baris `all 0:`; label dipotong 30 char.

**P6 Fase 1** (3 subagent: adversarial, auditor, agent pengguna; 2026-11-06). Diperbaiki:
- calamine **abort proses** (bukan exception) bila ada nilai jauh dari data (XFD1048576):
  alokasi persegi A1..sel terakhir. Sekarang `Workbook._scan` mengukur persegi itu dari XML
  (satu regex, 0,6 s seluruh sampel) dan menolak > 25 jt sel dengan `WorkbookError`.
- Range raksasa (`A1:XFD1048576`) dan merge raksasa menggantung: range dipotong ke area
  berisi (`_clamp`), merge > 10.000 sel hanya menandai sel kiri-atas (`SheetGrid`).
- Budget bisa jebol oleh satu baris lebar: tiap baris dipotong 600 char; `|` dan newline di
  label/nama sheet dinetralkan.
- Error non-`WorkbookError`: XML rusak (`XmlError`, `zlib.error`, `BadZipFile`), `open(None)`,
  angka 5000 digit, `offset`/`limit`/`budget`/`mode` tidak valid (sebelumnya `limit=0`
  menghasilkan loop tak berujung), baris 0 / kolom di luar XFD, ligatur `ﬀ1` jadi `FF1`.
- Split tabel rekursif (RecursionError di 1.100 tabel, 5,7 s di 800): jadi satu lintasan; noise
  di atas tabel pertama tidak lagi hilang. `_merge_overlaps` jadi sweep. `workbook_map` hanya
  merangkum sheet di halaman yang diminta. `_Grid` jadi `SheetGrid` publik, dibangun sekali per
  sheet; `col_index` satu definisi di `workbook.py`; id sheet menang atas nama sheet serupa id;
  region id + range sekaligus = error.
- Docstring tool ditulis ulang untuk LLM (arti tiap kolom); header kolom `sheet_regions` jadi
  `hdr rows|orientation|total rows|group|merged|noise`.
Ditunda (catat, belum perlu):
- Agent bingung: `workbook_map` role "mixed" untuk 40 dari 72 sheet; tidak ada petunjuk kolom
  mana tanggal/label; baris subtotal bulan (`JAN`, `.JAN`) bercampur di tabel harian;
  `+N` memotong sel anper; tidak ada "region di sel X". Jalan upgrade: `profile_region` (2.2)
  dan lineage (2.1) memberi tipe per kolom dan asal formula.
- Error cell (`#DIV/0!`) dibaca calamine sebagai kosong; inf hilang; int > 2^53 terpotong 30 char.
- `Workbook.meta` openpyxl tidak dibatasi ukuran. `.xls` tidak punya scan formula.
- Test blok kecil tanpa header (`columns`) dan `_rects` hanya diuji lewat tool.

**Lineage (2.1)** di BALANSITAS: 542.366 formula, 144.049 teks unik, 258 edge sheet->sheet.
`(BB) CR` paling banyak dibaca (32.079 sel), lalu `(N) PKG` (15.979), `BALANS NPK` (13.927).
Link ke file lain (`[1]...`): `(N) PKC/PKT/PIM/PSP/PKG` (1.696 sel). Dari 20.531 defined names
hanya 128 menunjuk sheet, hanya 2 (`AA`, `BULAN`) dipakai formula. Peran: 22 source, 39 mixed,
11 derived (sebelumnya persen formula saja: `(N) PKG` 72% formula = "mixed", padahal 7.210 angka
ketikan = source). Per region: `(N) PKG` R1 source, R13-R15 derived (kolom turunan di samping),
`Per Bulan` R2 derived, R1 mixed, `RKAP 2026` R1 source, `REKAP BALANS` derived.
- Sel dihitung typed / local (formula yang hanya membaca sheetnya sendiri) / cross (membaca
  sheet atau file lain). Teks tidak memilih. Ambang di `classify` (10% / 50%).
- Parse formula pakai regex, tanpa tokenizer penuh: awalan `Sheet!`, `'Nama sheet'!`, `A:C!`
  (3D, diperluas ke semua sheet di antaranya), `[1]X!`, `#REF!`, string literal dibuang dulu.
  Nama yang bukan sheet file ini dianggap link file lain (`[file]`).
- Shared formula: sel anak memakai teks sel pertama (referensi relatif tidak digeser); cukup
  untuk "sheet mana yang dibaca", tidak cukup untuk "sel mana".
- Kecepatan: pra-filter regex 128 nama 6,5 s, tokenisasi + set 0,3 s (diukur). Lineage 1,9 s,
  teks formula 2,3 s. `workbook_map` sampel 8 s (sebelum lineage 4,5 s; sempat 25 s karena
  `count` mengiterasi sejuta baris).

**Profile (2.2)** BALANSITAS: `(N) PKG` R1 512 token (29 kolom), `Per Bulan` R1 1.151 token
(76 kolom), `RKAP 2026` R1 409 token. Nama kolom dari header bertingkat (`JANUARI|PKG`,
`Phonska 1|Ton`); parent yang tidak di-merge dibawa ke kanan sampai label berikutnya.
Baris total dibuang dari angka dan disebut di baris pertama. `odd <tipe> n at r..` menunjuk
sel bertipe lain: di `(N) PKG` kolom A memperlihatkan 21 baris teks `JAN`/`.JAN` di tengah
tanggal (subtotal bulan), menjawab keluhan agent P6.

**find_text (2.3)** BALANSITAS: `tanggal` 78 sel di 42 dari 72 sheet, 598 token; `phonska`
195 sel di 17 sheet, 469 token. Panggilan pertama 2,0 s (memuat semua sheet + normalisasi
144k teks, di-cache di `wb.memo`), panggilan berikut 0,09 s. Hanya sel teks: `rekap balans`
dan `supplyinout` 0 hit (nama sheet saja; nama sheet urusan `workbook_map`). Normalisasi
NFKC + casefold + kolaps spasi (ligatur `ﬀ` = `ff`, NBSP = spasi), substring, bukan regex.

**compare_sheets (2.4)** BALANSITAS: `supplyinout (2)` ternyata bukan copy-paste `supplyinout`
(1115 dari 1150 sel terisi beda; duplikat nama saja, isinya data periode lain). `(N) PKG` vs
`(N) PKC`: 17.183 dari 17.837 beda. Cepat: 0,04-0,32 s per pasang (sheet sudah termuat).
Verdict 3 kata (`same values`/`values differ`/`shapes differ`); baris 2 = shape, sel terisi
yang beda di bagian shared, jumlah formula, jumlah region + same ranges; baris 3 = 6 diff
pertama `A5 10|12` + jumlah baris yang memuat diff. Tanpa paginasi (output kecil by design).
Satu workbook saja; compare antar file = kebutuhan fase recycle (ditunda).

**propose_datasets (2.5)** BALANSITAS: 447 usulan (367 source, 80 mixed), 297 tabel derived
ditinggalkan, 72 sheet. Halaman pertama 4.777 char (~1.200 token), 9 halaman (limit 50).
Panggilan pertama 7,6 s (detect_regions + region_role semua sheet), halaman berikut 0,0 s
(hasil di-`wb.memo` per file_id). Seksi `derived sheets to check against` hanya sheet yang
punya edge cross (sheet ber-tablet derived-lokal hanya dihitung di head, tidak masuk seksi:
tidak ada yang bisa direkonsiliasi). Dashboard = pembaca terbanyak (5 tabel, 29 sheet dibaca).

**P6 Fase 2** (verifikasi paralel 3 subagent: adversarial 341 cek, auditor statis,
agent-user dengan workbook buatan; semua cek kebenaran agent-user lulus). Diperbaiki:
`limit=None` di 4 tool paginasi jadi `WorkbookError` (sebelumnya TypeError di hitungan
halaman; host yang kirim null eksplisit untuk argumen opsional); `localSheetId` rusak di
workbook.xml (bukan angka/negatif/luar range) jadi `WorkbookError` (sebelumnya ValueError
bocor; `-1` diam-diam terikat ke sheet terakhir); `compare_sheets` set `truncated`
sebenarnya saat budget memotong (sebelumnya selalu false); seksi derived `propose_datasets`
hanya menyebut sheet yang tabel derived-nya memuat sel cross di dalam box-nya (koreksi temuan
2.5: sel KPI liar di luar tabel tadinya ikut mengatribusi lineage); hitungan `rows` usulan =
baris data tanpa baris total, sama dengan `profile_region` (BALANSITAS: RKAP 2026 R1 kini
132, bukan 133); diff float pakai repr penuh (0.3 vs 0.30000000000000004 kini terlihat,
sebelumnya 0.3|0.3); echo query `find_text` dipotong 40 char (query 50k char tidak lagi
menelan baris hitungan); error "no sheet" memuat maks 5 nama + `+N`; `_region` menolak
sheet yang bernama persis id region (konsisten dengan `_target`); pesan "has no regions"
menggantikan "R1..R0"; docstring budget menjelaskan soft cap (head + baris pertama selalu
tampil). Tabel ber-header tidak mungkin ber-role `empty` (deteksi butuh >=1 sel
numerik), jadi temuan "tabel text-only hilang dari head" tidak reachable; cabang guard
di `_proposals` dibiarkan. Ditunda: tabel 1 kolom/tanpa header tak terdeteksi (ambang 2x2
by design), error cell `#DIV/0!` terbaca kosong (warisan P6 Fase 1), memo `text_keys`
tumbuh per nilai teks unik (mati bersama LRU workbook), `_fit` streaming (materialisasi
body dulu), render compressed menggabung kolom sejenis jadi satu run, heuristik
total-row match label "Total ..." di dashboard, formula `=?` tanpa teks di render
compressed. 153 unit + 11 sampel lulus.

**PipelineSpec (3.1)** `pipeline.py`: `sources` (file_role, sheet: name/name_like/
fallback_contains, region: header_anchor/header_rows/end, expect_columns), `steps` (id, node,
inputs, config bebas), `outputs` (input, format parquet|csv). Semua model `extra="forbid"`:
kunci salah ketik jadi error, bukan hilang diam-diam. Validasi pelekatan: step hanya boleh
membaca nama yang sudah ada (source atau step sebelumnya), jadi urutan tak bisa berputar;
nama = huruf/angka/`_` (jadi nama relation duckdb). Error = `SpecError` berisi
`path.ke.field: pesan` per baris, maks 10. Config node masih dict bebas; divalidasi saat
registry node (3.2) ada. Spec tidak punya path output: itu urusan host.

**Registry node (3.2)** `nodes/base.py`: `Node` (ClassVar `Config`, `min_inputs`/`max_inputs`,
`infer_schema(list[Schema]) -> Schema`, `run(list[Relation]) -> Relation`), `@node("nama")`,
`build(name, config) -> Node`, `infer(node, schemas)` (cek jumlah input), `schema_of(rel)`,
`quote(name)`. `Schema` = tuple `Column(name, type)`; tipe = nama tipe duckdb. `NodeConfig`
`extra="forbid"`. Semua kegagalan jadi `NodeError` (nama node tak dikenal memuat daftar node,
config salah memuat `path: pesan`). Node pertama `select` (`nodes/general.py`) sebagai contoh
dan bukti kontrak: test mengecek `infer_schema == schema_of(run(...))`. `describe` (format error
pydantic) dipakai bersama `parse_spec`. `quote` wajib untuk semua identifier: header Excel
memuat `|`, spasi, kutip (`JANUARI|PKG`). duckdb 2.0.0: `rel.columns`/`rel.types` cukup untuk
skema; `enable_external_access=false` memblok `read_csv` (PermissionException) dan tak bisa
dinyalakan lagi di koneksi yang sama.

**read_region (3.3)** `nodes/source.py`: `read_region(wb, source, con, name) -> Relation`. Sheet:
`name`, lalu `name_like` (cocok persis atau setelah normalisasi teks), lalu sheet yang memuat semua
`fallback_contains`; lebih dari satu kandidat = `NodeError` (tidak menebak). Header: tabel pertama
dari `detect_regions` yang baris headernya (dalam `_HEADER_SEARCH`=12 baris dari puncak tabel) memuat
semua `header_anchor`; anchor dinilai per tabel karena teks yang sama berulang di tabel samping
(`Phonska 1` ada di 4 tempat di `(N) PKG`). Nama kolom dari `_column_names(sep_safe=False)`:
`PARENT|LEAF` dengan `|` asli dipertahankan (teks view memakai `/`); duplikat jadi `Ton (2)`.
Akhir tabel = `until_blank_rows(N)` baris kosong berturut. Tipe kolom dari nilai: satu jenis
dipertahankan (BIGINT/DOUBLE/DATE/...), campuran jadi VARCHAR dengan bentuk teks tiap nilai
(tidak ada yang hilang atau ditebak); formula tanpa cache = NULL. Koneksi dari `connect()`
(`enable_external_access=false`). BALANSITAS: `(N) PKG` 802 baris x 29 kolom 1,4 s, `Per Bulan` 49 x 76
0,2 s. TANGGAL jadi VARCHAR karena memuat subtotal `JAN`, `FEB - 2025`, `.` di tengah tanggal
(34 titik, 11 `FEB - 2025`); `drop_rows` yang membuangnya. Kolom `Formula` berisi teks `sub c` (724) =
bukan angka, jadi VARCHAR. Per Bulan: dua kolom pertama tanpa nama header (`-`, `- (2)`).
Ditunda: kontrak `expect_columns` (cek tipe/wajib) dan status exact/shifted/drift ke fase rebind.

**drop_rows, fill_down (3.3)** `nodes/excel.py`. `drop_rows`: `blank` (default true, baris
semua-NULL), `column` + `keep_type` (date|timestamp|bigint|double|boolean; simpan baris yang
`try_cast` ke tipe itu berhasil, jadi label `JAN`/`FEB - 2025`/`.` terbuang) atau `column` +
`drop_regex` (baris Total; sel NULL tidak cocok jadi tetap). Tipe kolom tidak diubah: `cast`
(3.4) yang mengubah. Config tak konsisten (column tanpa aturan, aturan tanpa column, regex rusak)
ditolak saat `build`. `fill_down`: `last_value(... IGNORE NULLS)` dengan frame `ROWS UNBOUNDED
PRECEDING..CURRENT ROW`, `* REPLACE` jadi urutan kolom sama; NULL di awal tetap NULL.
Mengandalkan urutan baris, yang dijaga `read_region` (tabel dimuat berurutan). BALANSITAS
`(N) PKG`: `drop_rows(TANGGAL, keep_type=date)` menyisakan tepat 730 baris tanggal dari 802
(72 baris subtotal/label/kosong terbuang).

**unpivot, unpivot_groups (3.3)** `nodes/excel.py`, dirancang tanpa asumsi satu workbook.
`unpivot`: `keep` + (`columns` XOR `pattern` regex pada nama kolom), `name_column`/`value_column`,
`value_type` opsional, `drop_nulls` (default true). Tipe kolom nilai disatukan: sama = tetap,
semua numerik = DOUBLE, selain itu VARCHAR (atau `value_type`); duckdb menolak UNPIVOT lintas tipe,
jadi tiap kolom di-CAST dulu. `unpivot_groups`: `pattern` wajib berisi grup bernama
`(?P<group>..)` dan `(?P<measure>..)`; pemisah/format nama bebas (`A|Ton`, `north_units`,
`Q1 Sales`...) karena itu di regex, bukan di kode. Blok boleh tak seragam (ukuran yang tak ada
= NULL bertipe), `drop_empty` membuang baris blok yang kosong semua, duplikat (grup, ukuran) dan
ukuran yang bentrok dengan kolom keluaran = `NodeError`. Dibangun dengan `UNION ALL` per grup
(duckdb `UNPIVOT` multi-kolom dibatasi: tiap entri harus berukuran sama). Catatan perilaku:
pattern yang terlalu longgar ikut menangkap blok yang tak dimaksud (mis. `Total|n`); ada tes
yang menunjukkannya dan cara mempersempit dengan lookahead. Di sampel, `unpivot_groups`
menghasilkan 13 kolom keluaran (ukuran Ton, Formula, Sub, NS, ...): pattern longgar menangkap
semua `X|Y`, jadi pengguna menyempit sesuai kebutuhan. Mutasi `NULL` polos vs `CAST(NULL AS t)`
tidak berefek: UNION ALL mengikuti tipe cabang lain; CAST dipertahankan agar eksplisit.

**parse_hierarchy, split_header_token (3.3)** `nodes/labels.py`. `parse_hierarchy`: `column`,
`separator` (default `.`, bebas panjang: `-`, ` > `), `level_column`, `parent_column`. Level =
jumlah segmen, parent = kode tanpa segmen terakhir (NULL di puncak). Kode dengan segmen kosong
(`1..2`, `1.2.`, `.1`) atau kosong/NULL diberi NULL di kedua kolom, bukan ditebak. Kolom
desimal DITOLAK saat `infer_schema` (Excel menyimpan `1.10` sebagai angka 1.1: informasi hilang
sebelum node ini bertemu data); integer dan teks diterima. `split_header_token`: `column`,
`into` (>=2 nama), `separator` literal (kolom terakhir menerima sisa) ATAU `pattern` regex
dengan tepat satu grup tangkap per nama; bagian kurang/kosong/tak cocok = NULL, bagian di-trim,
`drop_source`. Biasanya dipakai setelah `unpivot` (isi `name_column` = nama kolom lama).
Hasil keluaran selalu VARCHAR. Tes: 26, termasuk dua workbook sintetis berbeda bentuk (kolom
`2025|Units` lalu unpivot lalu split; kode akun `1`, `1.1`, `2.1`). Dua kode mubazir ditemukan
mutasi dan dihapus (cast BIGINT pada `len`, CASE di pola: `regexp_extract` tanpa cocok sudah
'').

**Validator ekspresi SQL (3.4)** `nodes/sqlexpr.py`: `check_expression(text) -> text`, `NodeError`
bila tidak lolos. Teks dibungkus `SELECT (<expr>) AS __e` lalu diparse oleh parser duckdb sendiri
(`json_serialize_sql`), bukan regex. Tiga lapis: (1) bentuk: harus tepat satu SELECT, satu kolom,
tanpa FROM/WHERE/WITH/ORDER/GROUP/UNION; (2) pohon: hanya kelas node
`BETWEEN CASE CAST COLUMN_REF COMPARISON CONJUNCTION CONSTANT FUNCTION OPERATOR TYPE`
(subquery, `*`/`COLUMNS`, `?`, `#1`, window, lambda ditolak), fungsi tidak boleh ada di
`_DENIED_FUNCTIONS` (baca file/jaringan, `query`, state sesi, sequence, nilai berukuran
bebas seperti `repeat`/`range`/`lpad`, dan fungsi tak-reprodusibel: `random`, `now`, `uuid`),
tanpa ORDER BY/FILTER/DISTINCT di dalam pemanggilan fungsi; (3) koneksi `connect()` tanpa akses
file. Panjang maks 4000, NUL ditolak. Nama kolom yang tak ada baru ketahuan saat node meng-bind
ekspresi ke skema (`infer_schema`). Penyelidikan: duckdb 2.0 menyerialisasi nama fungsi
sudah huruf kecil; pembungkus `( ... )` membuat `1) WHERE (` dan sejenisnya syntax error, jadi
sebagian cek bentuk (WHERE, tipe node) kini lapisan cadangan: ditandai di komentar dan di tes
`test_parser_behaviour_the_shape_check_leans_on` yang mengunci perilaku parser supaya upgrade
duckdb yang melonggarkannya langsung terlihat. Fuzz (1.500 ekspresi dari tata bahasa, 133 bentuk
jahat terbungkus, 3.000 teks acak): tidak ada ekspresi berbahaya lolos, tidak ada exception selain
`NodeError`. Satu celah ditemukan fuzz: `upper(*)`/`list_value(*)` lolos karena parser membacanya
sebagai pemanggilan tanpa argumen (aman, tidak bisa menjangkau data lain); `min(COLUMNS(*))`
yang berbahaya (banyak kolom) ditolak. Dikunci di tes. 11 mutasi: 8 tertangkap, 3 lolos
(WHERE, tipe node, huruf besar-kecil) karena memang lapisan cadangan (lihat di atas).

**Node umum langkah 1 (3.4)** `nodes/general.py`. Aturan bersama: config yang mengandung SQL
(`filter.where`, `derive.expr`, `derive.type`, `cast.types`) diperiksa saat `build` (validator di
`sqlexpr.py`), lalu diikat ke skema saat `infer_schema` lewat `bind` (CREATE TEMP TABLE kosong
berskema input + `SELECT`), jadi kolom tak ada, tipe tak cocok, dan agregat jadi `NodeError`
sebelum ada data. `check_type` = whitelist regex (huruf, spasi, `(10,2)`, `[]`) lalu duckdb
harus mengenalnya; STRUCT/ENUM ditolak; nama tipe adalah titik injeksi kedua di `CAST`.
`rename` (swap diperbolehkan, bentrok nama keluaran ditolak), `cast` (`strict` false = TRY_CAST,
nilai tak cocok jadi NULL), `filter` (ekspresi harus BOOLEAN), `derive` (ganti di tempat atau
tambah di akhir, `type` opsional), `fill_missing` (nilai tetap; harus muat di tipe kolom, dicek
dengan evaluasi konstanta karena bind di tabel kosong tidak mengevaluasi), `sort` (`-kolom` =
turun, NULL selalu di akhir, kolom yang namanya memang diawali `-` menang atas penanda),
`dedupe` (`keys` opsional; yang pertama = pertama menurut urutan tabel).
**Temuan dari tes-tes ini:** (1) *agregat lolos validator*: `derive` dengan `sum(x)` akan mengubah
seluruh tabel jadi satu baris tanpa error; validator awal (dan tes saya sendiri) bahkan menyatakan
`sum(a)` "lolos". Kini agregat ditolak dengan daftar dari `duckdb_functions()` (79 nama, tak ada
yang sekaligus skalar) + `count_star`. (2) *dedupe memilih baris salah pada data besar*:
`row_number() OVER (PARTITION BY k)` tanpa ORDER BY mengikuti scan paralel, bukan urutan tabel
(200k baris: baris "pertama" bukan yang pertama); mutasi menangkapnya, kini memakai posisi
eksplisit `row_number() OVER ()`. (3) bind di tabel kosong tidak mengevaluasi konstanta
(`coalesce(id, CAST('abc' AS INTEGER))` lolos) sehingga `fill_missing` mengevaluasi nilainya
sendiri. Mutasi lolos yang dinilai kode ganda/bukan celah: `NULLS LAST` (default duckdb sudah
begitu, tetap ditulis eksplisit dan dites), `count_star` manual (sudah ada di katalog), cek nama
baru ganda di `rename` config (cek keluaran di `infer_schema` menangkap yang sama).

**Node umum langkah 2 (3.4)** `nodes/combine.py` + `nodes/relops.py`. Tidak ada teks SQL di
config: semua pilihan berupa opsi bernama, jadi spec tidak bisa menyelipkan ekspresi.
`relops.apply`/`shape`: satu fungsi SQL per node dipakai `run` (atas relation) dan
`infer_schema` (atas tabel kosong berskema sama), jadi keduanya tidak bisa berbeda. Nama view
unik per panggilan: view bernama sama yang diganti membuat relation lama membaca data baru
(diukur), mutasi nama tetap menjatuhkan 12 tes. Kolom berawalan `__` dicadangkan (`__pos`).
`join`: `on` (nama sama atau `{left,right}`), `how` inner/left/right/full, `suffixes` opsional
(kolom non-kunci bernama sama tanpa suffix = error, tidak diam-diam diganti nama),
`null_equal`; tipe kunci harus sama (cast dulu); kunci bernama sama muncul sekali, di full/right
join diisi dari sisi yang ada. `union`: `by` name (kolom yang tak ada = NULL) atau position
(jumlah kolom sama, nama dari input pertama), tipe disatukan seperti `unpivot`,
`source_column` (nomor input), `distinct`; 2+ input. `group_by`: `by` + `aggs`
`{name, fn, column}` dengan fn tetap (sum avg min max count count_rows count_distinct first last
join_text); hasil berurutan kemunculan pertama grup; `first`/`last`/`join_text` mengikuti urutan
tabel (diuji 200k baris); sum bilangan bulat -> BIGINT (duckdb memberi HUGEINT; overflow =
error, bukan angka salah); sum/avg kolom teks ditolak. `pivot`: `values` WAJIB (kolom hasil harus
diketahui sebelum data dibaca), nilai yang tak muat di tipe kolom ditolak (evaluasi konstanta,
pelajaran `fill_missing`), `prefix`, fn sum avg min max count first last. 14 mutasi: 13
tertangkap; 1 lolos = kode mubazir (`IS NOT DISTINCT FROM` di pivot, nilai config tak bisa
NULL) dan dihapus.

## Temuan P6 Fase 3-4 (dibersihkan 2026-11-07)

Selesai: konversi error jadi dict (`tools.py`), `catalog()` node, pesan config dengan daftar field, pesan `Ident` dalam kata, `Step.inputs` vs `Output.input` (pesan + docstring), saran `cast` untuk sum teks (sudah ada), `drop_regex` NULL (dokumentasi + tes), `until_blank_rows` memotong grand total (peringatan di run), header 2 baris `PARENT|LEAF` (docstring `RegionRef` + tes), contoh spec lengkap (README + tes jalan), pesan drift dengan langkah berikut, `Location.how` `Literal`, `_State.counts` dibuang (satu sumber: `NodeStat`), `end` diparse satu tempat (`RegionRef.blank_gap`), cek `required` satu fungsi (`missing_required`), import privat lintas modul jadi publik (`common_type`, `sql_text`, `check_column`, `reason`, `column_names`, `few`, `text_key`), pemotongan `_MAX_ISSUES`/`_MAX_MESSAGE` bertanda, diff versi menampilkan bagian yang berbeda, `ColumnExpect.type` aktif, `ColumnExpect.group` dihapus, adversarial dijalankan (Fase 3-5 dan 6), `find_text`/`sheet_regions` ada di tools.

Tersisa:

- Peringatan "filled rows below" dihitung dari sel terisi, bukan dari apakah itu baris total; sheet dengan catatan kaki akan memberi peringatan walau tak ada yang salah (ponytail: peringatan, bukan error).
- `rebind` tidak melihat tipe kolom (hanya header); `expect_columns.type` dicek `validate`/`run`.

## Pertanyaan terbuka

- Batas jumlah dataset per job?
- Pemilik target schema Snowflake (nama/tipe kolom)?
- Retensi versi spec & RunReport?
| 2026-11-13 | Hemat token agent L1-L5 (dari log 16 job nyata; docstring tool hanya ~1k token, tidak diubah). (L1) `catalog()` tanpa nama kini memuat field config per node (`types*, strict`) dan `node_catalog("cast, drop_rows")` menerima beberapa nama: satu panggilan menggantikan ~2 (dulu 99 panggilan, 15x `drop_rows`). (L2) `save_spec` dari versi 2 hanya menampilkan schema tabel yang berubah (`_changed_tables`: sumber/step baru atau diubah + hilirnya); `ValidationReport.text(only=)`. Terukur: 240 -> 54 karakter pada save ulang. (L3) `RunReport.text(full=False)` dipakai `run_pipeline`: step yang jalan mulus tanpa catatan dan berbaris > 0 dibuang; `last_run` tetap penuh. 537 -> 218 karakter. (L4) `engine._explain`: error konversi duckdb menyebut `cast`/`drop_rows keep_type`, kolom tak ditemukan menyebut kolom yang dibaca step itu. (L5) tool baru `edit_spec(patch_json, note)` dan `pipeline.patch_spec`: `steps` (id sama = ganti, id baru = disisip sebelum step pembacanya, atau di akhir), `remove`, `sources`/`outputs` (null = hapus); hasilnya lewat `_commit` yang sama dengan `save_spec` (validasi, versi, diff). 23 tool agent (dulu 22): HANDOVER, README, skill build-spec dan recycle diperbarui. **Belum:** pengukuran token nyata di host (butuh `usage_metadata`), tes `edit_spec` pada pola sheet berulang, peringatan `#DIV/0!` per sumber masih satu baris tiap sumber. |

## Log

| Tanggal | Apa |
|---|---|
| 2026-11-12 | Perbaiki total pada `synth`: `drop_rows` memeriksa setiap kolom grain teks, bukan hanya yang pertama; default `total_regex` menambah awalan `jml.` dan `rekap` (hanya awal label, bukan substring nama data); kolom total tidak menjadi kandidat unpivot, termasuk blok `TOTAL|...` yang dibuang sebelum `unpivot_groups`. Steelman satu drop_rows lebih ringkas, tetapi label total di kolom kunci kedua tetap lolos; langkah berurutan menang dan test urutan node disesuaikan. 1236 tes + 25 skip, ruff bersih; 18 tes non-live lab lulus (3 live dibatasi HTTP 429). E2E pupuk lewat HTTP synth `ok` (0,962915), run 156 sheet/517.608 baris; nol baris total pada kabupaten/kode kab. Target uji masih memiliki 936 duplikasi grain dan NULL hasil cast, dilaporkan sebagai masalah kualitas, bukan disembunyikan. |

| 2026-11-12 | S1-S4 synth selesai. `excel_etl/synth.py`: `Target`/`TargetColumn` (ident, grain subset kolom, maks satu `from_header`, `total_regex`, filter lewat `check_expression`), `proposal_regions` (memo `wb.memo`, hanya role source/mixed), `match_columns` (jawaban user 1,0 / exact casefold 1,0 / hint+glossary 0,95 / leaf 0,9 / fuzzy `difflib`*0,8 bila >= 0,75; ragu bila dua teratas beda < 0,1; tipe tak cocok *0,5; header blok berulang dicocokkan per nama leaf), `_build` (fill_down bila grain text bolong; drop_rows total; unpivot|unpivot_groups dari from_header; rename, cast, filter, select; `source_sheet` bila `name_regex`), `_evaluate` (run ke temp dir + skor 0,35 coverage + 0,25 fill + 0,25 unique + 0,15 match; null/unique dihitung dari seluruh hasil lewat SQL, bukan 20 baris pratinjau), `synthesize` (ok >= 0,85 / ask / none; kandidat lain tanpa preview). Lab: `/jobs/{id}/synth` + `/jobs/{id}/synth/publish`, tab Target (form kolom, pertanyaan `select` + `aria-label`, pratinjau, publish). Uji live localhost: synth ok skor 1,0 -> publish -> `/auto` memakai spec. 1204 unit + 25 skip; ruff bersih; lab 20 tes lulus. |
| 2026-11-13 | `synth`: beberapa grup sheet dicentang jadi SATU output. `answers["region"]` boleh berisi beberapa id dipisah koma; `_synthesize_merged` memanggil `_build(prefix="tN_")` per tabel (id source/step unik), `_merge` menumpuk dengan node `union` (by name) -> satu output `target.name`. Tiap tabel wajib memuat target (tidak ada yang dilewati diam-diam, kalau gagal status `none` + alasan). `_evaluate` menerima daftar region; probe memakai satu sheet wakil per sumber. `draft_targets` kini mengembalikan `sheets` (jumlah), `first`/`last` (urut nama), `left_out` (sheet bernama sama bentuk tapi layout beda, hanya info). Lab: tabel grup dengan checkbox + "Use selected". Total regex default juga menangkap "Total" di akhir; kolom kunci angka di kiri blok berulang jadi grain. Uji: stok pupuk (156 sheet) tetap ok 0.983; 2 grup sintetis (60 harian + 3 bulanan) tergabung benar. **Belum:** kolom beda nama antar grup (satu target, satu pemetaan `answers` untuk semua), peringatan sheet baru berlayout beda di Auto, cap `[:40]` tabel. |
| 2026-11-11 | M4 selesai: lab job `m4_daily`, workbook harian 161 sheet, agent 181,5 detik; 1 source + 5 steps, 6 save_spec (5 gagal), output 80.698 baris x28 kolom; publish `m4_daily_stock`, `/auto` ok sebelum dan sesudah P6. Satu sheet tanggal tak sebentuk dikecualikan eksplisit agent. Dokumentasi README/DESIGN/docstring/prompt diperbarui; byte UTF-8 rusak di DESIGN diperbaiki. P6 tiga reviewer: perbaiki typing union inkompatibel -> VARCHAR, tabel kedua tak hilang, rebind casefold, escaping regex pipe, hanya source yang dikelompokkan, docstring group. 1198 unit +25 skip, ruff check/format seluruh repo lulus. |
| 2026-11-11 | M3 selesai: `_shape_groups` memoized mengelompokkan >=3 sheet berdasarkan header region pertama; pola digit `[0-9]{n}` hanya diusulkan bila tak cocok ke sheet luar; map/proposal satu baris per grup, ID asli dan paging sheet tetap. Budget 150 sheet <1500 char. 1193 unit +25 skip lulus. |
| 2026-11-11 | M2 selesai: rebind memakai `locate_all`; record multi menyimpan sheet pertama, gabungan kolom, jumlah sheet; perubahan nama/jumlah sheet bukan diff; sheet cocok yang gagal -> drift, 0 cocok -> missing. `save_spec` memperingatkan >=3 source dengan role/anchor sama; docstring, prompt lab, label graph menjelaskan `name_regex`. 1186 unit + 25 skip; lab 16 tes non-live relevan lulus, assertion lama 6-vs-7 tab tetap gagal di luar M2. |
| 2026-11-11 | M1 multi-sheet selesai: `SheetRef.name_regex`/`sheet_column` + validasi K3 dan regex; `locate_all`, `locate` menolak source multi, `read_locations` union by name dengan kolom sheet dan urutan eksplisit; engine menjumlah warning dan memberi stat sheet. 19 tes sintetis baru. Mutasi fullmatch->search, tanpa ORDER BY (dengan simulasi planner legal), dan skip sheet gagal semua tertangkap. |
| 2026-11-05 | DESIGN.md, AGENTS.md, IMPLEMENTATION.md dibuat. Benchmark calamine/openpyxl, uji sheetwise. |
| 2026-11-05 | 1.1 scaffold: `pyproject.toml` (uv, tanpa build-system: belum perlu dipaket), `excel_etl/__init__.py`, `tests/conftest.py` (`make_xlsx`, fixture `sample_path`), `test_calamine_contract.py`. Ruff format juga cek blok kode di DESIGN.md (dirapikan). |
| 2026-11-05 | 1.2 `workbook.py`: `Workbook.open(path|bytes)` + cache LRU 4 per sha256, `sheets` (visibility), `values` (grid immutable, `""`->None, float bulat->int), `merged` (0-based inklusif), `meta` (formula/bold/fill/border/number_format, openpyxl lazy, ukuran dari calamine). Error input -> `WorkbookError` (tools nanti ubah jadi dict). Koreksi temuan: 4 sheet hidden, bukan 2. |
| 2026-11-05 | 1.3 `inspect/encode.py`: `workbook_map` (id|name|used|cells|formula%|role, paginasi). `Workbook.formula_cells` baru: scan XML, menutup celah calamine (formula tanpa cache terlihat kosong). Peran = ambang % formula (<20 source, >80 derived) sampai lineage 2.1 ada. |
| 2026-11-05 | 1.4 `inspect/regions.py`: `detect_regions` -> `Region` (box, header_rows, merged_header, total_rows, orientation rows/columns/matrix, groups, noise). Komponen 8-terhubung (merged+formula tanpa cache ikut), header naik dari data (parent jarang diterima bila melabeli awal grup), split pada header daun berulang, grup kolom periode 2..8. Kolom `regions` di `workbook_map`. Sinyal style openpyxl belum dipakai (ponytail; terlalu mahal ~4 s/sheet). |
| 2026-11-05 | 1.5 `sheet_regions(wb, file_id, sheet)` di `encode.py`: sheet via nama atau id `F1.S12`, ID region `F1.S12.R{n}` (urutan `detect_regions`), satu baris per tabel berheader (range, hdr, orientasi, total, grup + parent, merged, noise; daftar dipotong `+N`), blok tanpa header satu baris ringkas (ponytail). Paginasi atas tabel. |
| 2026-11-05 | 1.6 `render_range(wb, file_id, ref, range, mode, offset, budget)` di `encode.py`: ref = nama/id sheet atau id region, range A1 opsional. `compressed` (teks per baris/kolom + inverted index + run tipe/rentang per kolom) dan `raw` (`row|A|B`, `^` merged, `=?` formula tanpa cache). Budget dalam char (default 24k, sekitar 6k token) + paginasi per baris output. `tests/test_render.py`. |
| 2026-11-06 | 1.7 + P6 Fase 1: 3 subagent paralel (adversarial/auditor/agent). Perbaikan di `workbook.py` (guard persegi sel, wrap error, zip-bomb cap), `regions.py` (`SheetGrid`, split iteratif, sweep merge), `encode.py` (clamp range, klip baris, validasi paging, docstring LLM). `tests/test_hardening.py` (44 test) termasuk budget 800x160 sintetis < 1k token tanpa file sampel. Total 71 test lulus + 5 sampel. Fase 1 selesai. |
| 2026-11-06 | 2.1 `inspect/lineage.py`: `lineage(wb)` (edge sheet->sheet, sel cross), `Votes`/`sheet_role`/`region_role` (typed/local/cross), tool `formula_lineage` di `encode.py`. `Workbook.formulas()` (teks formula, shared formula diisi dari sel pertama), `Workbook.defined_names()`, `Workbook.memo`. `workbook_map` role dan `sheet_regions` kolom `orientation/role` kini dari lineage; `_role` berbasis persen formula dihapus. `tests/test_lineage.py` (26) + `test_lineage_tool.py`; 3 mutasi (string literal, 3D, nama lokal) sudah dicek menggagalkan test. |
| 2026-11-06 | 2.2 `profile_region(wb, file_id, ref, offset, budget)` di `encode.py`: satu baris per kolom (nama dari header, tipe utama + min..max, distinct, null %, `=?`, `odd` + baris). Butuh id region; helper `_region`, `_fit`, `_check_budget` dipakai bersama `render_range`. `tests/test_profile.py`. |
| 2026-11-06 | 2.3 `find_text(wb, file_id, query, offset, budget)` di `encode.py`: sel teks yang memuat query (substring, NFKC + casefold + kolaps spasi, via `_text_key`), satu baris per sheet (hits + maks 6 posisi + `+N`), flag `[hidden]`, normalisasi teks di-cache di `wb.memo["text_keys"]`. `tests/test_find_text.py` (12). Rencana `profile.py` dilepas: semua tool render hidup di `encode.py`, helper dipakai bersama. |
| 2026-11-06 | 2.4 `compare_sheets(wb, file_id, a, b, budget)` di `encode.py`: verdict + shape + sel terisi yang beda di bagian shared + jumlah formula + region (`_same` type-aware: bool ≠ 1), 6 diff pertama + baris yang memuat diff. Tanpa paginasi. `tests/test_compare.py` (11); formula ber-cache di test via patch XML (`<v />` openpyxl kosong). |
| 2026-11-06 | 2.5 `propose_datasets(wb, file_id, offset, limit)` di `encode.py`: usulan tabel source/mixed (`id|sheet|range|role|rows|cols|2 nama kolom`), head menghitung derived yang ditinggalkan, seksi derived = sheet dengan edge cross + yang dibaca, hasil di-memo per file_id. `tests/test_propose.py` (7). Fase 2 (inspect) selesai; P6 Fase 2 disarankan sebelum fase 3. |
| 2026-11-06 | P6 Fase 2: 3 subagent paralel (adversarial/auditor/agent-user) atas 2.1-2.5. 11 perbaikan di `encode.py`/`workbook.py`: limit=None -> WorkbookError (paged=True di `_paging`), guard localSheetId, truncated `compare_sheets`, gating seksi derived (cross di dalam box), hitungan baris data tanpa total, repr diff float, echo query 40 char, cap 5 nama sheet di error, sheet bernama id region, "has no regions", wording budget. +18 test; 153 unit + 11 sampel lulus. Temuan valid yang tidak diperbaiki dicatat di §Temuan. |
| 2026-11-06 | 3.1 `pipeline.py`: `PipelineSpec` (sources/steps/outputs, pydantic v2, `extra="forbid"`), `parse_spec(json)` -> `SpecError` dengan path per masalah, `dump_spec`. `tests/test_pipeline.py` (31); mutasi (forbid off, cek input off) menjatuhkan tes. 184 unit lulus. |
| 2026-11-06 | 3.2 `nodes/base.py` (`Node`, `@node`, `build`, `infer`, `schema_of`, `quote`, `NodeError`) + `nodes/general.py` (`select`). `tests/test_nodes.py` (10); 3 mutasi (forbid off, infer != run, arity off) tertangkap. `parse_spec` memakai `describe` bersama. 194 unit lulus. |
| 2026-11-06 | 3.3a `nodes/source.py`: `read_region` (sheet via name/name_like/fallback, header via anchor per tabel, akhir via blank rows, tipe per kolom). `nodes/base.py`: `connect()` (duckdb tanpa akses file). `_column_names(sep_safe=)`. `tests/test_read_region.py` (12). Uji BALANSITAS menemukan bug: anchor yang berulang di tabel samping gagal (dinilai per workbook-wide) -> dinilai per tabel. 206 unit lulus. |
| 2026-11-06 | 3.3b `nodes/excel.py`: `drop_rows` (blank, keep_type, drop_regex) dan `fill_down`. `tests/test_excel_nodes.py` (14 + 1 sampel); tiap kasus mengecek `infer_schema == schema_of(run)`; 3 mutasi (keep_type terbalik, tanpa IGNORE NULLS, regex tak NULL-aman) tertangkap. Sampel: (N) PKG 802 -> 730 baris. 221 unit + 13 sampel lulus. |
| 2026-11-06 | 3.3c `unpivot` dan `unpivot_groups` di `nodes/excel.py` (+`_common_type`, `_pick`, `_regex`). Tes memakai 8+ bentuk sintetis (pemisah `|` dan `_`, ukuran tak seragam, nama dengan kutip, tipe campur, workbook dua baris header Q1..Q3 x Sales/Cost), bukan tiruan BALANSITAS. +24 tes; 5 dari 6 mutasi tertangkap, 1 mutasi tanpa efek. 246 unit + 14 sampel lulus. Aturan kerja baru: contoh bukan patokan (memory + Status). |
| 2026-11-06 | 3.3d `nodes/labels.py`: `parse_hierarchy`, `split_header_token`. `tests/test_label_nodes.py` (26): 4+ pemisah, kode salah -> NULL, desimal ditolak, pola/pemisah, nama berkutip, dua workbook sintetis. 8 mutasi: 6 tertangkap, 2 = kode mubazir (dihapus). 272 unit + 14 sampel lulus. 3.3 selesai. |
| 2026-11-06 | 3.4a `nodes/sqlexpr.py`: `check_expression` (parser duckdb + bentuk + whitelist kelas node + deny-list fungsi). `tests/test_sqlexpr.py` (78): ekspresi biasa lolos (24), bentuk/sintaks jahat ditolak (26), fungsi berbahaya di mana pun (20), klausa dalam pemanggilan, panjang/NUL/non-string. Fuzz tata bahasa + bungkus jahat + teks acak bersih. 350 unit lulus. |
| 2026-11-06 | 3.4b node umum langkah 1: `rename cast filter derive fill_missing sort dedupe` di `nodes/general.py`; `check_type`, `bind`, deteksi agregat di `nodes/sqlexpr.py`; `INTEGER_TYPE` bersama di `base.py`. `tests/test_general_nodes.py` (67) + tes agregat. 14+ mutasi; temuan: agregat lolos validator, dedupe salah-urutan pada data besar. 430 unit lulus. |
| 2026-11-06 | 3.4c `nodes/combine.py` (`join`, `union`, `group_by`, `pivot`) + `nodes/relops.py` (`apply`, `shape`, view unik). `tests/test_combine_nodes.py` (54): semua jenis join, union by name/position/3 input, 10 fungsi agregat, pivot 7 fungsi, urutan pada 200k baris, overflow, rantai dua sheet join->derive->group_by->pivot. 13/14 mutasi tertangkap. 484 unit + 14 sampel lulus. 3.4 selesai. |
| 2026-11-06 | 3.5 `engine.py`: `validate(spec, files=None)` + `run(spec, files, out_dir)` lewat `_load_sources`+`_run_steps` yang sama. ValidationReport (ok/issues/warnings/schemas per nama), RunReport (status/issues/nodes/outputs/seconds), `PipelineError` bila tak bisa mulai. Masalah satu step tak menyembunyikan masalah step tak bergantung; lanjutan step gagal tidak menambah issue. Output `.part` + `os.replace` bila semua sukses; `connect(write_dir)` set `allowed_directories` sebelum kunci. Perbaikan urutan hasil ukur: join diurut `__lp`/`__rp`, union distinct simpan salinan pertama urutan tumpuk, dedupe tanpa keys row_number semua kolom, unpivot_groups urut baris lalu blok. `pipeline.py`: nama tolak prefiks `__`, >64 char, beda hanya huruf besar-kecil (duckdb me-rename `A` jadi `A_1`), output = nama device Windows; `source._unique` casefold. Gotcha duckdb: `fetchone()` meninggalkan result terbuka (pakai `fetchall`); bind tabel kosong tak evaluasi konstanta & terima INTEGER vs literal VARCHAR (limitasi terdokumentasi; `expect_columns` cek kehadiran, type/group belum -> warning). `tests/test_engine.py` 35 (+1 sampel end-to-end): validate dengan/tanpa file, masalah independen, sandbox, round-trip nilai/nama aneh, gagal bersih (2 output, direktori penghalang, output lama terlindungi), dua file satu run, urutan 20k baris, HUGEINT->DOUBLE warning. 5 mutasi: 4 tertangkap; 1 lolos (cek skema run != infer) = jaring pengaman bug node, dipertahankan sadar. 524 unit + 15 sampel lulus. 3.5 selesai. |
| 2026-11-06 | 3.6 multi-file per job: tidak perlu kode baru — `file_role` per source + `run(spec, files, out_dir)` sudah mencakupnya (workbook di-cache per role; dua source boleh satu role; `files` terima path/str/bytes/Workbook). Tes tambahan `test_two_files_with_the_same_layout_are_unioned`: dua file layout sama (jan/feb, nama sheet sama) di-union + `source_column` (nomor input 0-based) + `validate` dua role. 525 unit + 15 sampel lulus. Fase 3 selesai. |
| 2026-11-06 | 4.1/4.2/4.4 `rebind.py`: `rebind(spec, files, previous=None)` -> `BindReport` (per source `exact`/`shifted`/`drift`/`missing`, `record`, `diff`, `message`; `.ok`, `.records()`, `.text()`). `nodes/source.py`: `read_region` dipecah jadi `locate()` (sheet + tabel + nama kolom, tanpa muat ke duckdb) + `Location`; `expect_hits`/`expect_label` dipakai bersama `engine._check_columns` (satu sumber kebenaran). Aturan: kolom required hilang / sheet hanya ketemu lewat isi (`fallback_contains`) / kolom tambah-hilang yang tak dicakup `expect_columns` = drift; header/sheet pindah atau kolom tambah-hilang yang dicakup grup = shifted; tak ada `previous` = hanya kontrak + sheet yang dinilai. File jelek/tak ada = `missing`, tidak raise; satu source jelek tak menyembunyikan lainnya. Record JSON-serializable (host simpan; library tanpa state). Bug ditemukan tes: `text()` crash bila record None. `tests/test_rebind.py` (15): geser bawah/kanan/dua tempat, sheet ganti nama, grup kolom tambah/hilang, header 2 baris, missing 4 sebab, JSON round-trip. 8 mutasi, semua tertangkap. ponytail: tipe kolom tak dibandingkan. 539 unit + 16 sampel lulus. |
| 2026-11-06 | 4.3 `versions.py`: `diff_specs(old, new)` -> `Change(where, kind added/removed/changed, before, after)` per path (`sources.pkg.region.header_anchor`), step dicocokkan lewat id (step bergeser = bukan perubahan; urutan tertukar = satu `steps.order`), nilai dipotong 80 char. `SpecHistory` (pydantic beku, JSON): `start`, `add(spec, note)` -> riwayat baru (lama tak berubah), `get(n)`, `current`, `text()`. `add` menolak spec sama dan note kosong (repair yang tak mengubah apa pun menyembunyikan kegagalan). Host menyimpan JSON; library tanpa state. ponytail: list dibandingkan utuh, bukan per item. `tests/test_versions.py` (11); 6 mutasi, 1 lolos (nomor versi tetap 2) lalu ditutup dengan tes 3 versi. 550 unit + 16 sampel lulus. Fase 4 selesai. |
| 2026-11-06 | P6 Fase 3-4 (auditor + agent-user; adversarial dibatalkan). Diperbaiki: `_unique` header kembar beda huruf (`Ton`,`Ton`,`Ton (2)`) -> nama unik tanpa memandang huruf; cek nama kolom di `unique`, `_unique` (general), `_clash`, `union` kini tanpa memandang huruf (duckdb tak membedakan); `_load_sources` tangkap `duckdb.Error`; `check_arity` dipanggil setelah `build` (validate tanpa files tetap cek jumlah input); `os.replace` terjaga (Issue, bukan exception); `validate`/`rebind` dengan `files` bukan dict -> Issue/`missing`; `BindReport.records()` hanya exact/shifted; `ValidationReport.text()` menampilkan nama kolom; `SpecHistory` memvalidasi nomor + diff saat dimuat dan menyalin spec; alias `FileRef` -> `FileInput` (bentrok dengan `FileRef` AGENTS §2); komentar usang; `_FUNCS` mati dan regex ganda dihapus; DESIGN.md §3/§5/§6 disesuaikan. +9 tes, mutasi tertangkap. Ditunda: lihat §Temuan P6 Fase 3-4. 559 unit lulus. |
| 2026-11-06 | Perf `read_region`: `executemany` per baris 112 s untuk 200k baris x 4 kolom (103 s di insert). Diganti satu `INSERT ... SELECT unnest(?::T[]), ...` dengan satu list per kolom: 8.5 s total (insert ~1 s; sisanya locate/detect_regions ~7 s). 559 unit + 16 sampel lulus. |
| 2026-11-06 | 5.1 `knowledge.py`: `Knowledge` (brief, glossary, rules, target_schema, examples, qa_log; `extra=forbid`, JSON round-trip) + `lookup_knowledge(k, query)`: kata utuh tanpa beda huruf, top 5 per seksi, brief per kalimat, examples maks 2, total <= 4000 char; query kosong -> overview; tidak ketemu -> daftar seksi terisi + saran tanya manusia. ponytail: tanpa stemming/fuzzy. 16 tes; 7 mutasi mati (2 selamat -> tes ditambah). |
| 2026-11-06 | 5.2 `hitl.py`: `HitlPolicy` (default sesuai DESIGN §7), `HitlState` (policy, pending, approvals, next_id; semua fungsi murni state -> state baru), `ask_human(state, kind, question, options, ref)` (clarify/confirm/choose; choose >= 2 opsi unik; pertanyaan terbuka yang sama tidak digandakan; maks 20 menunggu), `answer(state, knowledge, id, reply)` -> `qa_log` (choose dicocokkan tanpa beda huruf dan disimpan bentuk kanonik; confirm yes/no/ya/tidak), `approve(state, checkpoint, by, ref)`, `gate(state, checkpoint, failed, ref)` -> None atau alasan (required / required_if_fail / ref basi / pertanyaan masih menunggu), `should_ask(policy, confidence, unknown_term)`. `engine.run(..., hitl=None, ref=None)`: bila `hitl` diberikan, `after_propose_datasets` + `after_spec` dicek sebelum menyentuh disk; ditolak -> `PipelineError` (satu Issue `hitl.<cp>` per checkpoint). Error input -> `HitlError` (ValueError; dikonversi ke dict di `tools.py` Fase 6). ponytail: approval dicek lewat teks `ref`, bukan hash spec. 41+3 tes; 19 mutasi mati. |
| 2026-11-06 | 5.3 `quality.py` + `checks` di spec: `not_null`, `unique`, `range` (angka/tanggal, NULL dilewati, batas dibanding sebagai DOUBLE untuk angka), `accepted_values` (dibanding sebagai teks), `reconcile` (satu agregat sum/count/avg/min/max dari tabel kiri vs tabel kanan atau angka; `by` per grup, FULL OUTER JOIN, grup yang hilang = 0 hanya untuk sum/count; `abs_tol`, `rel_tol` = pecahan sisi terbesar). `severity` error|warn. `validate` menyaring tabel/kolom/tipe tak valid (`checks.<nama>`); `run` menjalankan sesudah semua step, error -> status failed tanpa file ditulis, warn -> `warnings`; `RunReport.checks` (CheckResult: passed, bad_rows, message) tampil di `.text()`. Check pada tabel dari step yang gagal dilewati (tidak dilaporkan dua kali). Bug ditemukan tes: reconcile tanpa `by` yang lolos mengembalikan 0 baris dan judge memakai `rows[0]`. 32 tes; 26 mutasi, semua mati (5 selamat -> tes ditambah, 1 kode mati dihapus). ponytail: accepted_values dibanding teks; satu agregat per reconcile. |
| 2026-11-06 | 5.4 `state.py`: `JobState` (job_id, version, files [FileRef id/filename/sha256, tanpa bytes], roles file_role->file id, knowledge, hitl, history, binds, last_run; `extra=forbid`, konsisten: role menunjuk file ada, id unik), `Job.from_state(json)` / `Job.open(job_id, store)` / `job.update(fn)` (fn(state)->state baru; version dinaikkan oleh job; hasil di-round-trip JSON sebelum disimpan; error apa pun -> state lama tetap), `StateStore` protocol (load/save dengan `expected_version`), `FileStore(dir)` (satu file `job-<id>.json`, tulis `.part` lalu `os.replace`, `StateConflict` bila versi tersimpan beda, file korup ditolak dan tidak ditimpa), `FileProvider` protocol + `open_files(state, provider)` -> `{role: Workbook}` dengan cek sha256. job_id huruf kecil/angka/`_.-` (aman di Windows). `Job.state` dan hasil `update` berupa salinan. ponytail: FileStore cek-lalu-ganti tidak atomik antar proses; production pakai compare-and-set DB host. 45 tes; 22 mutasi, semua mati (1 selamat -> tes ditambah, 1 ekuivalen). |
| 2026-11-07 | P6 Fase 3-5, dikerjakan sendiri (tanpa subagent): adversarial lewat ~70 probe skrip, auditor lewat grep aturan keras + baca DESIGN vs kode, agent-user lewat menulis spec dari dokumen + 16 kesalahan tipikal. DIPERBAIKI: (1) regex `$` menerima newline di akhir (`job_id`, `sha256`) dan `\d` menerima digit non-ASCII (`until_blank_rows(٢)` lolos validasi lalu `int()` gagal) -> `fullmatch` + `[0-9]`; (2) `Job.update(fn)` memberi `fn` objek state asli, mutasi di tempat merusak state walau update ditolak -> `fn` menerima salinan dalam; (3) `engine.run` dengan `hitl`/`out_dir` bertipe salah melempar AttributeError/TypeError -> `PipelineError`; (4) `range` dengan batas tak terbaca (`'abc'`, `nan`, `'2026-13-45'`) baru gagal saat run dengan pesan duckdb mentah -> `_Bad` terdeteksi di `validate` dengan contoh bentuk yang benar; (5) `_NUMERIC` digandakan di excel.py dan quality.py -> `NUMERIC_TYPE` di `nodes/base.py`; (6) agent tidak bisa menemukan field config node -> `nodes.catalog(name=None)` (satu baris per node, atau docstring + field `nama*: tipe = default` per node) dan pesan error config node memuat `fields (* required): ...`; (7) DESIGN §7/§9/§10 diselaraskan dengan kode. Dibuang: guard bool di `_check_bound` (pydantic sudah mengubah `True` jadi 1). 24 tes baru; 17 mutasi mati. |
| 2026-11-07 | 6.1 `tools.py`: `JobTools(job, provider, out_dir)`. Semua tool mengembalikan dict (`text` atau `error`), tidak pernah exception: `PipelineError` -> issues per baris, `StateConflict` -> `conflict: true` + 'Nothing was changed', error domain (Spec/Node/Hitl/State/Workbook/Value/Key) -> pesan, bug lain -> `internal error (Tipe): ... library bug`; argumen salah panggil dilaporkan dengan daftar parameter. Teks dipotong 24000 char + `truncated`. Tool agent (21): job_summary list_files workbook_map sheet_regions render_range profile_region find_text compare_sheets formula_lineage propose_datasets node_catalog lookup_knowledge ask_human pending_questions save_spec get_spec validate_spec rebind_sources run_pipeline last_run spec_history. Tool host (3, tidak diberikan ke agent): add_file answer approve. File dirujuk `F1..` (juga nama file / id host), hash dicek lewat `open_ref`. `save_spec` memvalidasi dulu dan menolak spec bermasalah; `run_pipeline` memakai `hitl` + `ref=v<N>` (persetujuan lama tidak berlaku untuk versi baru), mengingatkan `after_run`; `rebind_sources` menyimpan `binds` dan memberi tahu bila `on_rebind_drift` butuh persetujuan. Skema datar (tanpa None/Optional; default `""`/`[]`) dijaga tes. Tes sekali jalan: argumen hostile ke semua tool (None, 5, [], ' ', 5000 char) tidak ada `internal error`. 25 tes; 19 mutasi mati (3 selamat -> tes ditambah). |
| 2026-11-07 | 6.2 `README.md`: apa yang disediakan host (file provider, state/store, out_dir), snippet mode store dan stateless, pembungkus `LlmAgent` + `FunctionTool(fn) for fn in tools.agent_tools()`, instruksi awal untuk agent, tabel tool. Diverifikasi dengan google-adk dari venv PINTER: `FunctionTool` menerima method terikat apa adanya (nama, docstring, default dipakai) dan ke-24 tool menghasilkan skema tanpa `anyOf`/`oneOf`/`$ref`/`$defs`/`allOf`. `tests/test_readme.py` (3) menjalankan dua blok python README (store + stateless) dan mencocokkan tabel tool dengan `agent_tools()`; 5 mutasi README mati. Contoh LiteLLM ditulis sebagai catatan saja, belum diuji (6.3). |
| 2026-11-07 | P6 Fase 6 (sendiri, tanpa subagent): adversarial ~60 probe lewat `JobTools` (workbook aneh: kosong/1 sel/40 sheet/unicode/error cell/header kembar; urutan panggilan tak lazim; argumen salah tipe/negatif/raksasa; JSON bersarang 100k; 3000 source; 300 versi; qa_log; dua penulis; provider rusak), auditor (docstring tiap tool, `hint` di AGENTS.md vs kode), agent-user (BALANSITAS sungguhan: map -> propose_datasets -> profile -> render -> node_catalog -> save_spec -> run -> rebind, total ~1800 token output). DIPERBAIKI: (1) exception apa pun dari `provider.open` (bukan hanya OSError/KeyError) jadi `internal error ... library bug`; sekarang `read_file` mengubahnya jadi error host; (2) catatan versi spec tak terbatas (100k char tersimpan di state; pydantic membocorkan `input_value` 100k) -> `MAX_NOTE=500`, pesan bersih di `save_spec`; (3) `pydantic.ValidationError` bocor mentah dari tool -> `describe`; (4) run ditolak checkpoint: agent tidak tahu harus apa -> pesan menambahkan 'use ask_human ... host approves'; (5) `node_catalog` menampilkan `value_type: {}` dan `suffixes: [any]` -> enum bernama dan list ukuran tetap ditampilkan (`VARCHAR|DOUBLE|...`, `[string, string]`); (6) AGENTS.md §4 menyebut `hint`, kode tidak -> AGENTS disesuaikan (pesan sudah memuat apa yang diubah). 12 tes baru; mutasi mati. Terukur: `Job.update` ~40 ms pada state 150 KB (300 versi spec), deepcopy dominan: aman untuk satu agent, dicatat sebagai batas. |
| 2026-11-07 | Bersihkan temuan P6 Fase 3-4. Pesan: `describe` menjelaskan nama tak valid dalam kata (bukan regex), list vs satu nama, key salah ('is not a setting here'). `RegionRef`/`Step`/`Output` didokumentasikan (header 2 baris `PARENT|LEAF`, `inputs` list vs `input` satu nama, `end`). `end` diparse satu tempat (`RegionRef.blank_gap`); `locate` menghitung `last` dan `below`, run memberi peringatan bila ada baris terisi setelah akhir tabel (grand total terpotong); `read_location` memuat dari lokasi yang sudah dicari (engine memanggil `locate` sekali). `ColumnExpect.type` aktif (`type_fits`: float menerima bilangan bulat) dan gagal sebagai issue, `group` dihapus (mati). Pesan drift memuat langkah berikut. `_State.counts` dibuang, `issue()` menghitung yang tak muat (`N more problem(s) not shown`) dan memotong dengan `...`; diff versi memotong di sekitar beda pertama (perubahan di ekor tak lagi tersembunyi). `missing_required` satu fungsi untuk engine dan rebind; helper privat lintas modul jadi publik. Contoh spec lengkap di README dijalankan oleh tes. Bug ditemukan sambil jalan: `lit` lokal menimpa fungsi saat rename (dipakai `sql_text`). 786 unit + 16 sampel; 13 mutasi mati. |
| 2026-11-07 | Bersihkan temuan P6 Fase 3-5 dan 6. `state.py`: `FileStore.save` di bawah kunci `O_EXCL` (`_locked`; kunci basi 30 s diambil alih; menunggu 5 s lalu `StateError`), `_replace` dan `load` mengulang bila Windows menolak sesaat (tes banyak thread + pembaca serentak, 60x lari bersih), BOM diterima, `Job.reload`, `open_files(roles=)`. `tools.py`: tool host `reload_job`, `_open_all(spec)` hanya role yang dipakai, `per_version`, `_cap` selalu memberi `next_offset`. `quality.py`: `accepted_values` pada kolom angka dibanding sebagai angka (`_Bad` bila nilai spec bukan angka), `Side.where` (`check_expression` + `bind` seperti `filter`; `NodeError` diubah jadi `ValueError` agar `parse_spec` melapor `SpecError`). `engine.py`: `NodeStat.note` dari `Node.why_rows_change()` (filter, drop_rows, dedupe, join, union, group_by, pivot, unpivot), peringatan error cell (`Workbook.error_cells` dari XML `t="e"`, dihitung per tabel di `Location.errors`). Bug ditemukan sambil jalan: `\b` di string heredoc python menjadi karakter backspace (regex `_ERROR_CELL` rusak; tes menangkapnya); tes `open_files` memanggil `make_xlsx` dua kali sehingga hash beda (zip berisi timestamp). 33 tes baru; 29 mutasi mati. |
| 2026-11-07 | `HANDOVER.md` (ringkasan serah): apa yang diberikan host, satu putaran agent, apa yang hanya boleh dipanggil host (`add_file`, `answer`, `approve`, `reload_job`), alur approval, tabel checkpoint dan siapa yang menegakkannya, jaminan, batasan, pertanyaan produk. `tests/test_handover.py` (9) menjaga klaimnya: tabel checkpoint = kode (default, `after_spec` hanya memblok bila `required`, `after_run` dan `on_rebind_drift` hanya mengingatkan, `after_workbook_map` belum dipakai), snippet pengaturan knowledge/policy dijalankan, jumlah tool dan angka batas (25 juta sel, 24000 char) cocok dengan kode. Tes menemukan satu kesalahan dokumen (mode `after_run` ditulis bebas, di kode `required_if_fail`); 7 mutasi dokumen/kode mati. |
| 2026-11-08 | Perbaiki bug dari review AI eksternal (diverifikasi dulu satu per satu). (1) Tabel semua-teks (master/mapping) tak punya header terdeteksi karena `is_data` butuh angka: `regions._text_header` menerima baris atas sebagai header bila blok >= 4 baris, >= 2 kolom, tanpa angka dan tanpa formula, header teks unik, >= 3 dari 4 baris isi mostly terisi, dan header berhuruf tebal ATAU tak ada teks header yang berulang di bawah kolomnya; `lineage.region_role` memberi `source` pada blok teks >= 2x2 dengan >= 4 teks (satu label atau satu baris tetap `empty`). Diukur pada BALANSITAS: 3825 region, 0 sheet berubah. Awalnya 3 region berubah (potongan tabel angka: baris pengiriman) karena aturan tanpa syarat 'tanpa angka'; syarat itu ditambahkan setelah mengukur. (2) `FileStore.load` memperlakukan `NotADirectoryError` (Linux) sama dengan `FileNotFoundError` (Windows); tes `test_store_save_failure_keeps_job_state` tak lagi bergantung kode galat OS (mkdir ditolak lewat monkeypatch) + tes dua kode galat. (3) `JobTools.approve` tanpa `ref` memakai `v<N>` spec yang tersimpan; pesan 'approved for None' jadi 'approved for no version, but this is v1'. (4) `"^P\d$"` jadi raw string; `tests/test_source_hygiene.py` mengompilasi semua .py dengan peringatan terekam. Tambahan: sandbox menolak `pg_sleep`, `pg_sleep_for`, `pg_sleep_until`, `sleep`, `version`, `duckdb_version`, `pg_backend_pid`, `txid_current`, `current_query` (`pg_sleep(2)` terbukti menahan run 2 s; `current_date`/`current_user` adalah referensi kolom di parser, bukan fungsi, jadi tidak ditolak); `.github/workflows/ci.yml` (ruff, format, `compileall -W error`, pytest di ubuntu dan windows); 'Struktur target' disamakan dengan isi repo; README menyebut Moyung-ETL = excel-etl. 861 unit + 16 sampel; 24 mutasi mati (beberapa lolos dulu, dipertajam). Tes Linux sungguhan belum dijalankan (tak ada Linux di mesin ini): CI yang akan membuktikannya. |
| 2026-11-08 | Latihan end-to-end (map, propose, save_spec, run) menemukan peran sheet `workbook_map` untuk sheet teks masih `empty` padahal region-nya `source`: `sheet_role` memakai aturan teks yang sama (`_is_text_table` pada kotak terpakai). DESIGN: status baris awal diperbarui (Fase 1-6 berkode). 862 unit + 16 sampel; mutasi mati. |
| 2026-11-08 | README: bagian 'Steps a spec can use' (18 node dan batas ekspresi) dan 'Where the ideas come from' (SpreadsheetLLM, SheetBrain, sheetwise, DocETL, parse_excel PINTER; tautan diverifikasi lewat pencarian, klaim kompresi 35x dari ukuran terukur 985 vs ~34.000 token). `tests/test_readme.py`: tabel node = registry, tautan sumber lengkap, dependency tetap empat. 4 mutasi mati (satu lolos dulu: substring, dipertajam). Bagian 'Planned' ditambahkan: contoh ekspresi, node `window`, plugin coding agent. |
| 2026-11-08 | `Node.examples` (config + catatan) dicetak oleh `catalog(node)`; 10 contoh `derive`, 6 `filter` (teks, regex, tanggal, CASE, nullif untuk bagi-0 yang jadi `inf`). Node `window` (13 fungsi: sum/avg/min/max/count berjalan atau total grup, row_number, rank, dense_rank, lag, lead, first_value, last_value, share; `partition_by`, `order_by` dengan `-`, `offset`); urutan baris dijaga lewat `__pos`, seri diputus oleh posisi tabel, `rank` memakai seri yang sama; `Sort._keys` jadi `_order_keys` bersama; pesan ekspresi dengan OVER kini menunjuk node `window`. `tests/test_node_examples.py` (tiap contoh dijalankan, nilai dicek, katalog mencetak persis), `tests/test_window.py` (76 kasus melawan referensi Python pada 4 bentuk data: banyak grup kecil, satu grup, kunci NULL, banyak seri; tipe, tepi, penolakan, 200k baris ~1 s). Mutasi: 22, semua mati (satu NOOP karena sed, diulang). Sampel BALANSITAS 16 lulus. |
| 2026-11-08 | `skills/`: 4 skill format Agent Skills (`excel-etl-understand`, `-build-spec` + `references/patterns.md`, `-run-verify`, `-recycle`), agnostik agent. `tests/test_skills.py` (31): format (nama = folder, deskripsi, <= 150 baris, aturan Do not / Stop and ask), semua nama tool/node/setting ada di kode, 21 tool agent semua disebut, tiap snippet JSON valid dan dibangun lewat node, langkah wide-to-long dijalankan. Temuan: DESIGN menulis peran sheet `dashboard/chart_data/form`, kode hanya `source/mixed/derived/empty`; DESIGN diperbaiki, skill memakai peran nyata. 15 mutasi mati. |
| 2026-11-09 | Fase 7: muat hasil ETL ke warehouse sebagai RENCANA, bukan koneksi. `Output.load` (`LoadTarget`: table, mode replace/append/merge, keys; ditolak: merge tanpa keys, keys di luar merge, csv, dua output ke satu tabel; `"load": null` tidak ikut di dump). `excel_etl/load.py`: `Profile` per platform (Snowflake, Databricks, BigQuery: kutip, panjang nama, aturan nama, peta tipe, dari dokumentasi vendor yang dibaca 2026-11-08), `plan_load` (kolom dari file parquet, bukan spec; langkah upload/create/copy/merge/cleanup + SQL), `compare_target` (DESCRIBE host vs file: kolom baru, tipe lain, kolom lebih), `file_columns`. Checkpoint baru `before_load` (default required, per versi spec); tool agent `plan_load` (tool ke-22) dan tool host `record_load` (`JobState.loads`); skill `excel-etl-load`. Semua nama lewat `Profile.quote`, semua nilai lewat `_literal`, jalur stage divalidasi. Tes: `test_load.py` (100 kasus, termasuk parse tiap pernyataan dalam dialek aslinya bila `sqlglot` ada), `test_load_flow.py` (alur dari spec ke tabel di duckdb lokal untuk replace, append, merge), `test_pipeline.py`, skills/readme. Mutasi 50, semua mati. BELUM TERBUKTI: SQL tidak pernah dijalankan di Snowflake, Databricks atau BigQuery; dua hal sengaja ditandai di peringatan rencana (timestamp tanpa zona ke BigQuery DATETIME; batas panjang nama kolom Databricks tidak ditemukan di dokumen, dipakai 255). |
| 2026-11-12 | Scan bug `synth` + perbaikan (3 subagent: adversarial, auditor, pengguna; 1229 tes hijau). **Diperbaiki:** (1) `match_columns`: blok `grup\|metrik` dikenali walau kolom kunci > 1; jawaban `col:` menang mutlak (skor 2.0, dibatasi 1.0 di laporan) sehingga tak ada loop HITL; prefix hint 0.85; `_` dianggap spasi. (2) `_build`: kolom opsional tak ketemu dilewati dari `cast/select` (+ catatan di `problems`); header sumber tak terpakai yang sama nama dengan kolom target di-`select` keluar; `source_sheet` dipilih bebas-bentrok (casefold, `_free_name`) dan ikut `keep` unpivot; kolom `int` yang dibulatkan dicatat di `reasons`. (3) `SynthReport.notes`: alasan region dibuang (validasi/run/tak ada data), batas 40 region, header yang dilihat bila tak ada yang cocok. `_evaluate` mengembalikan `(kandidat, alasan)`; `run().warnings` masuk `problems`. (4) `_shape_groups`: nama sheet tak berpola dicantumkan sebagai `(?:a\|b\|...)` (batas 600 karakter); sheet bentuk-sama bernama beda (Template) tak merusak pola bertanggal; pengecualian sheet rusak lewat lookahead. `_representative`: wakil grup = sheet pertama yang berisi angka, bukan template kosong. (5) `regions.is_data`: `nums >= 1 and nums*2 >= texts` (tabel teks+teks+angka kini punya header). (6) `sqlexpr.check_expression`: ekspresi harus valid juga TANPA pembungkus `SELECT (...)`; `val) OR (1=1` ditolak (sebelumnya kurung tak seimbang lolos dan memutus filter lain). (7) `cast.decimal` (`"."`/`","`) + `Target.decimal`: teks `1.234,56`/`2.000` dibaca benar; hanya menyentuh kolom teks. (8) `Workbook._cal_lock`: calamine tidak thread-safe (`Already borrowed`). **Belum:** baris total tak dikenal (`Jml.`, `Rekap`), kolom `Total` di kanan ikut unpivot, tabel kedua dalam satu sheet (hanya peringatan run), tabel transpos, header tanggal/angka, sheet bernama mirip dengan isi beda ikut tergabung tanpa tanda, ambang prefix tanpa panjang minimum, `_cache`/`meta`/`_scan` tak terkunci, `proposals[:40]` hanya dicatat. File `nul` di root `excel-etl` (artefak Windows) tak bisa dihapus dari shell; hapus manual. |
| 2026-11-13 | Perbaikan dari diagnosa bug. (1) `RunReport` mencatat `ref` (versi spec) dan `files` (role -> sha); `plan_load` menolak run yang dibuat untuk versi spec lain atau file yang sudah diganti, jadi parquet lama tidak lagi dimuat atas approval versi baru. (2) Rencana load terakhir disimpan di `JobState.planned`, bukan di memori `JobTools`: `record_load` di turn berikutnya (objek baru, sesuai HANDOVER) jalan, dan `ref` yang dicatat adalah versi saat plan dibuat. (3) `nodes.base.infer` menolak dua kolom yang hanya beda huruf besar/kecil (duckdb menamai ulang jadi `X_1`, run lalu gagal dengan pesan "bug in the node"); `window` mengecek namanya sendiri karena skemanya diturunkan lewat duckdb. (4) `patch_spec` memeriksa bentuk `remove`/`steps`/`sources`/`outputs`. (5) `note`, `node_catalog(node)`, `add_file(role)` bertipe salah jadi error biasa; `node_catalog(",")` = daftar lengkap. (6) Peringatan bila `header_rows` masuk ke baris yang dilihat `sheet_regions` sebagai data. (7) `range` dengan `min > max` ditolak di spec. (8) Petunjuk cast yang tidak didukung duckdb tidak lagi menyalahkan "teks campur angka" (tidak bisa ditangkap saat validate: NULL lolos cast). (9) Kolom teks campuran menulis bool sebagai `TRUE`/`FALSE` dan durasi sebagai `30:00:00`, seperti Excel. Tes: `tests/test_name_case.py` + tambahan di test_load_flow, test_tools, test_engine, test_quality. |
