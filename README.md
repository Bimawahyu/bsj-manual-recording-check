# BSJ Manual Recording Check — GitHub Only

Single-page Manual Recording Check yang berjalan tanpa staging/VPS.

## Arsitektur

```
Browser / GitHub Pages
        |
        | workflow_dispatch (encrypted payload)
        v
GitHub Actions
        |
        +--> BSJ realtime ACC
        +--> wake ONLY jika ACC OFF
        +--> File List
        +--> Engine ON/OFF history
        +--> completeness + gap
        |
        v
GitHub Actions Artifact
        |
        v
Browser menampilkan result + timeline + Excel
```

GitHub Pages hanya menjadi UI. Python dijalankan oleh GitHub Actions runner.

## Wake rule

Realtime `/monitor/getRealTimeInfo` dibaca lebih dulu:

- `accState=0` = ACC ON -> **tidak wake**, langsung File List.
- `accState=1` = ACC OFF -> wake -> tunggu 60 detik -> File List.
- ACC unknown -> tidak wake, langsung mencoba File List.

## Repository secrets

Tambahkan di **Settings -> Secrets and variables -> Actions -> New repository secret**:

- `BSJ_API_KEY`
- `BSJ_WEB_USERNAME`
- `BSJ_WEB_PASSWORD_PAYLOAD`
- `MANUAL_GITHUB_TOKEN`

Jangan commit credential ke repository.

### MANUAL_GITHUB_TOKEN

Buat Fine-grained Personal Access Token khusus repo:

`Bimawahyu/bsj-manual-recording-check`

Permission minimal:

- Repository permission **Actions: Read and write**

Masukkan token sebagai repository secret `MANUAL_GITHUB_TOKEN`.

Dashboard utama tidak menampilkan field token. Untuk browser baru, buka `setup.html` satu kali, masukkan token yang sama, lalu token disimpan hanya di `localStorage` browser tersebut. Setelah itu halaman utama auto-connect.

Token sengaja **tidak di-hardcode** ke HTML/JavaScript karena repository dan GitHub Pages bersifat publik; hardcode akan membuat PAT dapat dibaca dari source/devtools.

IMEI + tanggal dienkripsi AES-GCM di browser menggunakan key turunan dari token sebelum dikirim sebagai workflow input. Workflow mendekripsinya menggunakan secret `MANUAL_GITHUB_TOKEN`.

## Live site

https://bimawahyu.github.io/bsj-manual-recording-check/

## Hasil

Setiap check menghasilkan artifact selama 7 hari:

- `result.json`
- `result.xlsx`
- `request_meta.json`

Web mengambil artifact melalui GitHub API dan menampilkan summary, detail, gap, Engine events, Raw File List, dan 24-hour recording timeline.
