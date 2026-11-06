# excel-etl — Design

Library Python murni: tools untuk agent LLM membangun, menjalankan, dan me-recycle
pipeline ETL dari file Excel raw (multi-sheet, multi-tabel, bentuk tak seragam)
sampai menghasilkan data bersih. Load ke Snowflake di luar scope (fase berikut).

Status: draft desain, belum ada kode.

## 1. Batasan

| Masuk scope | Di luar scope |
|---|---|
| Memahami workbook (sheet, region, header, lineage) | Orkestrasi agent, prompt, loop LLM (tugas host) |
| Node transformasi + engine eksekusi | Persistensi state (DB, storage) |
| Spec pipeline + validasi + rebind ke file baru | Transport HITL (SSE, UI, chat) |
| HITL policy + objek pertanyaan | Load ke Snowflake |
| Laporan kualitas + rekonsiliasi | MCP server |

Host = PINTER (dibungkus ADK `FunctionTool`, model via LiteLLM). Library tidak
import ADK, FastAPI, LiteLLM, atau DB.

## 2. Keputusan dependency

| Lib | Peran | Alasan |
|---|---|---|
| `python-calamine` | nilai sel, merged, visibility | 1.0 s vs openpyxl 16 s pada file 15 MB / 72 sheet |
| `openpyxl` (read_only, lazy per sheet) | formula, style, number format, defined names | calamine tidak punya; dipakai hanya bila tool butuh, hasil di-cache |
| `duckdb` | engine transform + ekspresi SQL | cepat, out-of-core untuk volume bervariasi, SQL = expression language gratis |
| `pydantic` v2 | config node, spec, JSON schema tool | schema otomatis untuk FunctionTool |

Ditolak: `sheetwise` (input pandas DataFrame, merged/style/region hilang,
dependency berat), `SheetBrain` (terikat OpenAI + tiktoken + multimodal).
Ide keduanya di-port: kompresi SpreadsheetLLM, siklus Understand-Execute-Validate,
sandbox deny-by-default.

Fakta file referensi BALANSITAS: 72 sheet, 542.358 sel formula, 14.010 defined
names, 0 ListObject. openpyxl membuang conditional-formatting extension dan WMF.

## 3. Lapisan

```
excel_etl/
  workbook.py     # Workbook handle: calamine + openpyxl lazy, cache per file hash
  inspect/        # tools memahami Excel
  nodes/          # node transformasi (registry @node)
  pipeline.py     # Spec, validate, run, rebind
  hitl.py         # HitlPolicy, PendingQuestion
  knowledge.py    # Knowledge (brief, glossary, rules, qa_log)
  quality.py      # assert + rekonsiliasi
  tools.py        # fungsi publik untuk agent (satu-satunya permukaan ke host)
```

### 3.1 Siklus kerja (per job)

```
Understand  workbook_map -> sheet_regions -> render_range -> lineage
Propose     propose_datasets  -> [HITL: manusia pilih dataset]
Build       spec draft -> validate (schema-only, tanpa data) -> [HITL opsional]
Execute     run di duckdb -> output parquet/csv
Validate    quality + rekonsiliasi -> [HITL bila gagal] -> repair -> ulang
Recycle     file baru -> rebind -> run (tanpa LLM bila match)
```

## 4. Tools inspect (untuk agent)

Semua output dibatasi ukuran, paginasi `offset/limit`, flag `truncated`.
ID stabil: `F1` (file), `F1.S12` (sheet), `F1.S12.R2` (region).

| Tool | Output ringkas |
|---|---|
| `list_files()` | file dalam job |
| `workbook_map(file)` | per sheet: nama, visible, ukuran terpakai, jumlah region, % formula, tebakan peran (`source`/`derived`/`dashboard`/`chart_data`/`form`/`empty`) |
| `sheet_regions(sheet)` | blok tabel: range, baris header (bisa >1), merged di header, orientasi, baris total terdeteksi, noise di atas |
| `render_range(sheet, range, mode)` | grid ringkas ala SpreadsheetLLM: `raw` / `compressed` (anchor + inverted index + tipe agregat) dengan budget token |
| `formula_lineage(file)` | graf sheet->sheet dari referensi formula + defined names |
| `profile_region(region)` | per kolom: tipe, null %, distinct, min/max, contoh |
| `find_text(file, query)` | lokasi sel yang cocok (label, istilah glossary) |
| `compare_sheets(a, b)` | kemiripan struktur + nilai (deteksi duplikat/versi seperti `REKAP BALANS (2)`) |
| `lookup_knowledge(term)` | arti istilah dari Knowledge |

Deteksi region: generalisasi `pinter-be-refactor/modules/parse_excel/parser.py`
(header bertingkat, merged vertikal, total row, multi-tabel satu sheet) +
sinyal style dari openpyxl (bold, fill, border) bila ambigu.

## 5. Node

Setiap node: config pydantic + `infer_schema(inputs) -> Schema` + `run(inputs) -> Relation`.
`infer_schema` memungkinkan `validate()` tanpa membaca data.

**Source**: `read_region` (region binding, lihat §6).

**Excel-spesifik**: `flatten_header` (multi-level -> `JANUARI|PKG`),
`fill_down` (label merged/hierarki), `parse_hierarchy` (`1.2.1` -> level, parent),
`unpivot` (lebar -> panjang), `unpivot_groups` (kolom berpasangan, mis. Phonska N [Ton, Formula]),
`drop_rows` (total, blank, noise), `split_header_token` (`JANUARI|PKG` -> bulan, anper).

**Umum (ala KNIME)**: `select`, `rename`, `cast`, `filter`, `dedupe`, `sort`,
`fill_missing`, `derive` (ekspresi SQL), `join`, `union`, `group_by`, `pivot`.

**Sink**: `write_output` (parquet/csv). `write_snowflake` fase berikut.

Ekspresi SQL `derive`/`filter`: diparse, hanya ekspresi skalar (tanpa subquery,
tanpa fungsi file/IO), duckdb dengan `enable_external_access=false`.

## 6. Recycle: pipeline yang bertahan saat struktur Excel berubah

Kunci: spec **tidak menyimpan alamat sel** (`B5:N40`), tapi **anchor semantik**.

```yaml
sources:
  produksi_npk_pkg:
    file_role: balansitas            # dicocokkan ke file baru via nama/pola
    sheet: {name_like: "(N) PKG", fallback_contains: ["TANGGAL", "Phonska"]}
    region:
      header_anchor: ["TANGGAL", "Phonska 1"]   # teks yang harus ada di header
      header_rows: 2
      end: until_blank_rows(2)
    expect_columns:                  # kontrak kolom
      - {match: "TANGGAL", type: date, required: true}
      - {match_regex: "^Phonska \\d+$", group: pabrik, required: true}
```

`rebind(spec, new_files)` menghasilkan `BindReport` per source:

| Status | Arti | Aksi |
|---|---|---|
| `exact` | anchor + kontrak cocok | run langsung, tanpa LLM |
| `shifted` | cocok tapi posisi berubah (baris/kolom bergeser) | run langsung, catat |
| `drift` | kontrak sebagian berubah (kolom baru/hilang/ganti nama, sheet ganti nama) | agent repair dengan diff; HITL sesuai policy |
| `missing` | source tidak ditemukan | agent cari kandidat (`find_text`, `compare_sheets`) + HITL |

Kolom baru yang cocok `group` (mis. `Phonska 6`) diterima otomatis karena
`unpivot_groups` bekerja per pola, bukan per daftar tetap.

Spec diberi versi; repair menghasilkan versi baru + diff, bukan menimpa.

## 7. HITL

Library tidak memblok. Dua bagian:

**`ask_human(kind, question, options, ref)`** -> mengembalikan `PendingQuestion`
(`clarify` | `confirm` | `choose`). Host yang menyampaikan ke user lalu memanggil
`answer(question_id, answer)`. Jawaban otomatis masuk `Knowledge.qa_log`.

**`HitlPolicy`** (config per job/tenant):

```yaml
checkpoints:
  after_workbook_map: off
  after_propose_datasets: required     # manusia memilih dataset
  after_spec: optional
  after_run: required_if_fail
  on_rebind_drift: required
auto_ask_when:
  confidence_below: 0.7
  unknown_term: true
```

`run()` menolak jalan bila checkpoint `required` belum di-approve.

## 8. Knowledge (konteks manusia)

```yaml
brief: "teks bebas"
glossary: {PKG: Petrokimia Gresik}
rules: ["Abaikan sheet Grafik*"]
target_schema: null        # opsional
examples: []               # spec lama sebagai contoh
qa_log: []                 # dari ask_human
```

Agent tidak menerima semua isi; pakai `lookup_knowledge`.

## 9. State dan persistensi

Semua state berbentuk satu objek pydantic, `model_dump_json()` / `model_validate_json()`:

```
JobState
  job_id, version
  files: [FileRef(id, filename, sha256)]   # bukan bytes
  knowledge: Knowledge
  spec: PipelineSpec + versions
  pending: [PendingQuestion]
  approvals: {checkpoint: who/when}
  last_run: RunReport
```

Inti library selalu fungsi murni: `fn(state, args) -> (result, new_state)`.
Mode persistensi dipilih saat membuat `Job`, bukan di inti:

| Mode | Cara pakai | Siapa menyimpan |
|---|---|---|
| `stateless` | `Job.from_state(json)` ... `job.state_json()` | host, bebas (session state ADK, DB, apa saja) |
| `store` | `Job.open(job_id, store=...)` | library memanggil `store.load/save` otomatis tiap mutasi |

```python
class StateStore(Protocol):
    def load(self, job_id: str) -> str | None: ...  # JSON
    def save(self, job_id: str, state_json: str, expected_version: int) -> None: ...
```

- Implementasi bawaan cuma satu: `FileStore(dir)` (JSON di disk, stdlib) untuk dev/test.
  ponytail: tanpa locking antar proses; production pakai store host.
- Store host (Postgres PINTER, Redis, dsb.) cukup mengimplementasi 2 method di atas.
- `expected_version` = optimistic lock: save gagal bila state sudah diubah pihak lain
  (dua tab user, dua worker), mencegah jawaban HITL tertimpa.

File tidak pernah masuk state. Host memberi `FileProvider`:
`open(file_id) -> path | bytes`. Cache parsing berbasis `sha256`, aman diulang
di mode mana pun.

## 10. Kualitas dan rekonsiliasi

- Assert per dataset: `not_null`, `unique(keys)`, `range`, `accepted_values`.
- Rekonsiliasi: total dari data bersih dibandingkan dengan baris/kolom TOTAL Excel
  atau sheet turunan (lihat `formula_lineage`), dengan toleransi.
- `RunReport`: baris in/out per node, baris dibuang + alasan, hasil assert,
  hasil rekonsiliasi.

### Sheet sumber vs turunan

- Peran sheet dari `formula_lineage`: sel nilai ketikan = sumber; sel formula yang
  merujuk sheet lain = turunan.
- Default: `propose_datasets` hanya mengusulkan dari **sumber**. Turunan otomatis
  dijadikan target rekonsiliasi (mis. SUM harian Januari == rekap Januari).
- Pengecualian: region turunan yang punya sel ketikan sendiri (prognosa, RKAP)
  tetap diusulkan, ditandai `mixed`. Manusia bisa memilih turunan apa pun secara
  eksplisit; label ini hanya default.

## 11. Aturan tool untuk LiteLLM

- JSON schema datar: primitif, enum, list primitif. Tanpa `anyOf`/`oneOf`/`$ref`.
  Struktur kompleks (spec) dikirim sebagai string YAML/JSON lalu divalidasi.
- Output tool <= ~6k token, paginasi, `truncated: true`.
- Teks saja; tanpa wajib vision.
- Error dikembalikan sebagai `{"error": "..."}` yang bisa ditindaklanjuti agent.
- Urutan dan ID deterministik.

## 12. Fase pengerjaan

1. `workbook.py` + `inspect/` (workbook_map, sheet_regions, render_range) + test di BALANSITAS.
2. `formula_lineage`, `profile_region`, `compare_sheets`, `find_text`.
3. Node Excel-spesifik + umum, `validate`, `run` duckdb.
4. Anchor binding + `rebind` + `BindReport`.
5. HITL policy, Knowledge, quality/rekonsiliasi.
6. Serah ke host: daftar fungsi `tools.py` + contoh pembungkus ADK.

## 13. Pertanyaan terbuka

- Satu file BALANSITAS menghasilkan banyak dataset: batas berapa dataset per job?
- Siapa pemilik `target_schema` di Snowflake nanti (agar nama/tipe kolom konsisten)?
- Retensi versi spec dan RunReport: berapa lama?
