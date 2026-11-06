# IMPLEMENTATION.md — excel-etl

Dokumen hidup: status, rencana per fase, keputusan, temuan, log.
Perbarui setiap selesai satu item (lihat AGENTS.md §7).

## Status

- Fase: **1, 2 selesai (termasuk P6). Fase 3: 3.1, 3.2 selesai; 3.3 selesai (`read_region`, `drop_rows`, `fill_down`, `unpivot`, `unpivot_groups`, `parse_hierarchy`, `split_header_token`).**
- 3.4 sebagian: **validator ekspresi SQL selesai** (`nodes/sqlexpr.py`).
- Berikutnya: **3.4 lanjut: node umum** (`rename`, `cast`, `filter`, `dedupe`, `sort`, `fill_missing`, `derive`, `join`, `union`, `group_by`, `pivot`); `filter` dan `derive` wajib memanggil `check_expression`.
- **Aturan kerja (dari user): BALANSITAS hanya satu contoh, bukan patokan bentuk.** Node berbasis pola umum dan config eksplisit; tes memakai beberapa bentuk sintetis berbeda; sampel hanya sanity. Temuan P6 Fase 1/2 yang ditunda: lihat §Temuan.

## Berikutnya

1. 3.4 node umum (`rename`, `cast`, `filter`, `dedupe`, `sort`, `fill_missing`, `derive`, `join`, `union`, `group_by`, `pivot`); `filter`/`derive` memakai `check_expression`. Catatan: `fill_down` dan `sort` bergantung pada urutan baris; `join`/`group_by` tidak menjamin urutan. `flatten_header` sudah terjadi di `read_region`: kolom sudah `PARENT|LEAF`, node terpisah hanya bila perlu.
2. 3.4-3.6 node + registry + `validate`/`run` (lihat §Fase 3).
3. Sambil jalan: temuan P6 yang ditunda (konversi WorkbookError ke dict di `tools.py`).

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
      encode.py        # render teks untuk LLM (map, regions, compressed, raw, profile,
                      #  find_text; compare_sheets menyusul di sini)
      lineage.py       # formula -> graf sheet, peran source/derived/mixed
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
- [x] 2.3 `find_text(file, query)`: case-insensitive, normalisasi spasi/unicode.
- [x] 2.4 `compare_sheets(a, b)`: kemiripan struktur + nilai (deteksi `X (2)`).
- [x] 2.5 `propose_datasets(files)`: usulan dataset dari region `source`/`mixed`, turunan
      dijadikan target rekonsiliasi. Output untuk dipilih manusia.

## Fase 3 — Node + pipeline

- [x] 3.1 `PipelineSpec` (pydantic) + parse dari JSON string (YAML ditunda, lihat §Keputusan).
- [x] 3.2 Registry `@node`, kontrak `infer_schema` + `run` (duckdb relation).
- [x] 3.3 Node Excel-spesifik: `read_region` (source), `drop_rows`, `fill_down`, `unpivot`, `unpivot_groups`, `parse_hierarchy`, `split_header_token`. `flatten_header` = bagian `read_region`.
- [ ] 3.4 Node umum + validasi ekspresi SQL skalar. Selesai: validator `check_expression`. Sisa: node umum.
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
| 2026-11-06 | propose_datasets: seksi derived level-sheet (edge lineage), bukan level-region | resolusi region->source butuh kerja per sel; presisinya baru berguna di fase quality (rekonsiliasi), ditunda ke sana |
| 2026-11-05 | State: dua mode, stateless (host pegang JSON) atau `StateStore` | fleksibel untuk host |
| 2026-11-05 | HITL via `ask_human` + `HitlPolicy` config | penempatan checkpoint fleksibel tanpa ubah kode |
| 2026-11-05 | Tolak `sheetwise` sebagai dependency, port idenya | lihat §Temuan |
| 2026-11-05 | Tolak `SheetBrain` sebagai dependency, ambil pola U-E-V + sandbox | terikat OpenAI/tiktoken/multimodal |
| 2026-11-06 | Spec diterima sebagai JSON saja, tanpa `pyyaml` | AGENTS.md batasi dependency; agent LLM menulis JSON andal, YAML = dependency baru untuk kenyamanan. Tambah bila host butuh (parse YAML -> dict -> `PipelineSpec.model_validate`). `SpecError` memberi hint bila input terlihat YAML. |

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
