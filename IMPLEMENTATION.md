# IMPLEMENTATION.md — excel-etl

Dokumen hidup: status, rencana per fase, keputusan, temuan, log.
Perbarui setiap selesai satu item (lihat AGENTS.md §7).

## Status

- Fase: **1 selesai. Fase 2: 2.1, 2.2 selesai.**
- Berikutnya: **Fase 2, item 2.3** (`find_text`). Lihat §Temuan "P6 Fase 1" untuk temuan yang ditunda; sebagian (tipe per kolom, kolom tanggal) dijawab 2.2.

## Berikutnya

1. 2.3 `find_text(file, query)`: case-insensitive, normalisasi spasi/unicode.
2. Sambil jalan: temuan P6 yang ditunda (kolom tanggal/subtotal di `sheet_regions`, `workbook_map` role "mixed", konversi WorkbookError ke dict di `tools.py`).

## Struktur target

```
excel-etl/
  AGENTS.md  IMPLEMENTATION.md  DESIGN.md
  pyproject.toml
  excel_etl/
    __init__.py
    workbook.py        # handle file: calamine + openpyxl lazy, cache sha256
    inspect/
      __init__.py
      regions.py       # deteksi region/header/total (port parse_excel/parser.py)
      encode.py        # render teks untuk LLM (map, regions, compressed, raw)
      lineage.py       # formula -> graf sheet, peran source/derived/mixed
      profile.py       # profil kolom, find_text, compare_sheets
    nodes/
      __init__.py      # registry @node
      excel.py         # flatten_header, fill_down, parse_hierarchy, unpivot(_groups), drop_rows, split_header_token
      common.py        # select, rename, cast, filter, dedupe, sort, fill_missing, derive, join, union, group_by, pivot
      io.py            # read_region, write_output
    pipeline.py        # PipelineSpec, validate, run, rebind, BindReport
    state.py           # JobState, Job, StateStore, FileStore, FileProvider
    hitl.py            # HitlPolicy, PendingQuestion, ask_human, answer
    knowledge.py       # Knowledge, lookup_knowledge
    quality.py         # assert + rekonsiliasi, RunReport
    tools.py           # permukaan publik untuk agent
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
- [ ] 2.3 `find_text(file, query)`: case-insensitive, normalisasi spasi/unicode.
- [ ] 2.4 `compare_sheets(a, b)`: kemiripan struktur + nilai (deteksi `X (2)`).
- [ ] 2.5 `propose_datasets(files)`: usulan dataset dari region `source`/`mixed`,
      turunan dijadikan target rekonsiliasi. Output untuk dipilih manusia.

## Fase 3 — Node + pipeline

- [ ] 3.1 `PipelineSpec` (pydantic) + parse dari YAML/JSON string.
- [ ] 3.2 Registry `@node`, kontrak `infer_schema` + `run` (duckdb relation).
- [ ] 3.3 Node Excel-spesifik.
- [ ] 3.4 Node umum + validasi ekspresi SQL skalar.
- [ ] 3.5 `validate(spec)` tanpa data; `run(spec)` -> output parquet/csv + RunReport.
- [ ] 3.6 Multi-file per job (join/union antar `F1`, `F2`).

## Fase 4 — Recycle

- [ ] 4.1 Anchor binding: sheet (`name_like`, `fallback_contains`), region
      (`header_anchor`, `header_rows`, `end`), kontrak kolom (`match`, `match_regex`,
      `group`, `required`).
- [ ] 4.2 `rebind(spec, files)` -> BindReport (`exact`/`shifted`/`drift`/`missing`) + diff.
- [ ] 4.3 Versi spec + diff saat repair.
- [ ] 4.4 Test: file sintetis bergeser baris/kolom, kolom grup bertambah, sheet ganti nama.

## Fase 5 — HITL, Knowledge, Quality, State

- [ ] 5.1 `Knowledge` + `lookup_knowledge`.
- [ ] 5.2 `HitlPolicy`, `PendingQuestion`, `ask_human`, `answer` -> qa_log.
      `run` menolak bila checkpoint `required` belum approved.
- [ ] 5.3 `quality.py`: assert + rekonsiliasi dengan toleransi.
- [ ] 5.4 `state.py`: `JobState`, `Job.from_state` / `Job.open(store=)`,
      `StateStore` protocol, `FileStore` (optimistic lock `expected_version`),
      `FileProvider`.

## Fase 6 — Serah ke host

- [ ] 6.1 `tools.py` final: daftar fungsi + docstring LLM.
- [ ] 6.2 Contoh pembungkus ADK `FunctionTool` (di README/snippet, bukan dependency).
- [ ] 6.3 Uji schema tool lewat LiteLLM ke >= 2 provider (oleh rekan / bersama).

## Keputusan

| Tanggal | Keputusan | Alasan |
|---|---|---|
| 2026-11-05 | Library Python murni, bukan framework/plugin/MCP | host (PINTER) yang mengorkestrasi |
| 2026-11-05 | calamine untuk nilai + openpyxl lazy untuk formula/style | 1,0 s vs 16 s (15 MB, 72 sheet) |
| 2026-11-05 | duckdb sebagai engine + SQL skalar sebagai expression | volume bervariasi, tanpa expression language sendiri |
| 2026-11-05 | Spec pakai anchor semantik untuk recycle | tiap file beda bentuk, alamat sel tidak stabil |
| 2026-11-05 | Default: dataset dari sheet source; derived untuk rekonsiliasi | cegah hitung dobel |
| 2026-11-05 | State: dua mode, stateless (host pegang JSON) atau `StateStore` | fleksibel untuk host |
| 2026-11-05 | HITL via `ask_human` + `HitlPolicy` config | penempatan checkpoint fleksibel tanpa ubah kode |
| 2026-11-05 | Tolak `sheetwise` sebagai dependency, port idenya | lihat §Temuan |
| 2026-11-05 | Tolak `SheetBrain` sebagai dependency, ambil pola U-E-V + sandbox | terikat OpenAI/tiktoken/multimodal |

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

## Pertanyaan terbuka

- Batas jumlah dataset per job?
- Pemilik target schema Snowflake (nama/tipe kolom)?
- Retensi versi spec & RunReport?

## Log

| Tanggal | Apa |
|---|---|
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
