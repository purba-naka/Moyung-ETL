# Moyung-ETL --- Technical Roadmap & Architecture

> **Tujuan dokumen:** Menjabarkan arah pengembangan Moyung-ETL menjadi
> library ETL yang dapat digunakan AI agent untuk memahami sumber data,
> merancang pipeline, mengeksekusi transformasi secara deterministik,
> dan memverifikasi hasilnya.
>
> **Prinsip utama:** LLM merencanakan dan memberi alasan; execution
> engine menjalankan komputasi secara deterministik. Moyung tetap
> ringan, dapat digunakan tanpa framework agent tertentu, dan tidak
> mengambil alih tanggung jawab host application.

------------------------------------------------------------------------

## 1. Executive Summary

Moyung-ETL sebaiknya berkembang sebagai **agent-oriented data
preparation and ETL library**, bukan sebagai platform data monolitik
yang mencoba menggantikan seluruh fungsi Dataiku, Airflow, dbt, atau
platform ML.

Arsitektur yang disarankan memisahkan lima tanggung jawab:

1.  **Data access:** membaca dan menginspeksi berbagai sumber data
    melalui adapter.
2.  **Data understanding:** schema inference, profiling, sampling, dan
    identifikasi struktur data.
3.  **Pipeline planning:** menyimpan spesifikasi pipeline deklaratif,
    membentuk DAG, dan memvalidasi dependensi.
4.  **Deterministic execution:** menjalankan transformasi menggunakan
    engine seperti DuckDB.
5.  **Verification and metadata:** memeriksa kualitas hasil, mencatat
    lineage, dan menyimpan metadata eksekusi.

AI agent menggunakan tool API Moyung untuk melakukan discovery,
profiling, membangun spesifikasi, menjalankan pipeline, dan
memverifikasi output. Agent tidak perlu menulis kode ETL bebas untuk
setiap pekerjaan.

### Keputusan cakupan

-   **Fokus awal:** Excel-first, dengan fondasi arsitektur yang
    memungkinkan multi-source.
-   **Target jangka menengah:** Excel, CSV, Parquet, JSON, dan database
    melalui adapter.
-   **Cakupan ML:** hanya **ML data preparation**. Moyung menyiapkan
    feature-ready dataset dan artefak pendukung, tetapi tidak melatih,
    mengevaluasi, menyimpan, atau men-deploy model.
-   **Execution engine awal:** DuckDB.
-   **Integrasi agent:** MCP dan Agent Skills sebagai lapisan integrasi
    opsional; core library tidak bergantung pada framework agent.

### Status implementasi (per 2026-11-20)

Dokumen ini ditulis sebelum kode ada, jadi sebagian nama di dalamnya
tidak dipakai. Yang berlaku di kode adalah nama di kolom kanan.

| Di dokumen ini | Di kode |
|---|---|
| package `moyung/`, class `Moyung` | package `excel_etl/`, class `JobTools` di `excel_etl/tools.py` |
| protocol `DataAsset` + adapter per sumber (§6, §16) | `Source` di `excel_etl/pipeline.py`; Excel di `workbook.py`, csv/parquet/json di `tablefile.py`. Tidak ada protocol terpisah |
| tool `create_spec`, `get_lineage` (§17) | `save_spec`/`edit_spec`; lineage dibaca dari `last_run` dan `verify_run` |

Status per fase §18. "sebagian" berarti inti jalan, sisanya ada di
daftar di bawah tabel.

| Fase | Status | Catatan |
|---|---|---|
| Phase 0 Stabilize Excel-first | selesai | spec tervalidasi, error terstruktur, `verify_run`, rebind file baru |
| Phase 1 Data understanding | selesai | profiling, strategi sampling (`sample.py`), `relate_tables`, `build_context` |
| Phase 2 Reliable pipeline engine | selesai | deteksi cycle di `pipeline.py`, severity `error`/`warn`, drift policy, run metadata. Check gagal memblokir hanya output yang terkait (status `partial`) |
| Phase 3 Multi-source foundation | sebagian | csv, parquet, json, jsonl jalan sebagai source; database adapter belum |
| Phase 4 ML data preparation | selesai | `ml.py`, `prep.py`, `leakage.py`, artefak preprocessing |

Yang belum, dan sengaja ditunda:

-   Database adapter: menunggu batas credential jelas.
-   Cache per node dari fingerprint (§9.5): `spec_sha256` dan sha256 file
    sudah dicatat untuk penelusuran, tetapi hasil node belum dipakai ulang.
-   Pada sumber non-Excel, `sample_data`, `profile_region`, dan
    `find_text` belum tersedia; `workbook_map` satu-satunya alat
    discovery. `excel_etl/auto.py` juga masih Excel saja.
-   Nama `excel-etl` akan janggal setelah multi-source matang. Ganti nama
    = perubahan nama paket publik, jadi butuh keputusan terpisah.

------------------------------------------------------------------------

## 2. Posisi Produk dan Batas Tanggung Jawab

### 2.1 Apa itu Moyung?

Moyung adalah library yang menyediakan kemampuan ETL terstruktur kepada
AI agent. Agent dapat memahami file dan data, menyusun pipeline dalam
format deklaratif, meminta klarifikasi manusia jika diperlukan, lalu
menjalankan dan memverifikasi pipeline tersebut.

Contoh alur:

``` text
Excel files
    |
    v
Inspect workbook and sheets
    |
    v
Profile candidate data regions
    |
    v
Agent proposes datasets and transformation plan
    |
    v
Human clarification / approval (when needed)
    |
    v
Validate pipeline specification
    |
    v
Execute deterministic pipeline
    |
    v
Run data quality checks
    |
    v
Parquet / CSV + metadata + reusable specification
```

### 2.2 Apa yang bukan tujuan Moyung?

Hindari memperluas scope menjadi semua hal berikut sekaligus:

-   Platform orchestration dan scheduler enterprise.
-   Data warehouse atau storage engine sendiri.
-   Pengganti penuh Dataiku, Airflow, dbt, atau Spark.
-   General-purpose agent framework.
-   Platform model training, experiment tracking, model registry, atau
    model serving.
-   Sistem yang membiarkan LLM menjalankan SQL atau Python arbitrer
    tanpa validasi dan kontrol.

Moyung boleh menyediakan integrasi dengan sistem-sistem tersebut, tetapi
tidak harus mengimplementasikan ulang fungsinya.

### 2.3 Pembagian tanggung jawab

  -----------------------------------------------------------------------
  Komponen                            Tanggung jawab
  ----------------------------------- -----------------------------------
  Agent / LLM                         Memahami tujuan pengguna,
                                      menafsirkan temuan data,
                                      mengusulkan transformasi, meminta
                                      klarifikasi

  Moyung core                         Discovery, profiling, sampling,
                                      spec validation, DAG, execution,
                                      quality checks, lineage

  Execution engine                    Menjalankan transformasi secara
                                      deterministik

  Host application                    File access, credentials, storage,
                                      secrets, approvals, scheduling, dan
                                      integrasi eksternal

  Human                               Menyetujui keputusan bisnis yang
                                      ambigu atau berisiko
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 3. Prinsip Desain

1.  **Deterministic execution.** Transformasi yang sama dengan input dan
    konfigurasi yang sama seharusnya menghasilkan output yang sama,
    selama engine dan dependensinya kompatibel.
2.  **Declarative pipeline.** Agent menghasilkan pipeline specification,
    bukan script ETL bebas.
3.  **Agent-framework agnostic.** Core tidak bergantung pada LangChain,
    LangGraph, CrewAI, OpenAI Agents SDK, atau framework tertentu.
4.  **Human-in-the-loop.** Keputusan yang tidak dapat disimpulkan dengan
    aman harus diklarifikasi, bukan ditebak.
5.  **Schema-aware.** Pipeline harus mengetahui struktur data yang
    diharapkan dan mendeteksi perubahan.
6.  **Observable.** Setiap node dan run memiliki status, durasi,
    input/output, error, serta metadata yang dapat diperiksa.
7.  **Portable.** Pengguna dapat menjalankan library secara lokal tanpa
    server wajib.
8.  **Explicit over implicit.** Perubahan schema, join, deduplication,
    dan aturan bisnis harus terlihat di spec.
9.  **Safe by default.** Validasi spec, pembatasan resource, dan kontrol
    akses dilakukan sebelum eksekusi.
10. **Incremental complexity.** Tambahkan abstraksi ketika ada kebutuhan
    nyata, bukan sekadar agar arsitektur terlihat enterprise.

------------------------------------------------------------------------

## 4. Rekomendasi Fitur dan Prioritas

### P0 --- Fondasi utama

  ---------------------------------------------------------------------------------
  Fitur                   Tujuan                  Hasil yang diharapkan
  ----------------------- ----------------------- ---------------------------------
  DataAsset abstraction   Menyamakan antarmuka    Excel/CSV/Parquet/JSON/database
                          sumber data             dapat diinspeksi dengan pola API
                                                  seragam

  Data profiling engine   Membantu agent memahami Profil kolom, missing values,
                          karakter data           distribusi, duplikasi, kandidat
                                                  key

  Adaptive sampling       Memberi agent sampel    Strategi sampling dan iterasi
                          yang relevan tanpa      berbasis temuan
                          membaca semua data ke   
                          konteks                 

  Pipeline DAG            Mengelola dependensi    Validasi graph, urutan eksekusi,
                          transformasi secara     rebuild downstream
                          eksplisit               

  Schema management       Menjaga kontrak         Schema contract, drift detection,
                          struktur data           evolution policy

  Data quality engine     Memastikan output       PASS / WARNING / FAIL dan
                          memenuhi aturan         kebijakan blocking

  Execution metadata      Membuat run dapat       Run ID, node status, input/output
                          ditelusuri              metadata, error
  ---------------------------------------------------------------------------------

### P1 --- Penguatan pengalaman agent dan operasional

  -----------------------------------------------------------------------
  Fitur                               Tujuan
  ----------------------------------- -----------------------------------
  Context builder                     Mengirim konteks yang relevan
                                      kepada agent berdasarkan task dan
                                      token budget

  Incremental execution/cache         Menghindari pengulangan komputasi
                                      yang tidak perlu

  Business knowledge/context          Menyimpan definisi kolom, istilah,
                                      mapping, dan aturan bisnis yang
                                      telah dikonfirmasi

  Reusable pipeline templates         Menggunakan kembali spec untuk file
                                      atau periode berikutnya

  Multi-source connectors             Memperluas sumber data setelah
                                      kontrak DataAsset stabil

  ML data preparation                 Menghasilkan dataset siap untuk
                                      workflow ML eksternal
  -----------------------------------------------------------------------

### P2 --- Ekspansi setelah kebutuhan terbukti

-   Connector tambahan dan integrasi sink yang lebih luas.
-   Scheduling dan monitoring melalui integrasi host/orchestrator.
-   Distributed execution jika ukuran data dan kebutuhan pengguna memang
    menuntutnya.
-   UI untuk visualisasi DAG, profiling, dan run history.

**Catatan:** P0/P1/P2 adalah prioritas rekomendasi, bukan komitmen bahwa
semua fitur harus dibuat sebelum rilis awal.

------------------------------------------------------------------------

## 5. Arsitektur Target

``` text
                 HOST APPLICATION
     (file access, credentials, storage, approval)
                         |
                         v
              AGENT INTERFACE LAYER
              MCP / Skills / SDK / API
                         |
                         v
                 MOYUNG CORE API
       +-----------------+------------------+
       |                 |                  |
       v                 v                  v
  Discovery         Pipeline Planner    Run & Verify
       |                 |                  |
       v                 v                  v
  Data Layer       Spec + DAG Engine    Execution Layer
  - Connectors     - Spec validation    - DuckDB
  - DataAsset      - Dependency graph   - Future engines
  - Schema         - Schema propagation
  - Profiler       - Plan validation
  - Sampler
       |                 |                  |
       +-----------------+------------------+
                         |
                         v
             QUALITY & LINEAGE LAYER
       - Quality results
       - Run metadata
       - Node lineage
       - Input/output fingerprints
                         |
                         v
                  OUTPUT ARTIFACTS
       Parquet / CSV / metadata / reusable specs
```

### 5.1 Pisahkan agent interface dari core

MCP hanyalah salah satu cara agent memanggil Moyung. Core API harus
tetap dapat dipakai langsung dari Python:

``` python
from moyung import Moyung

moyung = Moyung(...)
result = moyung.inspect(...)
```

Adapter MCP menerjemahkan tool call ke core API. Jangan menaruh business
logic ETL di handler MCP.

### 5.2 Pisahkan planning dari execution

Agent boleh mengusulkan pipeline, tetapi eksekusi harus melalui spec
yang tervalidasi:

``` text
User request
   -> Agent reasoning
   -> Pipeline spec
   -> Schema / DAG / policy validation
   -> Execution plan
   -> Deterministic execution
   -> Quality verification
```

Jangan menjadikan output kode Python atau SQL bebas dari LLM sebagai
satu-satunya sumber kebenaran pipeline.

------------------------------------------------------------------------

## 6. DataAsset Abstraction

> **Tidak diikuti.** Multi-source dikerjakan tanpa protocol ini: `Source`
> di `excel_etl/pipeline.py` menyatakan sumber, pembacanya `workbook.py`
> (Excel) dan `tablefile.py` (csv, parquet, json, jsonl). Bagian ini
> disimpan sebagai alasan di balik pilihan itu, bukan sebagai rencana.

### 6.1 Mengapa diperlukan?

Saat ini fokus Moyung adalah workbook Excel. Agar sumber data lain dapat
ditambahkan tanpa mengubah seluruh pipeline engine, definisikan kontrak
umum untuk sebuah data asset.

DataAsset adalah representasi logis dari sumber data yang dapat
diinspeksi dan dibaca. Ia tidak harus berarti data sudah disalin ke
storage internal.

### 6.2 Kontrak konseptual

``` python
from typing import Protocol


class DataAsset(Protocol):
    def inspect(self) -> "AssetMetadata": ...
    def get_schema(self) -> "Schema": ...
    def profile(self, request: "ProfileRequest") -> "DataProfile": ...
    def sample(self, request: "SampleRequest") -> "DataSample": ...
    def read(self, request: "ReadRequest"): ...
```

Kontrak final perlu mempertimbangkan bahwa kemampuan tiap sumber
berbeda. Contohnya, Excel memiliki workbook, sheet, cell range, formula,
dan merged cells; database memiliki table, view, query, dan kemungkinan
predicate pushdown.

Jangan memaksa semua sumber berpura-pura memiliki konsep sheet atau cell
range.

### 6.3 Adapter awal

Urutan implementasi yang disarankan:

1.  Excel --- pertahankan kemampuan khusus workbook dan region
    discovery.
2.  CSV --- sumber tabular sederhana.
3.  Parquet --- format output dan input analitik yang efisien.
4.  JSON / JSONL --- struktur semi-terstruktur.
5.  Database --- setelah abstraksi read, schema, sampling, dan
    credential boundary matang.

Contoh struktur:

``` text
moyung/
  assets/
    base.py
    excel.py
    csv.py
    parquet.py
    json.py
    database/
      base.py
      duckdb.py
      postgres.py
```

### 6.4 Metadata asset

Metadata umum dapat mencakup:

-   `asset_id`
-   `source_type`
-   `location` atau logical identifier
-   `display_name`
-   `schema`
-   `created_at` / `modified_at` jika tersedia
-   ukuran atau estimasi jumlah baris jika tersedia
-   kemampuan sumber (`capabilities`)
-   metadata spesifik adapter

Jangan menyimpan credential database di pipeline spec. Gunakan reference
ke credential yang dikelola host.

------------------------------------------------------------------------

## 7. Data Profiling Engine

### 7.1 Tujuan

Profiling menjawab pertanyaan: "Data ini seperti apa?" sebelum agent
memutuskan transformasi.

Profiling bukan tempat untuk membuat keputusan bisnis otomatis. Temuan
harus disajikan sebagai bukti atau kandidat, bukan selalu sebagai
instruksi pembersihan.

### 7.2 Statistik yang disarankan

**Profil umum:**

-   Row count atau estimasi row count.
-   Column count.
-   Missing/null count dan percentage.
-   Duplicate row count.
-   Distinct count dan approximate distinct count jika sesuai.
-   Deteksi tipe data dan proporsi nilai yang gagal dikonversi.

**Numerik:**

-   Min, max, mean, median.
-   Standard deviation.
-   Quantiles / percentiles.
-   Kandidat outlier.

**Kategorikal / teks:**

-   Top values dan frekuensinya.
-   Distinct values.
-   Panjang string minimum, maksimum, dan rata-rata.
-   Pola format yang umum.

**Relasi:**

-   Kandidat primary key berdasarkan uniqueness dan nullability.
-   Kandidat foreign key berdasarkan overlap nilai.
-   Kandidat join key.
-   Duplikasi pada kombinasi kolom.

### 7.3 Contoh hasil profiling

``` json
{
  "asset": "sales_2026",
  "row_count": 125000,
  "columns": {
    "customer_id": {
      "inferred_type": "string",
      "null_percentage": 0.0,
      "distinct_percentage": 100.0,
      "candidate_role": ["primary_key_candidate"]
    },
    "revenue": {
      "inferred_type": "decimal",
      "null_percentage": 1.2,
      "min": -250000,
      "max": 95000000,
      "candidate_role": ["measure"]
    }
  }
}
```

Angka di atas hanya contoh ilustratif.

### 7.4 Jangan menyamakan anomali statistik dengan kesalahan

Contoh: nilai revenue negatif dapat berarti refund, reversal, atau
koreksi transaksi. Moyung boleh menandainya sebagai temuan, tetapi
jangan otomatis menghapusnya tanpa aturan bisnis yang disetujui.

Simpan hasil profiling sebagai metadata yang dapat diakses ulang, dengan
informasi kapan dan berdasarkan sampel atau keseluruhan data mana profil
dihitung.

------------------------------------------------------------------------

## 8. Adaptive Sampling dan Context Builder

### 8.1 Masalah yang diselesaikan

Mengirim seluruh isi workbook atau dataset ke LLM tidak efisien, mahal,
dan sering tidak diperlukan. Agent biasanya membutuhkan konteks berbeda
pada setiap tahap.

### 8.2 Strategi sampling

Dukung strategi berikut sesuai kemampuan sumber:

-   First N rows.
-   Last N rows.
-   Random N rows.
-   Random fraction.
-   Stratified sampling berdasarkan kolom.
-   Column-value subset.
-   Head + tail.
-   Targeted sample berdasarkan filter atau kondisi.
-   Sample untuk nilai null, duplikasi, atau kandidat anomali.

Untuk sumber yang tidak mendukung strategi tertentu, adapter dapat
menggunakan fallback yang eksplisit.

### 8.3 Iterative sampling loop

``` text
1. Agent meminta inspeksi awal
2. Moyung mengembalikan metadata, schema, dan sample kecil
3. Agent mengidentifikasi pertanyaan yang belum terjawab
4. Agent meminta targeted sample atau profiling tambahan
5. Moyung mengembalikan bukti tambahan
6. Agent menyusun pipeline spec
```

### 8.4 Context builder

Buat komponen yang menyusun konteks berdasarkan jenis pekerjaan:

  -----------------------------------------------------------------------
  Task                                Konteks yang relevan
  ----------------------------------- -----------------------------------
  Discovery                           Asset metadata, sheet/region,
                                      schema kandidat

  Cleaning                            Profil missing values, tipe,
                                      duplikasi, contoh nilai

  Join                                Kandidat key, uniqueness, overlap,
                                      cardinality

  Aggregation                         Tipe kolom, kategori, grain data

  Validation                          Schema contract, quality rules,
                                      hasil run sebelumnya

  ML preparation                      Target/feature candidates,
                                      distribusi target, missingness,
                                      leakage warnings
  -----------------------------------------------------------------------

Context builder sebaiknya memiliki:

-   Token atau ukuran konteks maksimum.
-   Prioritas informasi.
-   Ringkasan profil.
-   Sampel yang representatif dan dapat ditelusuri.
-   Referensi ke data lengkap, bukan menyalin semua data ke prompt.
-   Pemisahan fakta hasil inspeksi dari interpretasi agent.

------------------------------------------------------------------------

## 9. Pipeline Specification dan DAG

### 9.1 Pipeline spec tetap menjadi sumber kebenaran

Pipeline harus direpresentasikan dalam format deklaratif yang
tervalidasi. Agent mengubah spec melalui tool API; execution engine
tidak mengeksekusi instruksi yang belum tervalidasi.

### 9.2 Contoh spec konseptual

``` json
{
  "pipeline_id": "monthly_sales",
  "version": 1,
  "sources": [
    {
      "id": "sales_source",
      "asset": "sales_workbook",
      "sheet": "Sales",
      "region": "A5:H"
    }
  ],
  "steps": [
    {
      "id": "clean_sales",
      "type": "select",
      "input": "sales_source",
      "columns": ["date", "customer_id", "product_id", "revenue"]
    },
    {
      "id": "sales_by_customer",
      "type": "group_by",
      "input": "clean_sales",
      "keys": ["customer_id"],
      "aggregations": {
        "total_revenue": {"column": "revenue", "function": "sum"}
      }
    }
  ],
  "outputs": [
    {
      "id": "customer_sales",
      "input": "sales_by_customer",
      "format": "parquet"
    }
  ],
  "checks": [
    {
      "type": "not_null",
      "dataset": "customer_sales",
      "columns": ["customer_id"]
    }
  ]
}
```

Ini adalah contoh bentuk konseptual; schema final perlu mengikuti model
Pydantic dan kontrak node yang benar-benar didukung.

### 9.3 Mengapa DAG?

Representasi DAG membuat dependency antar-transformasi eksplisit. Moyung
dapat:

-   Memastikan input node tersedia.
-   Mendeteksi dependency yang tidak valid.
-   Mendeteksi cycle.
-   Menentukan urutan eksekusi.
-   Menjalankan node independen secara paralel di masa depan.
-   Mengidentifikasi node downstream yang terdampak perubahan.
-   Mengulang hanya bagian pipeline yang perlu dibangun ulang.

### 9.4 Metadata per node

Minimal simpan:

-   Node ID dan jenis transformasi.
-   Input node dan output node.
-   Spec/config yang digunakan.
-   Status eksekusi.
-   Waktu mulai, selesai, dan durasi.
-   Row count input/output jika tersedia.
-   Schema input/output.
-   Error terstruktur.
-   Fingerprint input dan konfigurasi.

### 9.5 Fingerprint dan cache

Gunakan canonical serialization dari spec/config, lalu hash kriptografis
seperti SHA-256. Jangan menggunakan Python `hash()` sebagai identitas
persisten karena nilainya tidak dijamin stabil lintas proses.

Cache hanya aman jika fingerprint mencakup semua faktor yang memengaruhi
hasil, misalnya:

-   Identitas dan versi input.
-   Transformasi dan parameter.
-   Versi engine atau kompatibilitas runtime.
-   Schema dan konfigurasi relevan.

Jika sumber tidak menyediakan versi atau fingerprint yang andal,
nyatakan keterbatasan cache dan pilih re-execution daripada menganggap
input tidak berubah.

------------------------------------------------------------------------

## 10. Schema Management

### 10.1 Mengapa penting?

Pipeline bulanan dapat gagal bukan karena transformasi salah, tetapi
karena bentuk file berubah: nama kolom berganti, kolom hilang, tipe data
berubah, atau kolom baru ditambahkan.

Moyung harus membedakan schema yang terdeteksi dari schema yang
diharapkan.

### 10.2 Schema contract

Schema contract minimal mencakup:

-   Nama kolom.
-   Tipe data yang diharapkan.
-   Nullable atau non-nullable.
-   Urutan kolom jika memang penting.
-   Deskripsi dan metadata opsional.
-   Aturan coercion yang diizinkan.
-   Versi schema.

### 10.3 Schema drift policy

  -----------------------------------------------------------------------
  Perubahan                           Default policy yang disarankan
  ----------------------------------- -----------------------------------
  Kolom baru                          WARNING; jangan otomatis
                                      memasukkannya ke output

  Kolom wajib hilang                  FAIL

  Tipe berubah tetapi aman dikonversi WARNING atau perlu approval sesuai
                                      policy

  Tipe berubah dan konversi lossy     FAIL

  Kolom berubah nama                  FAIL atau minta mapping eksplisit

  Null muncul pada kolom non-nullable FAIL quality check
  -----------------------------------------------------------------------

Policy harus dapat dikonfigurasi. Jangan menganggap aturan ini cocok
untuk semua use case.

### 10.4 Schema propagation

Setiap node transformasi perlu menghasilkan schema output yang dapat
diprediksi. Contohnya:

-   `select`: hanya mempertahankan kolom terpilih.
-   `rename`: mengubah nama kolom.
-   `cast`: mengubah tipe dengan aturan konversi eksplisit.
-   `join`: menggabungkan schema kedua input dan menangani konflik nama.
-   `group_by`: menghasilkan key dan kolom agregasi.
-   `unpivot`: mengubah struktur wide menjadi long.

Validasi schema dilakukan sebelum eksekusi jika memungkinkan, lalu
diverifikasi kembali terhadap hasil aktual.

------------------------------------------------------------------------

## 11. Data Quality Rules Engine

### 11.1 Tingkatkan checks yang sudah ada

Moyung sudah memiliki arah checks seperti `not_null`, `unique`, `range`,
`accepted_values`, dan `reconcile`. Kembangkan menjadi rules engine
dengan severity dan kebijakan eksekusi.

### 11.2 Kategori checks

**Column-level:**

-   `not_null`
-   `unique`
-   `range`
-   `accepted_values`
-   `regex`
-   `type`
-   `min_length` / `max_length`

**Dataset-level:**

-   `row_count`
-   `duplicate_rows`
-   `freshness` jika metadata waktu tersedia
-   `schema_match`
-   `completeness`

**Relational:**

-   `foreign_key`
-   `referential_integrity`
-   `reconcile`
-   `uniqueness_combination`

### 11.3 Hasil check

Setiap check sebaiknya mengembalikan:

``` json
{
  "rule_id": "customer_id_not_null",
  "status": "FAIL",
  "severity": "error",
  "observed": {
    "null_count": 15
  },
  "expected": {
    "null_count": 0
  },
  "message": "customer_id contains null values"
}
```

Status hasil keseluruhan:

-   **PASS:** semua aturan wajib terpenuhi.
-   **WARNING:** ada temuan yang tidak memblokir pipeline.
-   **FAIL:** ada pelanggaran yang memblokir output atau downstream
    sesuai policy.

Sediakan `block_downstream` atau kebijakan setara agar pengguna dapat
menentukan apakah suatu kegagalan harus menghentikan pipeline.

### 11.4 Jangan bergantung pada satu quality score

Satu angka agregat dapat menyembunyikan pelanggaran kritis. Tampilkan
hasil per aturan, severity, jumlah baris terdampak, dan keputusan
blocking.

------------------------------------------------------------------------

## 12. Execution Engine

### 12.1 DuckDB sebagai engine awal

DuckDB merupakan pilihan masuk akal untuk tahap awal karena cocok untuk
analytical workloads lokal dan dapat bekerja dengan format seperti
Parquet serta data tabular. Pertahankan interface execution agar engine
lain dapat ditambahkan bila dibutuhkan.

### 12.2 Kontrak execution

Secara konseptual:

``` python
class ExecutionEngine(Protocol):
    def validate(self, plan: "ExecutionPlan") -> "ValidationResult": ...
    def execute(self, plan: "ExecutionPlan") -> "RunResult": ...
```

Jangan menambahkan abstraction engine yang terlalu kompleks sebelum ada
engine kedua yang benar-benar dibutuhkan. Abstraksi awal cukup untuk
memisahkan planning dan eksekusi.

### 12.3 Guardrails

-   Batasi ukuran file dan resource sesuai konfigurasi.
-   Validasi operator dan parameter sebelum membuat query.
-   Hindari interpolasi identifier/value yang tidak aman.
-   Jangan menerima SQL arbitrer dari agent tanpa policy dan validasi.
-   Gunakan temporary workspace yang terisolasi.
-   Catat error per node.
-   Hindari menimpa output yang sudah ada tanpa kebijakan eksplisit.
-   Pastikan operasi file dibatasi pada path yang diizinkan host.

------------------------------------------------------------------------

## 13. ML Data Preparation --- Scope yang Disetujui

> **Batas tegas:** Moyung hanya menyiapkan dataset untuk ML. Training,
> hyperparameter tuning, model evaluation, model registry, experiment
> tracking, model serving, dan deployment berada di luar scope.

### 13.1 Tujuan

Membantu agent menghasilkan dataset yang lebih siap digunakan oleh
workflow ML eksternal, dengan transformasi yang eksplisit dan dapat
direproduksi.

Moyung tidak perlu mengetahui algoritma model yang akan digunakan. Ia
perlu membantu memastikan dataset memiliki struktur, tipe, nilai, dan
pemisahan yang sesuai dengan tujuan persiapan data.

### 13.2 Fitur yang termasuk scope

#### A. Target dan feature declaration

-   Memungkinkan pengguna atau agent menyatakan target column.
-   Mendeklarasikan feature columns.
-   Menandai identifier, timestamp, label, dan kolom yang harus
    dikecualikan.
-   Memberi kandidat target/feature berdasarkan profiling, tetapi
    meminta konfirmasi ketika ambigu.

Jangan otomatis menyimpulkan target hanya dari nama kolom.

#### B. Missing-value preparation

-   Mengidentifikasi missingness per kolom.
-   Mendukung strategi eksplisit seperti drop, constant fill, mean,
    median, atau mode jika sesuai tipe data.
-   Menyimpan nilai/parameter imputasi yang dipelajari dari data
    training saja jika pipeline membuat split.
-   Tidak menganggap satu strategi cocok untuk semua kolom.

#### C. Categorical encoding

-   One-hot encoding atau mapping kategori sesuai kebutuhan.
-   Menentukan kebijakan kategori yang tidak dikenal pada data baru.
-   Menyimpan mapping kategori sebagai artefak preprocessing.
-   Menghindari encoding berbasis seluruh dataset sebelum split jika hal
    tersebut dapat menyebabkan leakage.

#### D. Numerical transformation

-   Casting tipe numerik.
-   Scaling/normalization jika diminta.
-   Transformasi log atau transformasi numerik lain yang eksplisit.
-   Parameter transformasi harus disimpan agar dapat diterapkan
    konsisten pada data baru.

#### E. Dataset splitting

-   Train/validation/test split atau train/test split sesuai kebutuhan.
-   Random split dengan seed yang tercatat.
-   Stratified split untuk klasifikasi jika sesuai.
-   Time-based split untuk data temporal.
-   Group-aware split jika baris dari entitas yang sama tidak boleh
    tersebar lintas subset.

Moyung hanya membuat pembagian dataset; tidak melatih atau mengevaluasi
model.

#### F. Leakage checks

Bantu mengidentifikasi risiko seperti:

-   Kolom yang secara langsung membocorkan target.
-   Fitur yang secara temporal terjadi setelah target.
-   Identifier yang berpotensi menjadi proxy.
-   Duplikasi atau entitas yang tersebar ke beberapa split.
-   Preprocessing yang menggunakan statistik dari seluruh data sebelum
    split.

Leakage detection bersifat pemeriksaan dan peringatan, bukan jaminan
bahwa dataset bebas leakage. Validasi domain tetap dibutuhkan.

#### G. Output dataset siap pakai

Hasil dapat berupa:

-   `train.parquet`
-   `validation.parquet` (opsional)
-   `test.parquet`
-   `preprocessing_metadata.json`
-   `feature_schema.json`
-   `preparation_report.json`

Format output dan nama file dapat dikonfigurasi.

### 13.3 Contoh deklarasi ML preparation

``` json
{
  "task": "prepare_ml_dataset",
  "target": "churn",
  "features": [
    "age",
    "monthly_spend",
    "tenure_months",
    "customer_segment"
  ],
  "exclude": [
    "customer_name",
    "internal_note"
  ],
  "preprocessing": {
    "missing_values": {
      "age": {"strategy": "median"},
      "customer_segment": {"strategy": "most_frequent"}
    },
    "categorical": {
      "customer_segment": {
        "strategy": "one_hot",
        "handle_unknown": "ignore"
      }
    },
    "numerical": {
      "monthly_spend": {"strategy": "standard_scale"}
    }
  },
  "split": {
    "strategy": "stratified",
    "target": "churn",
    "test_size": 0.2,
    "random_seed": 42
  }
}
```

Contoh ini bersifat ilustratif. Schema final harus mengatur validasi
tipe, strategi yang didukung, dan parameter yang wajib.

### 13.4 Aturan penting untuk mencegah leakage

Jika preprocessing mempelajari parameter dari data, seperti median
imputation, vocabulary kategori, mean/std scaling, atau feature
selection berbasis statistik, parameter tersebut harus **fit hanya pada
training partition**, lalu diterapkan ke validation/test.

Alur yang aman:

``` text
Raw dataset
    |
    v
Declare target / features / exclusions
    |
    v
Split dataset
    |
    +-------------------+--------------------+
    |                   |                    |
    v                   v                    v
  Train              Validation             Test
    |
    v
Fit preprocessing parameters
    |
    +-------------------+--------------------+
                        |
                        v
          Apply same parameters to all splits
                        |
                        v
             Prepared datasets + metadata
```

Untuk time-based atau group-aware split, urutan dan aturan pembagian
harus mempertahankan batas temporal atau kelompok yang diminta.

### 13.5 Di luar scope ML

Jangan implementasikan hal-hal berikut dalam roadmap ini:

-   Training model.
-   Pemilihan algoritma ML.
-   Hyperparameter tuning.
-   Cross-validation untuk evaluasi model.
-   Model metrics seperti accuracy, F1, RMSE, AUC.
-   Experiment tracking / MLflow integration untuk training.
-   Model registry.
-   Model serving atau deployment.
-   Automated model selection.

Integrasi dengan tool ML eksternal dapat dilakukan di masa depan melalui
output dataset dan metadata yang dihasilkan Moyung, tanpa menjadikan
fitur tersebut bagian dari core.

------------------------------------------------------------------------

## 14. Reusable Pipelines dan Perubahan File Bulanan

Salah satu use case bernilai tinggi Moyung adalah menjalankan ulang
pipeline yang sudah disetujui pada file periode berikutnya.

Contoh:

``` text
January.xlsx
   |
   v
Agent discovers structure
   |
   v
Pipeline spec approved and saved
   |
   v
February.xlsx arrives
   |
   v
Rebind source
   |
   v
Validate workbook / region / schema
   |
   +---- Compatible ----> Run saved pipeline
   |
   +---- Drift detected -> Ask for clarification / update spec
```

Hal yang perlu diperhatikan:

-   Rebinding sumber tidak boleh hanya berdasarkan nama file.
-   Validasi sheet, region, header, dan schema.
-   Deteksi perubahan struktur sebelum menjalankan transformasi.
-   Simpan versi spec dan histori perubahan.
-   Catat apakah pipeline dijalankan dengan spec yang sama atau versi
    yang diperbarui.
-   Minta persetujuan ketika mapping sumber berubah secara material.

------------------------------------------------------------------------

## 15. Human-in-the-loop dan Business Knowledge

Tidak semua keputusan pembersihan dapat disimpulkan dari statistik.
Misalnya, apakah nilai negatif valid, apakah dua kode berarti entitas
sama, atau apakah kolom tertentu boleh dihapus.

Moyung sebaiknya:

1.  Menyajikan temuan yang relevan.
2.  Menjelaskan konsekuensi dari pilihan transformasi.
3.  Mengajukan pertanyaan yang spesifik.
4.  Menyimpan jawaban yang telah dikonfirmasi sebagai business
    knowledge.
5.  Menggunakan knowledge tersebut pada pipeline berikutnya, dengan
    provenance dan versi.

Contoh business knowledge:

``` json
{
  "key": "sales.revenue.negative_values",
  "value": "Negative values represent refunds and must be retained.",
  "source": "human_confirmed",
  "scope": "sales_pipeline",
  "version": 1
}
```

Jangan mengubah asumsi agent menjadi business rule permanen tanpa
konfirmasi.

------------------------------------------------------------------------

## 16. Usulan Struktur Package

> **Tidak diikuti.** Paket nyata bernama `excel_etl/` dan lebih datar
> daripada susunan di bawah ini: tanpa folder `assets/`, dan tanpa class
> `Moyung`. Permukaan publiknya `excel_etl/tools.py`.

Contoh struktur target yang modular, tetapi tetap sederhana:

``` text
moyung/
  __init__.py

  core/
    config.py
    context.py
    errors.py
    models.py

  assets/
    base.py
    registry.py
    excel.py
    csv.py
    parquet.py
    json.py
    database/

  discovery/
    workbook.py
    regions.py
    datasets.py

  profiling/
    profiler.py
    statistics.py
    relationships.py
    models.py

  sampling/
    strategies.py
    sampler.py

  schema/
    models.py
    inference.py
    contracts.py
    drift.py
    propagation.py

  pipeline/
    spec.py
    nodes.py
    catalog.py
    validator.py
    dag.py
    planner.py
    history.py

  execution/
    base.py
    duckdb.py
    runner.py
    workspace.py
    cache.py

  quality/
    rules.py
    engine.py
    results.py

  lineage/
    models.py
    tracker.py

  ml_prep/
    declaration.py
    splitting.py
    imputation.py
    encoding.py
    scaling.py
    leakage.py
    artifacts.py

  context_builder/
    builder.py
    policies.py

  integrations/
    mcp/
    skills/

  storage/
    interfaces.py
    local.py
```

Struktur ini adalah arah modularisasi, bukan instruksi untuk langsung
membuat semua file. Pertahankan struktur yang sudah ada jika pembagian
modulnya telah bekerja baik; lakukan refactor bertahap berdasarkan
tanggung jawab yang mulai membesar.

------------------------------------------------------------------------

## 17. Usulan Tool API untuk Agent

> **Nama usulan, bukan nama final.** Daftar tool yang benar-benar ada
> beserta kelompoknya ada di `README.md`; sumbernya `JobTools` di
> `excel_etl/tools.py`.

Pertahankan tool yang sudah ada bila fungsinya masih relevan.
Pengembangan berikut dapat memperjelas urutan kerja agent.

### Discovery

-   `list_assets`
-   `inspect_asset`
-   `workbook_map`
-   `sheet_regions`
-   `profile_region`
-   `sample_data`
-   `find_text`
-   `compare_sheets`
-   `propose_datasets`

### Planning

-   `node_catalog`
-   `create_spec`
-   `edit_spec`
-   `get_spec`
-   `validate_spec`
-   `preview_plan`
-   `spec_history`

### Execution

-   `run_pipeline`
-   `get_run`
-   `get_node_result`
-   `verify_run`
-   `rebind_sources`

### Quality and lineage

-   `get_quality_report`
-   `get_schema`
-   `get_schema_diff`
-   `get_lineage`
-   `get_run_history`

### ML preparation

-   `declare_ml_dataset`
-   `prepare_ml_dataset`
-   `get_preparation_report`

Jangan membuat satu tool raksasa yang melakukan discovery, planning,
execution, dan approval sekaligus. Tool yang lebih kecil memudahkan
agent memahami kemampuan, mengelola error, dan meminta persetujuan di
titik yang tepat.

------------------------------------------------------------------------

## 18. Roadmap Implementasi Bertahap

### Phase 0 --- Stabilize current Excel-first workflow

**Tujuan:** memastikan use case inti bekerja end-to-end sebelum menambah
banyak abstraksi.

Pekerjaan:

-   Audit tool dan node yang sudah tersedia.
-   Tetapkan schema spec yang tervalidasi.
-   Pastikan error terstruktur.
-   Perkuat validasi input/output.
-   Pastikan output dapat diverifikasi.
-   Tambahkan integration tests untuk workbook yang memiliki variasi
    struktur.
-   Dokumentasikan batasan Excel reader dan formula handling.

**Exit criteria:**

-   Agent dapat menemukan region, menyusun spec, menjalankan pipeline,
    dan memverifikasi output.
-   Pipeline yang sama dapat digunakan kembali pada file baru yang
    kompatibel.
-   Perubahan struktur terdeteksi sebelum menghasilkan output yang
    salah.

### Phase 1 --- Data understanding

**Tujuan:** meningkatkan kualitas keputusan agent.

Pekerjaan:

-   DataAsset interface yang tidak menghilangkan fitur khusus Excel.
-   Profiling kolom dan dataset.
-   Sampling strategies.
-   Candidate key dan relationship discovery.
-   Context builder sederhana.

**Exit criteria:**

-   Agent menerima schema, profil, dan sample yang cukup untuk menyusun
    transformasi.
-   Profiling dapat dilakukan tanpa mengirim seluruh dataset ke LLM.
-   Temuan profiling dapat ditelusuri ke asset dan waktu inspeksi.

### Phase 2 --- Reliable pipeline engine

**Tujuan:** membuat pipeline lebih aman, dapat diprediksi, dan mudah
di-debug.

Pekerjaan:

-   DAG validation.
-   Schema propagation.
-   Schema drift policy.
-   Quality rules engine.
-   Run metadata dan lineage.
-   Fingerprint/cache setelah invalidation rules jelas.

**Exit criteria:**

-   Dependency invalid dan cycle terdeteksi.
-   Perubahan schema menghasilkan warning/failure sesuai policy.
-   Setiap run dapat ditelusuri hingga input, spec, node, dan output.
-   Quality failure dapat memblokir downstream sesuai konfigurasi.

### Phase 3 --- Multi-source foundation

**Tujuan:** memperluas sumber tanpa merusak kontrak core.

Pekerjaan:

-   CSV adapter.
-   Parquet adapter.
-   JSON/JSONL adapter.
-   Uji konsistensi schema/profil/sampling antar adapter.
-   Database adapter hanya setelah kebutuhan dan credential boundary
    jelas.

**Exit criteria:**

-   Agent menggunakan pola discovery dan pipeline yang konsisten pada
    sumber yang didukung.
-   Perbedaan kemampuan sumber dinyatakan secara eksplisit.
-   Tidak ada credential yang tersimpan di spec.

### Phase 4 --- ML data preparation

**Tujuan:** menyiapkan dataset yang dapat dipakai oleh workflow ML
eksternal.

Pekerjaan:

-   Target/feature/exclusion declaration.
-   Missing-value preparation.
-   Categorical encoding.
-   Numerical scaling.
-   Random, stratified, time-based, dan group-aware splitting sesuai
    dukungan.
-   Leakage checks.
-   Preprocessing metadata dan output artifacts.

**Exit criteria:**

-   Dataset hasil preparation memiliki schema dan metadata yang jelas.
-   Parameter preprocessing dipelajari dari training partition saja.
-   Split dapat direproduksi melalui seed dan konfigurasi.
-   Tidak ada training atau evaluasi model di dalam Moyung.

------------------------------------------------------------------------

## 19. Testing Strategy

Testing perlu mencakup unit, integration, dan regression tests.

### Unit tests

-   Schema inference.
-   Profiling statistics.
-   Sampling strategies.
-   Node parameter validation.
-   DAG cycle detection.
-   Schema propagation.
-   Quality rules.
-   Fingerprint generation.
-   ML split dan preprocessing fit/apply behavior.

### Integration tests

-   Excel workbook dengan banyak sheet.
-   Header di baris yang berbeda.
-   Merged cells dan formula.
-   Data kosong atau region tidak ditemukan.
-   File bulanan dengan schema sama.
-   File bulanan dengan kolom baru/hilang/berubah tipe.
-   Join dengan duplicate keys dan cardinality berbeda.
-   Pipeline multi-step sampai output Parquet.

### Regression tests

Simpan fixture dan expected output untuk kasus-kasus yang pernah
menyebabkan bug. Ketika engine atau dependency diperbarui, jalankan
ulang regression suite.

### ML preparation tests

-   Seed yang sama menghasilkan split yang konsisten.
-   Stratified split mempertahankan distribusi target secara wajar.
-   Time split tidak mencampur masa depan ke training.
-   Group split tidak memecah group antar subset.
-   Imputer/scaler/encoder fit hanya pada training partition.
-   Unknown categories ditangani sesuai kebijakan.
-   Leakage warnings muncul untuk pola yang memang dapat dideteksi.

------------------------------------------------------------------------

## 20. Risiko dan Trade-offs

  -----------------------------------------------------------------------
  Risiko                  Dampak                  Mitigasi
  ----------------------- ----------------------- -----------------------
  Scope melebar terlalu   Core menjadi sulit      Excel-first; tambah
  cepat                   dipelihara              adapter berdasarkan use
                                                  case

  Abstraksi DataAsset     Fitur khusus Excel      Kontrak umum +
  terlalu generik         hilang                  capability khusus
                                                  adapter

  Agent membuat asumsi    Data salah dibersihkan  Human confirmation dan
  bisnis                                          business knowledge

  Sampling tidak          Agent mengambil         Profiling + sampling
  representatif           keputusan keliru        adaptif + metadata
                                                  strategi

  Cache salah menganggap  Output stale            Fingerprint input yang
  input sama                                      kuat; fallback re-run

  Schema drift tidak      Pipeline menghasilkan   Contract dan drift
  terdeteksi              output salah            validation

  SQL/transformasi tidak  Risiko akses atau       Validasi spec,
  aman                    resource                allowlist node,
                                                  workspace terisolasi

  Leakage pada ML         Dataset tidak valid     Split sebelum fit
  preparation             untuk eksperimen ML     preprocessing

  Terlalu banyak tool     Agent bingung memilih   Tool names dan
  untuk agent             tool                    deskripsi berorientasi
                                                  task
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 21. Rekomendasi Prioritas Paling Praktis

Jika sumber daya pengembangan terbatas, jangan mengimplementasikan
seluruh roadmap sekaligus.

Urutan yang paling masuk akal:

1.  **DataAsset + schema contract:** fondasi untuk membaca dan memahami
    sumber secara konsisten.
2.  **Profiling + sampling:** meningkatkan kualitas konteks dan
    keputusan agent.
3.  **DAG + schema propagation + quality rules:** membuat pipeline dapat
    diandalkan.
4.  **Run metadata + lineage:** membuat pipeline dapat di-debug dan
    diaudit.
5.  **CSV/Parquet/JSON adapters:** memperluas sumber setelah fondasi
    stabil.
6.  **ML data preparation:** tambahkan setelah pipeline dan schema
    contract matang.

### Definisi keberhasilan produk

Moyung berhasil bukan karena memiliki jumlah connector atau node paling
banyak. Nilai utamanya adalah:

-   Agent dapat memahami data yang bentuknya tidak rapi.
-   Agent dapat membuat pipeline yang dapat diperiksa manusia.
-   Pipeline berjalan secara deterministik dan dapat diulang.
-   Perubahan input terdeteksi sebelum merusak output.
-   Hasil memiliki quality report dan lineage yang jelas.
-   Spesifikasi pipeline dapat digunakan kembali.
-   Core tetap ringan dan dapat diintegrasikan dengan berbagai agent.

------------------------------------------------------------------------

## 22. Referensi Konsep

Konsep berikut dapat menjadi bahan pembelajaran dan pembanding desain:

-   **Dataiku DSS Flow:** pemodelan dataset dan recipe sebagai graph
    dependensi; berguna untuk memahami lineage dan rebuild downstream.
-   **Dataiku sampling and visual recipes:** referensi untuk strategi
    sampling dan transformasi visual.
-   **DuckDB:** execution engine analitik lokal.
-   **Pydantic:** validasi schema untuk pipeline specification dan tool
    inputs.
-   **Parquet:** format kolumnar untuk output tabular analitik.
-   **SpreadsheetLLM:** inspirasi untuk representasi dan kompresi
    konteks spreadsheet.
-   **dbt:** referensi konsep model dependency, tests, dan lineage;
    bukan berarti Moyung perlu meniru seluruh dbt.
-   **Data quality frameworks:** referensi untuk aturan, severity, dan
    hasil validasi.

Gunakan referensi tersebut untuk mengambil konsep yang relevan, bukan
menyalin seluruh kompleksitas platformnya.

------------------------------------------------------------------------

## 23. Keputusan Arsitektur yang Perlu Dipertahankan

1.  Moyung adalah library, bukan platform server wajib.
2.  Excel-first untuk kualitas use case awal, tetapi core dirancang agar
    multi-source dapat ditambahkan.
3.  LLM menyusun rencana; spec tervalidasi menjadi kontrak eksekusi.
4.  DuckDB menjadi engine awal; abstraction engine dijaga sederhana.
5.  Schema dan data quality merupakan bagian inti, bukan fitur tambahan.
6.  ML scope berhenti pada data preparation.
7.  Credentials, scheduling, dan akses sistem eksternal tetap menjadi
    tanggung jawab host.
8.  Jangan membangun fitur enterprise sebelum ada kebutuhan pengguna
    yang nyata.

**Arah akhir:** bangun Moyung sebagai *reliable data preparation engine
for AI agents* --- agent-friendly pada antarmuka, tetapi deterministic
dan schema-aware pada eksekusi.
