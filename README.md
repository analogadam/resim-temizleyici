# Resim Temizleyici

Klasördeki kopya resimleri içeriğe göre bulan (perceptual hash), taşıyan veya Geri Dönüşüm Kutusu'na gönderen; ayrıca klasör boyutlarını ağaç şeklinde gösteren Windows masaüstü uygulaması (tkinter).

## Çalıştırma

```
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python ResimTemizleyici.py
```

## Exe derleme

```
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller ResimTemizleyici.spec
```

Çıktı: `dist\ResimTemizleyici.exe`
