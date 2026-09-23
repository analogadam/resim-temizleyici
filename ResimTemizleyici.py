import os
import re
import sys
import io
import ctypes
import hashlib
import subprocess
import threading
import shutil
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from PIL import Image
import imagehash

# Windows konsolunda emoji / Türkçe karakter çıktısı için
if sys.platform.startswith("win"):
    if sys.stdout is not None:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SURUM = "1.2.0"
RESIM_UZANTILARI = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
# Windows'un kopya adlandırmaları: "x - Kopya.jpg", "x - Copy.jpg", "x (1).jpg"
KOPYA_ADI = re.compile(r"kopya|copy|\(\d+\)", re.IGNORECASE)
YUKLENIYOR = "yükleniyor..."


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


def dosya_ozeti(yol):
    h = hashlib.sha256()
    with open(yol, "rb") as f:
        for parca in iter(lambda: f.read(1 << 20), b""):
            h.update(parca)
    return h.hexdigest()


def format_bytes(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0: return f"{size:.2f} {unit}"
        size /= 1024.0


def hata_ozeti(hatalar):
    ilk = "\n".join(f"• {os.path.basename(yol)}: {e}" for yol, e in hatalar[:10])
    kalan = f"\n... ve {len(hatalar) - 10} dosya daha" if len(hatalar) > 10 else ""
    return f"{len(hatalar)} dosyada işlem yapılamadı:\n\n{ilk}{kalan}"


def konumu_goster(yol):
    if os.path.exists(yol):
        subprocess.run(["explorer", "/select,", os.path.normpath(yol)])


def dosyayi_ac(yol):
    try: os.startfile(yol)
    except OSError as e: messagebox.showerror("Hata", f"Dosya açılamadı: {e}")


# --- KOPYA BULMA MANTIĞI (arayüzden bağımsız) ---
@dataclass
class Resim:
    yol: str
    boyut: int
    genislik: int
    yukseklik: int
    tarih: float
    ozet: str = ""  # sha256; yalnız kopya grubundaki dosyalar için hesaplanır

    @property
    def ad(self): return os.path.basename(self.yol)

    @property
    def cozunurluk(self): return f"{self.genislik}x{self.yukseklik}"


def asil_onceligi(r):
    """Küçük olan ASIL olur: adında 'kopya' geçmeyen, çözünürlüğü yüksek, en eski dosya."""
    return (bool(KOPYA_ADI.search(r.ad)), -(r.genislik * r.yukseklik), r.tarih)


def kopya_gruplarini_bul(klasor, iptal_mi, ilerleme):
    """Görsel olarak aynı resimleri (perceptual hash) gruplar; her grubun ilk elemanı ASIL adayıdır."""
    yollar = [os.path.join(r, f) for r, _, dosyalar in os.walk(klasor) for f in dosyalar if f.lower().endswith(RESIM_UZANTILARI)]
    hashler, okunamayan = {}, 0
    for i, yol in enumerate(yollar):
        if iptal_mi(): break
        ilerleme(i, len(yollar))
        try:
            with Image.open(yol) as img:
                genislik, yukseklik = img.size
                img.draft("RGB", (64, 64))  # JPEG'i küçük çözer; phash zaten 32x32'ye indiriyor
                h = str(imagehash.phash(img))
            st = os.stat(yol)
            hashler.setdefault(h, []).append(Resim(yol, st.st_size, genislik, yukseklik, min(st.st_mtime, st.st_ctime)))
        except Exception:
            okunamayan += 1

    gruplar = [sorted(l, key=asil_onceligi) for l in hashler.values() if len(l) > 1]
    for grup in gruplar:
        for r in grup:
            try: r.ozet = dosya_ozeti(r.yol)
            except OSError: pass
    return gruplar, len(yollar), okunamayan


class KopyaUygulamasi:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"Gelişmiş Depo Yöneticisi & Dosya Gezgini — v{SURUM}")
        self.root.geometry("1300x850")

        self.iptal_kopya = False
        self.iptal_boyut = False
        self.resimler = {}  # yol -> Resim (tree1'deki satırlar)
        self.tarama_bilgisi = ""

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

    def sag_tik_bagla(self, tree, menu):
        def goster(event):
            satir = tree.identify_row(event.y)
            if not satir: return
            if satir not in tree.selection(): tree.selection_set(satir)
            tree.focus(satir)
            menu.tk_popup(event.x_root, event.y_root)
        tree.bind("<Button-3>", goster)

    def durdur_sinyali(self, hedef):
        if hedef == "kopya": self.iptal_kopya = True
        else: self.iptal_boyut = True

    # --- SEKME 2: KLASÖR BOYUT ANALİZİ ---
    def arayuz_boyut_analizoru(self):
        ust = tk.Frame(self.tab2, pady=15, bg="#f8f9fa")
        ust.pack(fill="x")

        self.tara_btn2 = tk.Button(ust, text="📁 Ana Klasör Seç", command=self.boyut_analizi_thread, bg="#28a745", fg="white", font=("Arial", 10, "bold"), padx=20)
        self.tara_btn2.pack(side="left", padx=20)

        self.iptal_btn2 = tk.Button(ust, text="🛑 Durdur", command=lambda: self.durdur_sinyali("boyut"), bg="#dc3545", fg="white", state="disabled", padx=20)
        self.iptal_btn2.pack(side="left")

        # Sağ taraftaki işlem butonları
        tk.Button(ust, text="🗑️ Seçilenleri Sil", command=self.analiz_sil, bg="#f44336", fg="white", padx=15).pack(side="right", padx=10)
        tk.Button(ust, text="📂 Konumu Göster", command=lambda: konumu_goster(self.analiz_odak_yolu()), bg="#6c757d", fg="white", padx=15).pack(side="right", padx=10)

        self.progress2 = ttk.Progressbar(self.tab2, orient="horizontal", mode="indeterminate")
        self.progress2.pack(fill="x", padx=20, pady=10)

        self.analiz_etiket = tk.Label(self.tab2, text="💡 Sağ tıkla menü açılır, Ctrl+A ile hepsi seçilir. Silinenler Geri Dönüşüm Kutusu'na gider.", fg="gray")
        self.analiz_etiket.pack()

        self.tree2 = ttk.Treeview(self.tab2, columns=("Boyut", "Yuzde", "TamYol"), show="tree headings", selectmode="extended")
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
        self.tree2.bind("<Control-a>", lambda e: self.tree2.selection_set(self.tree2.get_children()) or "break")

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="▶ Aç", command=lambda: dosyayi_ac(self.analiz_odak_yolu()))
        menu.add_command(label="📂 Konumu Göster", command=lambda: konumu_goster(self.analiz_odak_yolu()))
        menu.add_separator()
        menu.add_command(label="🗑️ Seçilenleri Sil", command=self.analiz_sil, foreground="red")
        self.sag_tik_bagla(self.tree2, menu)

    def analiz_odak_yolu(self):
        item_id = self.tree2.focus()
        return self.tree2.set(item_id, "TamYol") if item_id else ""

    def analiz_secilenleri(self):
        """Seçili öğeler; üst klasörü de seçili olanlar zaten onunla gideceği için atlanır."""
        secili = set(self.tree2.selection())
        def ustu_secili(iid):
            ust = self.tree2.parent(iid)
            while ust:
                if ust in secili: return True
                ust = self.tree2.parent(ust)
            return False
        return [i for i in self.tree2.selection() if self.tree2.set(i, "TamYol") and not ustu_secili(i)]

    def analiz_sil(self):
        secili = self.analiz_secilenleri()
        if not secili: return
        ne = f"'{self.tree2.item(secili[0], 'text').strip()}'" if len(secili) == 1 else f"{len(secili)} öğe"
        if not messagebox.askyesno("Silme Onayı", f"{ne} (varsa altındakilerle birlikte) Geri Dönüşüm Kutusu'na gönderilsin mi?"): return
        hatalar = []
        for iid in secili:
            yol = self.tree2.set(iid, "TamYol")
            try:
                geri_donusume_gonder(yol)
                self.tree2.delete(iid)
            except Exception as e: hatalar.append((yol, e))
        if hatalar: messagebox.showwarning("Bazı öğeler silinemedi", hata_ozeti(hatalar))

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
                self.tree2.insert(node, "end", text=YUKLENIYOR)

        self.analiz_etiket.config(text=f"✅ Klasör: {os.path.basename(yol)} | Toplam: {format_bytes(ana_toplam)}", fg="green")
        self.progress2.stop()
        self.tara_btn2.config(state="normal", text="📁 Ana Klasör Seç")
        self.iptal_btn2.config(state="disabled")

    def klasor_genislet(self, event):
        item_id = self.tree2.focus()
        cocuklar = self.tree2.get_children(item_id)
        if len(cocuklar) == 1 and self.tree2.item(cocuklar[0])["text"] == YUKLENIYOR:
            self.tree2.delete(cocuklar[0])
            tam_yol = self.tree2.set(item_id, "TamYol")
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
        self.kopya_etiket = tk.Label(self.tab1, text="💡 Çift tıkla resmi açar, sağ tık menüsünden ASIL seçimini değiştirebilirsiniz. 'Eşleşme: Benzer' olanlara silmeden önce bakın.", fg="gray")
        self.kopya_etiket.pack()

        liste = tk.Frame(self.tab1)
        liste.pack(fill="both", expand=True, padx=20)
        sutunlar = {"Grup": 60, "Durum": 70, "Dosya": 260, "Çözünürlük": 100, "Boyut": 90, "Eşleşme": 90, "Konum": 500}
        self.tree1 = ttk.Treeview(liste, columns=tuple(sutunlar), show="headings", selectmode="extended")
        for col, genislik in sutunlar.items():
            self.tree1.heading(col, text=col)
            self.tree1.column(col, width=genislik, anchor="w" if col in ("Dosya", "Konum") else "center")
        sb = ttk.Scrollbar(liste, orient="vertical", command=self.tree1.yview)
        self.tree1.configure(yscrollcommand=sb.set)
        self.tree1.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree1.tag_configure("asil", background="#d1e7dd"); self.tree1.tag_configure("kopya", background="#f8d7da")
        self.tree1.bind("<Double-1>", lambda e: self.tree1.identify_row(e.y) and dosyayi_ac(self.tree1.identify_row(e.y)))
        self.tree1.bind("<Control-a>", lambda e: self.tree1.selection_set(self.tree1.get_children()) or "break")

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="▶ Resmi Aç", command=lambda: dosyayi_ac(self.tree1.focus()))
        menu.add_command(label="📂 Konumu Göster", command=lambda: konumu_goster(self.tree1.focus()))
        menu.add_command(label="⭐ ASIL Yap", command=self.asil_yap)
        menu.add_separator()
        menu.add_command(label="➖ Listeden Çıkar", command=self.listeden_cikar)
        menu.add_command(label="🚚 Seçilenleri Taşı", command=lambda: self.dosyalari_tasi(self.tree1.selection()))
        menu.add_command(label="🗑️ Seçilenleri Sil", command=lambda: self.dosyalari_sil(self.tree1.selection()), foreground="red")
        self.sag_tik_bagla(self.tree1, menu)

        alt = tk.Frame(self.tab1, pady=10)
        alt.pack(fill="x")
        tk.Button(alt, text="🚚 Tüm Kopyaları Taşı", command=lambda: self.dosyalari_tasi(self.kopya_satirlari()), bg="#9C27B0", fg="white", padx=15).pack(side="right", padx=10)
        tk.Button(alt, text="🗑️ Tüm Kopyaları Sil", command=lambda: self.dosyalari_sil(self.kopya_satirlari()), bg="#f44336", fg="white", padx=15).pack(side="right", padx=10)
        tk.Button(alt, text="➖ Seçileni Listeden Çıkar", command=self.listeden_cikar, bg="#6c757d", fg="white", padx=15).pack(side="left", padx=20)

    def taramayı_baslat_thread(self):
        secilen = filedialog.askdirectory()
        if not secilen: return
        self.iptal_kopya = False
        self.tree1.delete(*self.tree1.get_children())
        self.resimler.clear()
        self.kopya_etiket.config(text="⏳ Resimler taranıyor...", fg="blue")
        self.tara_btn1.config(state="disabled")
        self.iptal_btn1.config(state="normal")
        threading.Thread(target=self.tara_mantigi, args=(secilen,), daemon=True).start()

    def tara_mantigi(self, yol):
        """Arka plan thread'i: kopya gruplarını bulur, sonucu arayüze bırakır."""
        def ilerleme(i, toplam):
            if i % 20 == 0: self.arayuzde(self.progress1.configure, {"maximum": max(toplam, 1), "value": i})
        sonuc = kopya_gruplarini_bul(yol, lambda: self.iptal_kopya, ilerleme)
        self.arayuzde(self.kopyalari_goster, *sonuc)

    def kopyalari_goster(self, gruplar, taranan, okunamayan):
        for g_id, grup in enumerate(gruplar, start=1):
            for idx, r in enumerate(grup):
                self.resimler[r.yol] = r
                self.tree1.insert("", "end", iid=r.yol, values=(f"#{g_id}", "ASIL" if idx == 0 else "KOPYA", r.ad, r.cozunurluk, format_bytes(r.boyut), "", os.path.dirname(r.yol)))
            self.grubu_boya(f"#{g_id}")
        durum = "⏹️ Durduruldu" if self.iptal_kopya else "✅ Bitti"
        ek = f", {okunamayan} okunamadı" if okunamayan else ""
        self.tarama_bilgisi = f"{durum}: {taranan} resim tarandı{ek}"
        self.ozet_guncelle()
        self.progress1.configure(value=self.progress1["maximum"])
        self.tara_btn1.config(state="normal"); self.iptal_btn1.config(state="disabled")

    # --- GRUP YÖNETİMİ ---
    def grup_satirlari(self, grup):
        return [i for i in self.tree1.get_children() if self.tree1.set(i, "Grup") == grup]

    def kopya_satirlari(self):
        return [i for i in self.tree1.get_children() if self.tree1.set(i, "Durum") == "KOPYA"]

    def grubu_boya(self, grup):
        """Renkleri ve ASIL'e göre 'Eşleşme' sütununu günceller."""
        satirlar = self.grup_satirlari(grup)
        asil = next(i for i in satirlar if self.tree1.set(i, "Durum") == "ASIL")
        for i in satirlar:
            if i == asil:
                self.tree1.set(i, "Eşleşme", "—"); self.tree1.item(i, tags="asil")
            else:
                ayni = self.resimler[i].ozet and self.resimler[i].ozet == self.resimler[asil].ozet
                self.tree1.set(i, "Eşleşme", "Birebir" if ayni else "Benzer"); self.tree1.item(i, tags="kopya")

    def grubu_duzenle(self, grup):
        """Satır silindikten sonra: tek kalan grup listeden düşer, ASIL'siz kalan gruba yeni ASIL atanır."""
        satirlar = self.grup_satirlari(grup)
        if len(satirlar) < 2:
            for i in satirlar:
                self.tree1.delete(i); self.resimler.pop(i, None)
            return
        if not any(self.tree1.set(i, "Durum") == "ASIL" for i in satirlar):
            yeni = min(satirlar, key=lambda i: asil_onceligi(self.resimler[i]))
            self.tree1.set(yeni, "Durum", "ASIL")
        self.grubu_boya(grup)

    def satirlari_kaldir(self, yollar):
        gruplar = {self.tree1.set(y, "Grup") for y in yollar}
        for y in yollar:
            self.tree1.delete(y); self.resimler.pop(y, None)
        for g in gruplar: self.grubu_duzenle(g)
        self.ozet_guncelle()

    def ozet_guncelle(self):
        kopyalar = self.kopya_satirlari()
        grup_sayisi = len({self.tree1.set(i, "Grup") for i in self.tree1.get_children()})
        kazanc = sum(self.resimler[y].boyut for y in kopyalar)
        self.kopya_etiket.config(text=f"{self.tarama_bilgisi} | {grup_sayisi} grup, {len(kopyalar)} kopya | Kopyalar silinirse açılacak alan: {format_bytes(kazanc)}", fg="green")

    def asil_yap(self):
        secili = self.tree1.selection()
        if len(secili) != 1:
            messagebox.showinfo("ASIL Yap", "ASIL yapmak için tek bir satır seçin."); return
        grup = self.tree1.set(secili[0], "Grup")
        for i in self.grup_satirlari(grup): self.tree1.set(i, "Durum", "KOPYA")
        self.tree1.set(secili[0], "Durum", "ASIL")
        self.grubu_boya(grup)
        self.ozet_guncelle()

    def listeden_cikar(self):
        self.satirlari_kaldir(self.tree1.selection())

    # --- DOSYA İŞLEMLERİ ---
    def dosyalari_sil(self, yollar):
        if not yollar: return
        asil = sum(1 for y in yollar if self.tree1.set(y, "Durum") == "ASIL")
        uyari = f"\n\n⚠️ Bunların {asil} tanesi ASIL dosya!" if asil else ""
        if not messagebox.askyesno("Onay", f"{len(yollar)} dosya Geri Dönüşüm Kutusu'na gönderilsin mi?{uyari}"): return
        self.toplu_islem(yollar, geri_donusume_gonder, "Bazı dosyalar silinemedi")

    def dosyalari_tasi(self, yollar):
        if not yollar: return
        hedef = filedialog.askdirectory(title=f"{len(yollar)} dosya hangi klasöre taşınsın?")
        if not hedef: return
        self.toplu_islem(yollar, lambda y: shutil.move(y, benzersiz_yol(hedef, os.path.basename(y))), "Bazı dosyalar taşınamadı")

    def toplu_islem(self, yollar, islem, hata_basligi):
        basarili, hatalar = [], []
        for yol in yollar:
            try:
                islem(yol); basarili.append(yol)
            except Exception as e: hatalar.append((yol, e))
        self.satirlari_kaldir(basarili)
        if hatalar: messagebox.showwarning(hata_basligi, hata_ozeti(hatalar))


if __name__ == "__main__":
    app = KopyaUygulamasi()
    app.root.mainloop()
