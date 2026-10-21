# BSJ Manual Recording Check

Single-page remote dashboard untuk **Manual Recording Check**.

## Live site

Expected GitHub Pages URL:

`https://bimawahyu.github.io/bsj-manual-recording-check/`

## Cara kerja

GitHub Pages hanya menjadi frontend. Proses BSJ tetap dijalankan oleh FastAPI backend yang kamu publish via HTTPS.

```
Browser
  -> GitHub Pages
  -> HTTPS BSJ Remote API
  -> BSJ Official API + BSJ Platform
```

Credential BSJ **tidak** disimpan di repository ini.

## Backend yang dibutuhkan

Gunakan remote backend dari project BSJ Monitor dan jalankan endpoint manual-only, misalnya:

```bash
python -m uvicorn backend.app.remote_main:app --host 0.0.0.0 --port 8011
```

Environment backend:

```env
REMOTE_DASHBOARD_API_TOKEN=<long-random-token>
REMOTE_DASHBOARD_ALLOWED_ORIGINS=https://bimawahyu.github.io
```

Plus credential BSJ yang sudah digunakan aplikasi lokal.

## First deploy

Repository ini public, jadi GitHub Pages tersedia pada GitHub Free. Bila Pages belum aktif, buka:

**Settings -> Pages -> Build and deployment -> Source -> GitHub Actions**

Setelah Source dipilih, workflow `.github/workflows/pages.yml` akan deploy site.
