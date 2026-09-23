import os
import re
import sys
import io
import ctypes
import subprocess
import threading
import shutil
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk
from PIL import Image
import imagehash

# Windows konsolunda emoji / Türkçe karakter çıktısı için
if sys.platform.startswith("win"):
    if sys.stdout is not None:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

RESIM_UZANTILARI = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
# Windows'un kopya adlandırmaları: "x - Kopya.jpg", "x - Copy.jpg", "x (1).jpg"
KOPYA_ADI = re.compile(r"kopya|copy|\(\d+\)", re.IGNORECASE)


# --- DOSYA İŞLEMLERİ (arayüzden bağımsız) ---
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def geri_donusume_gonder(yol):
    """Dosyayı/klasörü kalıcı silmek yerine Geri Dönüşüm Kutusu'na gönderir."""
    FO_DELETE = 0x3
    # Sessiz + onaysız + geri alınabilir; çöpe gidemeyecekse (USB, ağ sürücüsü) Windows uyarır
    bayraklar = 0x4 | 0x10 | 0x40 | 0x400 | 0x4000
    islem = _SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=os.path.abspath(yol) + "\0", fFlags=bayraklar)
    sonuc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(islem))
    if sonuc != 0 or islem.fAnyOperationsAborted:
        raise OSError(f"Geri Dönüşüm Kutusu'na gönderilemedi (kod {sonuc})")


def benzersiz_yol(klasor, ad):
    """Hedefte aynı adlı dosya varsa üzerine yazmamak için 'ad (2).jpg' üretir."""
    kok, uzanti = os.path.splitext(ad)
    aday, sayac = os.path.join(klasor, ad), 2
    while os.path.exists(aday):
        aday = os.path.join(klasor, f"{kok} ({sayac}){uzanti}")
        sayac += 1
    return aday


def format_bytes(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0: return f"{size:.2f} {unit}"
        size /= 1024.0


def hata_ozeti(hatalar):
    ilk = "\n".join(f"• {os.path.basename(yol)}: {e}" for yol, e in hatalar[:10])
    kalan = f"\n... ve {len(hatalar) - 10} dosya daha" if len(hatalar) > 10 else ""
    return f"{len(hatalar)} dosyada işlem yapılamadı:\n\n{ilk}{kalan}"


class KopyaUygulamasi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gelişmiş Depo Yöneticisi & Dosya Gezgini")
        self.root.geometry("1300x850")

        self.iptal_kopya = False
        self.iptal_boyut = False

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.tab1 = tk.Frame(self.notebook)
        self.tab2 = tk.Frame(self.notebook)

        self.notebook.add(self.tab1, text="  📸 Kopya Resim Avcısı  ")
        self.notebook.add(self.tab2, text="  📊 Klasör Boyut Analizi  ")

        self.arayuz_kopya_bulucu()
        self.arayuz_boyut_analizoru()

    def arayuzde(self, fonksiyon, *args):
        """tkinter thread-safe değil; arka plan thread'leri arayüze bu yolla dokunur."""
        self.root.after(0, fonksiyon, *args)

    # --- SEKME 2: KLASÖR BOYUT ANALİZİ ---
    def arayuz_boyut_analizoru(self):
        ust = tk.Frame(self.tab2, pady=15, bg="#f8f9fa")
        ust.pack(fill="x")

        self.tara_btn2 = tk.Button(ust, text="📁 Ana Klasör Seç", command=self.boyut_analizi_thread, bg="#28a745", fg="white", font=("Arial", 10, "bold"), padx=20)
        self.tara_btn2.pack(side="left", padx=20)

        self.iptal_btn2 = tk.Button(ust, text="🛑 Durdur", command=lambda: self.durdur_sinyali("boyut"), bg="#dc3545", fg="white", state="disabled", padx=20)
        self.iptal_btn2.pack(side="left")

        # Sağ taraftaki işlem butonları
        tk.Button(ust, text="🗑️ Seçileni Sil", command=self.analiz_sil, bg="#f44336", fg="white", padx=15).pack(side="right", padx=10)
        tk.Button(ust, text="📂 Klasörü Göster", command=self.analiz_goster, bg="#6c757d", fg="white", padx=15).pack(side="right", padx=10)

        self.progress2 = ttk.Progressbar(self.tab2, orient="horizontal", mode="indeterminate")
        self.progress2.pack(fill="x", padx=20, pady=10)

        self.analiz_etiket = tk.Label(self.tab2, text="💡 Silinen öğeler Geri Dönüşüm Kutusu'na gider.", fg="gray")
        self.analiz_etiket.pack()

        self.tree2 = ttk.Treeview(self.tab2, columns=("Boyut", "Yuzde", "TamYol"), show="tree headings")
        self.tree2.heading("#0", text="Dosya / Klasör Yapısı", anchor="w")
        self.tree2.heading("Boyut", text="Boyut")
        self.tree2.heading("Yuzde", text="Doluluk")

        self.tree2.column("#0", width=600, anchor="w")
        self.tree2.column("Boyut", width=120, anchor="center")
        self.tree2.column("Yuzde", width=100, anchor="center")
        self.tree2["displaycolumns"] = ("Boyut", "Yuzde")

        sb = ttk.Scrollbar(self.tab2, orient="vertical", command=self.tree2.yview)
        self.tree2.configure(yscrollcommand=sb.set)
        self.tree2.pack(side="left", fill="both", expand=True, padx=(20, 0), pady=10)
        sb.pack(side="right", fill="y", padx=(0, 20), pady=10)

        self.tree2.bind("<<TreeviewOpen>>", self.klasor_genislet)

    def analiz_goster(self):
        item_id = self.tree2.focus()
        if not item_id: return
        yol = self.tree2.item(item_id)["values"][2]
        if os.path.exists(yol):
            subprocess.run(["explorer", "/select,", os.path.normpath(yol)])

    def analiz_sil(self):
        item_id = self.tree2.focus()
        if not item_id: return
        yol = self.tree2.item(item_id)["values"][2]
        ad = self.tree2.item(item_id)["text"]

        if messagebox.askyesno("Silme Onayı", f"'{ad.strip()}' (varsa altındakilerle birlikte) Geri Dönüşüm Kutusu'na gönderilsin mi?"):
            try:
                geri_donusume_gonder(yol)
                self.tree2.delete(item_id)
            except Exception as e:
                messagebox.showerror("Hata", f"Silme işlemi başarısız: {e}")

    def boyut_analizi_thread(self):
        secilen = filedialog.askdirectory()
        if not secilen: return
        self.tree2.delete(*self.tree2.get_children())
        self.iptal_boyut = False
        self.tara_btn2.config(state="disabled", text="⏳ Hesaplanıyor...")
        self.iptal_btn2.config(state="normal")
        self.progress2.start()
        threading.Thread(target=self.boyut_hesapla, args=(secilen, ""), daemon=True).start()

    def boyut_hesapla(self, yol, parent_id):
        """Arka plan thread'i: sadece hesaplar, sonucu arayüze bırakır."""
        icerik, ana_toplam = [], 0
        try:
            for item in os.listdir(yol):
                if self.iptal_boyut: break
                tam_yol = os.path.join(yol, item)
                boyut = self.klasor_boyutu_al(tam_yol)
                icerik.append({"ad": item, "boyut": boyut, "yol": tam_yol, "is_dir": os.path.isdir(tam_yol)})
                ana_toplam += boyut
        except Exception as e: print(f"Hata: {e}")
        self.arayuzde(self.boyut_sonucunu_goster, yol, parent_id, icerik, ana_toplam)

    def boyut_sonucunu_goster(self, yol, parent_id, icerik, ana_toplam):
        icerik.sort(key=lambda x: x["boyut"], reverse=True)
        for i in icerik:
            oran = (i["boyut"] / ana_toplam * 100) if ana_toplam > 0 else 0
            node = self.tree2.insert(parent_id, "end", text=f" {i['ad']}", values=(format_bytes(i["boyut"]), f"%{oran:.1f}", i["yol"]), open=False)
            if i["is_dir"]:
                self.tree2.insert(node, "end", text="yükleniyor...")

        self.analiz_etiket.config(text=f"✅ Klasör: {os.path.basename(yol)} | Toplam: {format_bytes(ana_toplam)}", fg="green")
        self.progress2.stop()
        self.tara_btn2.config(state="normal", text="📁 Ana Klasör Seç")
        self.iptal_btn2.config(state="disabled")

    def klasor_genislet(self, event):
        item_id = self.tree2.focus()
        cocuklar = self.tree2.get_children(item_id)
        if len(cocuklar) == 1 and self.tree2.item(cocuklar[0])["text"] == "yükleniyor...":
            self.tree2.delete(cocuklar[0])
            tam_yol = self.tree2.item(item_id)["values"][2]
            self.progress2.start()
            threading.Thread(target=self.boyut_hesapla, args=(tam_yol, item_id), daemon=True).start()

    def klasor_boyutu_al(self, yol):
        if os.path.isfile(yol): return os.path.getsize(yol)
        t = 0
        try:
            for k, kl, f in os.walk(yol):
                if self.iptal_boyut: break
                for file in f:
                    try: t += os.path.getsize(os.path.join(k, file))
                    except OSError: pass
        except OSError: pass
        return t

    def durdur_sinyali(self, hedef):
        if hedef == "kopya": self.iptal_kopya = True
        else: self.iptal_boyut = True

    # --- SEKME 1: KOPYA RESİM AVCISI ---
    # tree1 satırlarının iid'si dosyanın tam yoludur.
    def arayuz_kopya_bulucu(self):
        ust = tk.Frame(self.tab1, pady=10)
        ust.pack(fill="x")
        self.tara_btn1 = tk.Button(ust, text="🔍 Taramayı Başlat", command=self.taramayı_baslat_thread, bg="#007bff", fg="white", font=("Arial", 10, "bold"), padx=20)
        self.tara_btn1.pack(side="left", padx=20)
        self.iptal_btn1 = tk.Button(ust, text="🛑 Durdur", command=lambda: self.durdur_sinyali("kopya"), bg="#dc3545", fg="white", state="disabled", padx=20)
        self.iptal_btn1.pack(side="left")
        self.progress1 = ttk.Progressbar(self.tab1, orient="horizontal", mode="determinate")
        self.progress1.pack(fill="x", padx=20, pady=10)
        self.kopya_etiket = tk.Label(self.tab1, text="💡 Çift tıklayarak resmi açabilirsiniz. Silinmesini istemediğiniz kopyayı seçip 'Listeden Çıkar' deyin.", fg="gray")
        self.kopya_etiket.pack()

        liste = tk.Frame(self.tab1)
        liste.pack(fill="both", expand=True, padx=20)
        self.tree1 = ttk.Treeview(liste, columns=("Grup", "Durum", "Dosya", "Boyut", "Konum"), show="headings")
        for col in self.tree1["columns"]: self.tree1.heading(col, text=col)
        sb = ttk.Scrollbar(liste, orient="vertical", command=self.tree1.yview)
        self.tree1.configure(yscrollcommand=sb.set)
        self.tree1.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree1.tag_configure("asil", background="#d1e7dd"); self.tree1.tag_configure("kopya", background="#f8d7da")
        self.tree1.bind("<Double-1>", self.resmi_ac)

        alt = tk.Frame(self.tab1, pady=10)
        alt.pack(fill="x")
        tk.Button(alt, text="🚚 Kopyaları Taşı", command=self.tum_kopyalari_tasi, bg="#9C27B0", fg="white", padx=15).pack(side="right", padx=10)
        tk.Button(alt, text="🗑️ Kopyaları Sil", command=self.tum_kopyalari_sil, bg="#f44336", fg="white", padx=15).pack(side="right", padx=10)
        tk.Button(alt, text="➖ Seçileni Listeden Çıkar", command=self.listeden_cikar, bg="#6c757d", fg="white", padx=15).pack(side="left", padx=20)

    def resmi_ac(self, event):
        yol = self.tree1.identify_row(event.y)
        if yol and os.path.exists(yol):
            os.startfile(yol)

    def listeden_cikar(self):
        for iid in self.tree1.selection():
            self.tree1.delete(iid)

    def taramayı_baslat_thread(self):
        secilen = filedialog.askdirectory()
        if not secilen: return
        self.iptal_kopya = False
        self.tree1.delete(*self.tree1.get_children())
        self.tara_btn1.config(state="disabled")
        self.iptal_btn1.config(state="normal")
        threading.Thread(target=self.tara_mantigi, args=(secilen,), daemon=True).start()

    def tara_mantigi(self, yol):
        """Arka plan thread'i: resimleri hash'ler, kopya gruplarını arayüze bırakır."""
        bulunanlar = [os.path.join(r, f) for r, d, files in os.walk(yol) for f in files if f.lower().endswith(RESIM_UZANTILARI)]
        self.arayuzde(self.progress1.configure, {"maximum": max(len(bulunanlar), 1), "value": 0})
        hashes, okunamayan = {}, 0
        for i, f_yol in enumerate(bulunanlar):
            if self.iptal_kopya: break
            if i % 20 == 0: self.arayuzde(self.progress1.configure, {"value": i})
            try:
                with Image.open(f_yol) as img: h = str(imagehash.phash(img))
                stat = os.stat(f_yol)
                ad = os.path.basename(f_yol)
                info = {"yol": f_yol, "ad": ad, "boyut": f"{stat.st_size / 1048576:.2f} MB", "klasor": os.path.dirname(f_yol), "tarih": min(stat.st_mtime, stat.st_ctime), "is_copy": bool(KOPYA_ADI.search(ad))}
                hashes.setdefault(h, []).append(info)
            except Exception:
                okunamayan += 1
        gruplar = [l for l in hashes.values() if len(l) > 1]
        for l in gruplar: l.sort(key=lambda x: (x["is_copy"], x["tarih"]))
        self.arayuzde(self.kopyalari_goster, gruplar, len(bulunanlar), okunamayan)

    def kopyalari_goster(self, gruplar, taranan, okunamayan):
        for g_id, l in enumerate(gruplar, start=1):
            for idx, d in enumerate(l):
                asil = idx == 0
                self.tree1.insert("", "end", iid=d["yol"], values=(f"#{g_id}", "ASIL" if asil else "KOPYA", d["ad"], d["boyut"], d["klasor"]), tags="asil" if asil else "kopya")
        kopya_sayisi = sum(len(l) - 1 for l in gruplar)
        durum = "⏹️ Durduruldu" if self.iptal_kopya else "✅ Bitti"
        ek = f" | {okunamayan} dosya okunamadı" if okunamayan else ""
        self.kopya_etiket.config(text=f"{durum}: {taranan} resim, {len(gruplar)} grup, {kopya_sayisi} kopya{ek}", fg="green")
        self.progress1.configure(value=self.progress1["maximum"])
        self.tara_btn1.config(state="normal"); self.iptal_btn1.config(state="disabled")

    def kopya_satirlari(self):
        return [i for i in self.tree1.get_children() if self.tree1.item(i)["values"][1] == "KOPYA"]

    def tum_kopyalari_sil(self):
        items = self.kopya_satirlari()
        if not items: return
        if not messagebox.askyesno("Onay", f"{len(items)} kopya Geri Dönüşüm Kutusu'na gönderilsin mi?"): return
        hatalar = []
        for yol in items:
            try:
                geri_donusume_gonder(yol)
                self.tree1.delete(yol)
            except Exception as e: hatalar.append((yol, e))
        if hatalar: messagebox.showwarning("Bazı dosyalar silinemedi", hata_ozeti(hatalar))

    def tum_kopyalari_tasi(self):
        items = self.kopya_satirlari()
        if not items: return
        hedef = filedialog.askdirectory(title="Kopyalar hangi klasöre taşınsın?")
        if not hedef: return
        hatalar = []
        for yol in items:
            try:
                shutil.move(yol, benzersiz_yol(hedef, os.path.basename(yol)))
                self.tree1.delete(yol)
            except Exception as e: hatalar.append((yol, e))
        if hatalar: messagebox.showwarning("Bazı dosyalar taşınamadı", hata_ozeti(hatalar))


if __name__ == "__main__":
    app = KopyaUygulamasi()
    app.root.mainloop()
