"""Basit Minecraft Launcher (Python + CustomTkinter) - modern koyu arayüz

Özellikler: Vanilla / Fabric / Forge, snapshot sürümleri, profiller,
            DURDUR (force kill), Microsoft girişi (Client ID gerekir).

Kurulum:  py -m pip install -U customtkinter minecraft-launcher-lib certifi
Çalıştır: py launcher.py
Gereken:  Java (yeni sürümler için Java 17/21).

Klasörler (hepsi %APPDATA%\\.basit_launcher altında, .minecraft'a dokunmaz):
  libraries/assets/versions -> ortak (tekrar indirmemek için)
  instances/<profil adı>/   -> her profilin kendi mods, saves, config klasörü

Dosya iki parça:
  1) Core -> tüm mantık (profiller, indirme, giriş, başlatma). Arayüz yok.
  2) UI   -> arayüz (App ve sayfalar). Arayüzü değiştirirken Core'a dokunma.
"""
import hashlib
import http.server
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from tkinter import messagebox

import customtkinter as ctk
import minecraft_launcher_lib as mll

# Windows'ta bazı Python kurulumlarında sertifika deposu eksik olur
# (SSL: CERTIFICATE_VERIFY_FAILED). certifi'nin sertifikalarını kullanıyoruz.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = ssl.create_default_context()

# --- AYARLAR ---------------------------------------------------------------
CLIENT_ID = "8c68f1bf-d8ec-44d4-a8c4-37ef5a67f95e"   # Microsoft girişi için Azure Client ID
LOGIN_PORT = 53682
REDIRECT_URI = f"http://localhost:{LOGIN_PORT}"   # Azure'da "http://localhost" olarak kayıtlı olmalı
# ---------------------------------------------------------------------------

# Bu launcher'ın KENDİ klasörü: resmi .minecraft'tan ve diğer launcher'lardan ayrı.
ROOT_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        ".basit_launcher")
MC_DIR = ROOT_DIR                                   # ortak: libraries, assets, versions
INSTANCES_DIR = os.path.join(ROOT_DIR, "instances")  # her profilin kendi oyun klasörü
SESSION_FILE = os.path.join(ROOT_DIR, "session.json")
PROFILE_FILE = os.path.join(ROOT_DIR, "profiles.json")
LOADERS = ("vanilla", "fabric", "forge")
DEFAULTS = {"name": "Steve", "version": "", "loader": "vanilla",
            "ram": 4, "snapshots": False, "perf": True, "perf_mods": True}

# --- PERFORMANS ------------------------------------------------------------
# JVM ayarları: GC takılmalarını (stutter) azaltır, kare süresini düzeltir.
PERF_JVM = [
    "-XX:+UseG1GC", "-XX:+UnlockExperimentalVMOptions",
    "-XX:G1NewSizePercent=20", "-XX:G1ReservePercent=20",
    "-XX:MaxGCPauseMillis=50", "-XX:G1HeapRegionSize=32M",
]
# Fabric için FPS modları (Modrinth slug, dosya adı başlangıcı)
PERF_MODS = [
    ("fabric-api", "fabricapi"),
    ("sodium", "sodium"),
    ("lithium", "lithium"),
    ("ferrite-core", "ferritecore"),
    ("modernfix", "modernfix"),
    ("entityculling", "entityculling"),
    ("immediatelyfast", "immediatelyfast"),
]
MODRINTH_API = "https://api.modrinth.com/v2"
HEADERS = {"User-Agent": "basit-launcher/1.0 (kisisel proje)"}
KIND_FOLDER = {"mod": "mods", "resourcepack": "resourcepacks"}
# ---------------------------------------------------------------------------


def client_id_set():
    """CLIENT_ID gerçek bir Azure kimliği (GUID) gibi görünüyorsa True."""
    cid = CLIENT_ID.strip()
    return len(cid) == 36 and cid.count("-") == 4 and not cid.startswith("BURAYA")


def offline_uuid(name):
    """Çevrimdışı sunucuların kullandığı, isme bağlı sabit UUID."""
    h = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def friendly(e):
    kind = type(e).__name__
    if kind == "AzureAppNotPermitted":
        return ("Azure uygulamanın Minecraft API'sini kullanma izni yok. "
                "Mojang'a izin başvurusu yapman gerekiyor.")
    if kind == "AccountNotOwnMinecraft":
        return "Bu Microsoft hesabında Minecraft (Java) yok."
    return f"{kind}: {e}"


class Cancelled(Exception):
    pass


# =========================================================== MANTIK (Core)
class Core:
    def __init__(self):
        os.makedirs(MC_DIR, exist_ok=True)
        os.makedirs(INSTANCES_DIR, exist_ok=True)
        lp = os.path.join(MC_DIR, "launcher_profiles.json")  # Forge kurucusu ister
        if not os.path.exists(lp):
            self._write(lp, {"profiles": {}})
        self.proc = None
        self.cancel = False
        self.session = None          # Microsoft girişi yapıldıysa dolu
        self._vlist = None
        self.data = self._read(PROFILE_FILE, {"last": "", "profiles": {}})

    # ---- dosya yardımcıları
    @staticmethod
    def _read(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write(path, obj):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ---- profiller
    def profile_names(self):
        return sorted(self.data["profiles"])

    def get_profile(self, name):
        p = self.data["profiles"].get(name)
        return {**DEFAULTS, **p} if p else None

    def last_profile(self):
        return self.data.get("last", "")

    def save_profile(self, name, profile):
        self.data["profiles"][name] = profile
        self.data["last"] = name
        self._write(PROFILE_FILE, self.data)

    def delete_profile(self, name):
        self.data["profiles"].pop(name, None)
        if self.data.get("last") == name:
            self.data["last"] = ""
        self._write(PROFILE_FILE, self.data)

    # ---- profil klasörleri (her profilin kendi mods/saves/config klasörü)
    @staticmethod
    def instance_dir(profile):
        safe = "".join(c if c.isalnum() or c in " -_" else "_"
                       for c in (profile or "").strip()) or "varsayilan"
        path = os.path.join(INSTANCES_DIR, safe)
        for sub in KIND_FOLDER.values():
            os.makedirs(os.path.join(path, sub), exist_ok=True)
        return path

    @staticmethod
    def open_folder(path):
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    # ---- sürümler
    def get_versions(self, snapshots=False):
        if self._vlist is None:
            self._vlist = mll.utils.get_version_list()
        types = {"release", "snapshot"} if snapshots else {"release"}
        return [v["id"] for v in self._vlist if v["type"] in types]

    # ---- Microsoft girişi
    def begin_login(self):
        if not client_id_set():
            raise RuntimeError("launcher.py başındaki CLIENT_ID satırı boş veya yanlış.\n"
                               "Azure'daki Uygulama (istemci) kimliğini tırnakların içine yapıştır.")
        return mll.microsoft_account.get_secure_login_data(CLIENT_ID, REDIRECT_URI)

    def wait_for_redirect(self, port=LOGIN_PORT, timeout=240):
        """Tarayıcı giriş sonrası http://localhost:PORT/?code=... adresine döner.
        Bu küçük sunucu o adresi yakalar ve tam URL'yi döner (süre dolarsa None)."""
        result = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(h):
                if "code=" in h.path or "error=" in h.path:
                    result["url"] = f"http://localhost:{port}{h.path}"
                    body = ("<html><head><meta charset='utf-8'></head>"
                            "<body style='font-family:sans-serif;background:#0e1016;"
                            "color:#e9ecf5;text-align:center;padding-top:80px'>"
                            "<h2>Giriş tamamlandı</h2>"
                            "<p>Bu sekmeyi kapatıp launcher'a dönebilirsin.</p>"
                            "</body></html>").encode("utf-8")
                else:
                    body = b""
                h.send_response(200)
                h.send_header("Content-Type", "text/html; charset=utf-8")
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                h.wfile.write(body)

            def log_message(h, *args):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
        srv.timeout = 1
        try:
            end = time.time() + timeout
            while "url" not in result and time.time() < end:
                srv.handle_request()
        finally:
            srv.server_close()
        return result.get("url")

    def finish_login(self, redirected, state, verifier):
        code = mll.microsoft_account.parse_auth_code_url(redirected, state)
        res = mll.microsoft_account.complete_login(
            CLIENT_ID, None, REDIRECT_URI, code, verifier)
        self._set_session(res)

    def _set_session(self, res):
        self.session = {
            "name": res["name"],
            "id": res["id"],
            "access_token": res["access_token"],
            "refresh_token": res.get("refresh_token", ""),
        }
        self._write(SESSION_FILE, self.session)

    def refresh_saved_login(self):
        """Kayıtlı oturum varsa yeniler. Başarılıysa True döner."""
        if not client_id_set() or not os.path.exists(SESSION_FILE):
            return False
        token = self._read(SESSION_FILE, {}).get("refresh_token")
        if not token:
            return False
        try:
            self._set_session(mll.microsoft_account.complete_refresh(CLIENT_ID, None, REDIRECT_URI, token))
            return True
        except Exception:
            return False

    def logout(self):
        self.session = None
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass

    # ---- kurulum + başlatma
    def make_callback(self, status, progress, maximum):
        """İndirme geri çağrıları. DURDUR'a basıldıysa indirmeyi keser."""
        def guard(fn):
            def wrapper(arg):
                if self.cancel:
                    raise Cancelled()
                fn(arg)
            return wrapper
        return {"setStatus": guard(status), "setProgress": guard(progress),
                "setMax": guard(maximum)}

    def prepare_version(self, mc, loader, callback):
        """Sürümü (ve gerekirse Fabric/Forge'u) kurar, başlatma id'sini döner."""
        if loader == "vanilla":
            mll.install.install_minecraft_version(mc, MC_DIR, callback=callback)
            return mc

        if not hasattr(mll, "mod_loader"):
            raise RuntimeError("Kütüphane eski. Şunu çalıştır:\n"
                               "py -m pip install -U minecraft-launcher-lib")
        ml = mll.mod_loader.get_mod_loader(loader)
        if mc not in ml.get_minecraft_versions(False):
            raise RuntimeError(f"{ml.get_name()}, {mc} sürümünü desteklemiyor.")
        lv = ml.get_latest_loader_version(mc)
        vid = ml.get_installed_version(mc, lv)
        installed = {v["id"] for v in mll.utils.get_installed_versions(MC_DIR)}
        if vid not in installed:
            ml.install(mc, MC_DIR, loader_version=lv, callback=callback)
        return vid

    # ---- modpaketi (= kendi klasörü olan yeni profil)
    def create_modpack(self, name, settings):
        name = name.strip()
        if not name:
            raise ValueError("Bir isim yazmalısın.")
        if name in self.data["profiles"]:
            raise ValueError("Bu isimde bir profil/modpaketi zaten var.")
        self.instance_dir(name)
        self.save_profile(name, {k: v for k, v in settings.items() if k != "profile"})

    # ---- Modrinth: arama + indirme (mod ve resourcepack)
    @staticmethod
    def _get_json(url, timeout=20):
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=timeout) as r:
            return json.load(r)

    @staticmethod
    def _download(url, timeout=60):
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=timeout) as r:
            return r.read()

    def modrinth_search(self, query, kind, mc, loader, limit=25):
        """kind: 'mod' veya 'resourcepack'. Sadece seçili sürüme uyanlar gelir."""
        facets = [[f"project_type:{kind}"], [f"versions:{mc}"]]
        if kind == "mod":
            facets.append([f"categories:{loader}"])
        q = urllib.parse.urlencode({"query": query,
                                    "facets": json.dumps(facets),
                                    "limit": limit})
        return self._get_json(f"{MODRINTH_API}/search?{q}")["hits"]

    def modrinth_install(self, project, kind, mc, loader, game_dir, _seen=None):
        """Projeyi (modlarda zorunlu bağımlılıklarıyla) profil klasörüne indirir.
        Yeni eklenen dosya adlarını liste olarak döner."""
        seen = _seen if _seen is not None else set()
        if project in seen:
            return []
        seen.add(project)

        info = self._get_json(f"{MODRINTH_API}/project/{project}")
        slug = info["slug"]
        seen.add(slug)
        folder = os.path.join(game_dir, KIND_FOLDER[kind])
        os.makedirs(folder, exist_ok=True)

        # Aynı mod zaten kuruluysa tekrar ekleme (çift mod oyunu çökertir)
        token = slug.lower().replace("-", "").replace("_", "")
        for f in os.listdir(folder):
            if f.lower().replace("-", "").replace("_", "").startswith(token):
                return []

        params = {"game_versions": json.dumps([mc])}
        if kind == "mod":
            params["loaders"] = json.dumps([loader])
        versions = self._get_json(
            f"{MODRINTH_API}/project/{slug}/version?{urllib.parse.urlencode(params)}")
        if not versions:
            raise RuntimeError(f"'{info['title']}' için {mc} ({loader}) "
                               "sürümüne uygun dosya yok.")
        v = next((x for x in versions if x.get("version_type") == "release"),
                 versions[0])

        installed = []
        if kind == "mod":
            for dep in v.get("dependencies", []):
                if dep.get("dependency_type") == "required" and dep.get("project_id"):
                    installed += self.modrinth_install(
                        dep["project_id"], kind, mc, loader, game_dir, seen)

        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        data = self._download(f["url"])
        want = f.get("hashes", {}).get("sha512")
        if want and hashlib.sha512(data).hexdigest() != want:
            raise RuntimeError("İndirilen dosya doğrulanamadı (hash uyuşmadı).")
        filename = os.path.basename(f["filename"])
        with open(os.path.join(folder, filename), "wb") as out:
            out.write(data)
        installed.append(filename)
        return installed

    def install_perf_mods(self, mc, game_dir, say):
        """Fabric profiline FPS modlarını Modrinth'ten indirir.
        Profilde zaten olan modları atlar. Bir mod inmezse oyunu engellemez."""
        mods_dir = os.path.join(game_dir, "mods")
        have = [f.lower().replace("-", "").replace("_", "")
                for f in os.listdir(mods_dir)]
        added = []
        for slug, token in PERF_MODS:
            if self.cancel:
                raise Cancelled()
            if any(f.startswith(token) for f in have):
                continue
            try:
                say(f"{slug} kontrol ediliyor...")
                q = urllib.parse.urlencode({"loaders": json.dumps(["fabric"]),
                                            "game_versions": json.dumps([mc])})
                req = urllib.request.Request(
                    f"{MODRINTH_API}/project/{slug}/version?{q}", headers=HEADERS)
                with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=20) as r:
                    versions = json.load(r)
                if not versions:
                    continue          # bu Minecraft sürümü için yok
                v = next((x for x in versions
                          if x.get("version_type") == "release"), versions[0])
                f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
                req = urllib.request.Request(f["url"], headers=HEADERS)
                with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=60) as r:
                    data = r.read()
                want = f.get("hashes", {}).get("sha512")
                if want and hashlib.sha512(data).hexdigest() != want:
                    continue          # bozuk indirme, ekleme
                with open(os.path.join(mods_dir, os.path.basename(f["filename"])),
                          "wb") as out:
                    out.write(data)
                added.append(slug)
            except Cancelled:
                raise
            except Exception:
                continue
        return added

    def launch(self, p, callback, on_started=None):
        """Bloklar: kurar, oyunu açar, oyun kapanana kadar bekler."""
        self.cancel = False
        vid = self.prepare_version(p["version"], p["loader"], callback)
        if self.cancel:
            raise Cancelled()

        game_dir = self.instance_dir(p.get("profile", ""))
        if p.get("perf_mods") and p["loader"] == "fabric":
            added = self.install_perf_mods(p["version"], game_dir,
                                           callback["setStatus"])
            if added:
                callback["setStatus"]("Eklenen modlar: " + ", ".join(added))

        if self.session:
            options = {"username": self.session["name"],
                       "uuid": self.session["id"],
                       "token": self.session["access_token"]}
        else:
            name = p["name"].strip()
            options = {"username": name, "uuid": offline_uuid(name), "token": ""}
        ram = int(float(p["ram"]))
        options["jvmArguments"] = [f"-Xmx{ram}G", f"-Xms{ram}G"]
        if p.get("perf"):
            options["jvmArguments"] += PERF_JVM
        options["gameDirectory"] = game_dir      # mods, saves, config buraya

        cmd = mll.command.get_minecraft_command(vid, MC_DIR, options)
        self.proc = subprocess.Popen(cmd, cwd=game_dir)
        if on_started:
            on_started()
        try:
            self.proc.wait()
        finally:
            self.proc = None

    def stop(self):
        """İndirmeyi iptal eder, oyun açıksa zorla kapatır (force kill)."""
        self.cancel = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()


# ============================================================ ARAYÜZ (UI)
# --- RENKLER ---------------------------------------------------------------
BG = "#0e1016"
SIDEBAR = "#131620"
CARD = "#1a1e2b"
CARD_HI = "#232940"
HERO = "#18213a"
TEXT = "#e9ecf5"
MUTED = "#8991a8"
ACCENT = "#5b8cff"
ACCENT_HI = "#7aa2ff"
GREEN = "#2fbf71"
GREEN_HI = "#3ad384"
RED = "#e5484d"
RED_HI = "#f0636a"
FONT = "Segoe UI"


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


# ============================================================ ANA PENCERE
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.core = Core()
        self.title("Basit Launcher")
        self.geometry("1060x660")
        self.minsize(940, 580)
        self.configure(fg_color=BG)

        # --- ayar değişkenleri (tüm sayfalar bunları paylaşır)
        self.v_name = ctk.StringVar(value=DEFAULTS["name"])
        self.v_loader = ctk.StringVar(value=DEFAULTS["loader"])
        self.v_version = ctk.StringVar(value="")
        self.v_ram = ctk.DoubleVar(value=DEFAULTS["ram"])
        self.v_snap = ctk.BooleanVar(value=False)
        self.v_perf = ctk.BooleanVar(value=True)
        self.v_permods = ctk.BooleanVar(value=True)
        self._max = 1

        # --- profil
        if not self.core.profile_names():
            self.core.save_profile("Varsayılan", dict(DEFAULTS))
        self.profile = self.core.last_profile() or self.core.profile_names()[0]

        # --- düzen
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=26, pady=22)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.pages = {
            "home": HomePage(self.content, self),
            "mods": BrowsePage(self.content, self, "mod"),
            "packs": BrowsePage(self.content, self, "resourcepack"),
            "profiles": ProfilesPage(self.content, self),
            "settings": SettingsPage(self.content, self),
        }
        for pg in self.pages.values():
            pg.grid(row=0, column=0, sticky="nsew")

        self.select_profile(self.profile, save=False)
        self.show_page("home")
        self.refresh_account_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.auto_login, daemon=True).start()

    # ---------------------------------------------------------- yan menü
    def build_sidebar(self):
        sb = ctk.CTkFrame(self, width=230, fg_color=SIDEBAR, corner_radius=0)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(8, weight=1)

        logo = ctk.CTkFrame(sb, fg_color="transparent")
        logo.grid(row=0, column=0, sticky="w", padx=22, pady=(26, 22))
        ctk.CTkLabel(logo, text="◆", font=font(26, "bold"),
                     text_color=ACCENT).pack(side="left")
        ctk.CTkLabel(logo, text="  BASİT", font=font(20, "bold"),
                     text_color=TEXT).pack(side="left")

        self.nav = {}
        items = [("home", "▶   Oyna"), ("mods", "⬢   Modlar"),
                 ("packs", "▣   Resourcepack"), ("profiles", "☰   Profiller"),
                 ("settings", "⚙   Ayarlar")]
        for i, (key, label) in enumerate(items, start=1):
            b = ctk.CTkButton(sb, text=label, anchor="w", height=42,
                              corner_radius=10, font=font(14, "bold"),
                              fg_color="transparent", hover_color=CARD,
                              text_color=MUTED,
                              command=lambda k=key: self.show_page(k))
            b.grid(row=i, column=0, sticky="ew", padx=14, pady=2)
            self.nav[key] = b

        # hesap kartı (altta)
        acc = ctk.CTkFrame(sb, fg_color=CARD, corner_radius=14)
        acc.grid(row=9, column=0, sticky="ew", padx=14, pady=16)
        row = ctk.CTkFrame(acc, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(12, 6))
        self.avatar = ctk.CTkLabel(row, text="S", width=40, height=40,
                                   corner_radius=10, fg_color=ACCENT,
                                   font=font(18, "bold"), text_color="white")
        self.avatar.pack(side="left")
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", padx=10)
        self.acc_name = ctk.CTkLabel(col, text="Steve", font=font(14, "bold"),
                                     text_color=TEXT, anchor="w")
        self.acc_name.pack(anchor="w")
        self.acc_mode = ctk.CTkLabel(col, text="Çevrimdışı", font=font(11),
                                     text_color=MUTED, anchor="w")
        self.acc_mode.pack(anchor="w")
        self.acc_btn = ctk.CTkButton(acc, text="Microsoft ile giriş", height=34,
                                     corner_radius=8, font=font(12, "bold"),
                                     fg_color=CARD_HI, hover_color=ACCENT,
                                     command=self.ms_toggle)
        self.acc_btn.pack(fill="x", padx=12, pady=(0, 12))

    def show_page(self, key):
        for k, b in self.nav.items():
            on = (k == key)
            b.configure(fg_color=CARD_HI if on else "transparent",
                        text_color=TEXT if on else MUTED)
        pg = self.pages[key]
        pg.tkraise()
        if hasattr(pg, "on_show"):
            pg.on_show()

    # ----------------------------------------------------------- yardımcılar
    def ui(self, fn):
        """Thread'den güvenle arayüzü güncelle."""
        try:
            self.after(0, fn)
        except Exception:
            pass

    def set_status(self, text):
        self.ui(lambda: self.pages["home"].status.configure(text=text))

    def set_progress(self, value=None, maximum=None):
        if maximum is not None:
            self._max = max(maximum, 1)

        def upd():
            if value is not None:
                self.pages["home"].bar.set(min(value / self._max, 1))
        self.ui(upd)

    # ---------------------------------------------------------- ayar / profil
    def settings(self):
        return {"profile": self.profile,
                "name": self.v_name.get().strip(),
                "version": self.v_version.get(),
                "loader": self.v_loader.get(),
                "ram": int(round(self.v_ram.get())),
                "snapshots": self.v_snap.get(),
                "perf": self.v_perf.get(),
                "perf_mods": self.v_permods.get()}

    def save_current(self):
        s = self.settings()
        s.pop("profile")
        self.core.save_profile(self.profile, s)

    def apply_settings(self, p):
        self.v_name.set(p["name"])
        self.v_loader.set(p["loader"])
        self.v_ram.set(float(p["ram"]))
        self.v_snap.set(p["snapshots"])
        self.v_perf.set(p["perf"])
        self.v_permods.set(p["perf_mods"])
        self.v_version.set(p["version"])
        self.load_versions(keep=p["version"])

    def select_profile(self, name, save=True):
        p = self.core.get_profile(name)
        if not p:
            return
        self.profile = name
        self.apply_settings(p)
        self.pages["home"].profile_lbl.configure(text=name)
        if save:
            self.save_current()
        if "profiles" in self.pages:
            self.pages["profiles"].refresh()

    def load_versions(self, keep=None):
        keep = keep or self.v_version.get()

        def work():
            try:
                vers = self.core.get_versions(self.v_snap.get())
            except Exception:
                vers = []
                self.set_status("Sürüm listesi alınamadı. İnternet var mı?")
            self.ui(lambda: self._set_versions(vers, keep))
        threading.Thread(target=work, daemon=True).start()

    def _set_versions(self, vers, keep):
        box = self.pages["home"].version_box
        box.configure(values=vers or [""])
        if keep in vers:
            self.v_version.set(keep)
        elif vers:
            self.v_version.set(vers[0])

    # ----------------------------------------------------------- Microsoft
    def ms_toggle(self):
        if self.core.session:
            self.core.logout()
            self.refresh_account_ui()
            self.set_status("Çıkış yapıldı")
            return
        try:
            url, state, verifier = self.core.begin_login()
        except Exception as e:
            messagebox.showinfo("Client ID gerekli", str(e))
            return
        self.set_status("Tarayıcıda giriş yap, sonra launcher'a dön...")
        webbrowser.open(url)

        def work():
            try:
                redirected = self.core.wait_for_redirect()
                if not redirected:
                    self.set_status("Giriş zaman aşımına uğradı")
                    return
                self.set_status("Giriş yapılıyor...")
                self.core.finish_login(redirected, state, verifier)
                self.set_status("Giriş başarılı")
            except Exception as e:
                msg = friendly(e)
                self.ui(lambda: messagebox.showerror("Giriş hatası", msg))
                self.set_status("Giriş başarısız")
            self.ui(self.refresh_account_ui)
        threading.Thread(target=work, daemon=True).start()

    def auto_login(self):
        if self.core.refresh_saved_login():
            self.set_status("Oturum yenilendi")
            self.ui(self.refresh_account_ui)

    def refresh_account_ui(self):
        s = self.core.session
        name = s["name"] if s else (self.v_name.get().strip() or "Steve")
        self.avatar.configure(text=(name[:1] or "S").upper(),
                              fg_color=GREEN if s else ACCENT)
        self.acc_name.configure(text=name)
        self.acc_mode.configure(text="Microsoft hesabı" if s else "Çevrimdışı")
        self.acc_btn.configure(text="Çıkış yap" if s else "Microsoft ile giriş")
        self.pages["settings"].name_entry.configure(
            state="disabled" if s else "normal")

    # ---------------------------------------------------------------- oyun
    def play(self):
        p = self.settings()
        if not p["version"]:
            messagebox.showwarning("Eksik", "Bir sürüm seçmelisin.")
            return
        if not self.core.session and not p["name"]:
            messagebox.showwarning("Eksik", "Ayarlardan oyuncu adı yazmalısın.")
            return
        self.save_current()
        home = self.pages["home"]
        home.play_btn.configure(state="disabled")
        home.stop_btn.configure(state="normal")
        home.bar.set(0)
        threading.Thread(target=self.run_game, args=(p,), daemon=True).start()

    def run_game(self, p):
        try:
            cb = self.core.make_callback(
                self.set_status,
                lambda v: self.set_progress(value=v),
                lambda m: self.set_progress(maximum=m))
            self.set_status(f"{p['loader']} {p['version']} hazırlanıyor...")
            self.core.launch(p, cb, on_started=lambda: self.set_status(
                "Oyun açık (kapatmak için DURDUR)"))
            self.set_status("Oyun kapandı")
        except Cancelled:
            self.set_status("İptal edildi")
        except Exception as e:
            if self.core.cancel:
                self.set_status("İptal edildi")
            else:
                msg = friendly(e)
                self.ui(lambda: messagebox.showerror("Hata", msg))
                self.set_status("Hata oluştu")
        finally:
            def reset():
                home = self.pages["home"]
                home.play_btn.configure(state="normal")
                home.stop_btn.configure(state="disabled")
                home.bar.set(0)
            self.ui(reset)

    def on_close(self):
        self.core.stop()
        self.destroy()


# ============================================================ SAYFALAR
def page_title(parent, title, sub=""):
    box = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkLabel(box, text=title, font=font(28, "bold"),
                 text_color=TEXT).pack(anchor="w")
    if sub:
        ctk.CTkLabel(box, text=sub, font=font(13), text_color=MUTED).pack(anchor="w")
    return box


class HomePage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)

        page_title(self, "Oyna", "Profilini seç, sürümü ayarla, başlat.").grid(
            row=0, column=0, sticky="w", pady=(0, 18))

        hero = ctk.CTkFrame(self, fg_color=HERO, corner_radius=20)
        hero.grid(row=1, column=0, sticky="ew")
        hero.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(hero, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=28, pady=(26, 6))
        ctk.CTkLabel(top, text="AKTİF PROFİL", font=font(11, "bold"),
                     text_color=ACCENT).pack(anchor="w")
        self.profile_lbl = ctk.CTkLabel(top, text=app.profile,
                                        font=font(34, "bold"), text_color=TEXT)
        self.profile_lbl.pack(anchor="w")

        opts = ctk.CTkFrame(hero, fg_color="transparent")
        opts.grid(row=1, column=0, sticky="ew", padx=28, pady=14)
        opts.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(opts, text="Mod yükleyici", font=font(12),
                     text_color=MUTED).grid(row=0, column=0, sticky="w")
        ctk.CTkSegmentedButton(
            opts, values=list(LOADERS), variable=app.v_loader, height=36,
            font=font(13, "bold"), fg_color=CARD, unselected_color=CARD,
            selected_color=ACCENT, selected_hover_color=ACCENT_HI,
            unselected_hover_color=CARD_HI).grid(row=1, column=0, sticky="w",
                                                 pady=(4, 12))

        ctk.CTkLabel(opts, text="Sürüm", font=font(12),
                     text_color=MUTED).grid(row=0, column=1, sticky="w", padx=(24, 0))
        self.version_box = ctk.CTkComboBox(
            opts, variable=app.v_version, values=[""], state="readonly",
            width=200, height=36, corner_radius=8, font=font(13),
            fg_color=CARD, border_color=CARD_HI, button_color=CARD_HI,
            button_hover_color=ACCENT, dropdown_fg_color=CARD)
        self.version_box.grid(row=1, column=1, sticky="w", padx=(24, 0), pady=(4, 12))

        ctk.CTkSwitch(opts, text="Snapshot sürümlerini göster",
                      variable=app.v_snap, font=font(12), text_color=MUTED,
                      progress_color=ACCENT,
                      command=lambda: app.load_versions()).grid(
            row=2, column=0, columnspan=2, sticky="w")

        # durum + butonlar
        bottom = ctk.CTkFrame(hero, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=28, pady=(8, 26))
        bottom.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bottom, text="Hazır", font=font(13),
                                   text_color=MUTED, anchor="w")
        self.status.grid(row=0, column=0, sticky="w")
        self.bar = ctk.CTkProgressBar(bottom, height=8, corner_radius=4,
                                      fg_color=CARD, progress_color=ACCENT)
        self.bar.set(0)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(8, 16))

        btns = ctk.CTkFrame(bottom, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="w")
        self.play_btn = ctk.CTkButton(
            btns, text="▶   OYNA", width=220, height=54, corner_radius=14,
            font=font(18, "bold"), fg_color=GREEN, hover_color=GREEN_HI,
            command=app.play)
        self.play_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(
            btns, text="■  DURDUR", width=140, height=54, corner_radius=14,
            font=font(14, "bold"), fg_color=CARD, hover_color=RED,
            state="disabled", command=app.core.stop)
        self.stop_btn.pack(side="left", padx=12)
        ctk.CTkButton(
            btns, text="📁  Profil klasörü", width=150, height=54,
            corner_radius=14, font=font(13), fg_color=CARD, hover_color=CARD_HI,
            command=self.open_folder).pack(side="left")

    def open_folder(self):
        try:
            self.app.core.open_folder(self.app.core.instance_dir(self.app.profile))
        except Exception as e:
            messagebox.showerror("Hata", str(e))


class BrowsePage(ctk.CTkFrame):
    """Modrinth'ten mod / resourcepack ara ve indir."""

    def __init__(self, master, app, kind):
        super().__init__(master, fg_color="transparent")
        self.app, self.kind = app, kind
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._first = True

        what = "Modlar" if kind == "mod" else "Resourcepack"
        page_title(self, what, "Modrinth'ten ara, tek tıkla aktif profile indir.").grid(
            row=0, column=0, sticky="w", pady=(0, 14))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        bar.grid_columnconfigure(0, weight=1)
        self.q = ctk.CTkEntry(bar, placeholder_text="Ara...  (örn. sodium, shaders)",
                              height=42, corner_radius=10, font=font(14),
                              fg_color=CARD, border_color=CARD_HI)
        self.q.grid(row=0, column=0, sticky="ew")
        self.q.bind("<Return>", lambda _e: self.search())
        ctk.CTkButton(bar, text="Ara", width=90, height=42, corner_radius=10,
                      font=font(14, "bold"), fg_color=ACCENT,
                      hover_color=ACCENT_HI, command=self.search).grid(
            row=0, column=1, padx=(10, 0))

        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.grid(row=2, column=0, sticky="nsew")
        self.list.grid_columnconfigure(0, weight=1)
        self.msg = ctk.CTkLabel(self, text="", font=font(12), text_color=MUTED)
        self.msg.grid(row=3, column=0, sticky="w", pady=(6, 0))

    def on_show(self):
        if self._first:
            self._first = False
            self.search()

    def say(self, text):
        self.app.ui(lambda: self.msg.configure(text=text))

    def search(self):
        s = self.app.settings()
        if not s["version"]:
            self.say("Önce Oyna sayfasından bir sürüm seç.")
            return
        if self.kind == "mod" and s["loader"] == "vanilla":
            self.say("Vanilla'da mod çalışmaz. Mod yükleyiciyi Fabric veya Forge yap.")
            return
        self.say("Aranıyor...")
        query = self.q.get().strip()

        def work():
            try:
                hits = self.app.core.modrinth_search(
                    query, self.kind, s["version"], s["loader"])
            except Exception as e:
                self.say(f"Arama başarısız: {e}")
                return
            self.app.ui(lambda: self.show(hits))
        threading.Thread(target=work, daemon=True).start()

    def show(self, hits):
        for w in self.list.winfo_children():
            w.destroy()
        for i, h in enumerate(hits):
            self.card(i, h)
        self.say(f"{len(hits)} sonuç" if hits else "Sonuç yok.")

    def card(self, i, h):
        c = ctk.CTkFrame(self.list, fg_color=CARD, corner_radius=14)
        c.grid(row=i, column=0, sticky="ew", pady=5, padx=(0, 6))
        c.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(c, fg_color="transparent")
        left.grid(row=0, column=0, sticky="ew", padx=16, pady=12)
        ctk.CTkLabel(left, text=h["title"], font=font(15, "bold"),
                     text_color=TEXT, anchor="w").pack(anchor="w")
        desc = (h.get("description") or "")[:140]
        ctk.CTkLabel(left, text=desc, font=font(12), text_color=MUTED,
                     wraplength=520, justify="left", anchor="w").pack(anchor="w")
        ctk.CTkLabel(left, text=f"{h['author']}  •  {h['downloads']:,} indirme",
                     font=font(11), text_color=ACCENT, anchor="w").pack(anchor="w",
                                                                         pady=(4, 0))
        btn = ctk.CTkButton(c, text="İndir", width=96, height=38,
                            corner_radius=10, font=font(13, "bold"),
                            fg_color=ACCENT, hover_color=ACCENT_HI)
        btn.configure(command=lambda: self.download(h, btn))
        btn.grid(row=0, column=1, padx=16)

    def download(self, h, btn):
        s = self.app.settings()
        game_dir = self.app.core.instance_dir(s["profile"])
        btn.configure(state="disabled", text="İniyor...")

        def work():
            try:
                done = self.app.core.modrinth_install(
                    h["slug"], self.kind, s["version"], s["loader"], game_dir)
                text = "Kuruldu ✓" if done else "Zaten kurulu"
                self.say(("Kuruldu: " + ", ".join(done)) if done
                         else "Zaten kurulu, bir şey eklenmedi.")
                self.app.ui(lambda: btn.configure(text=text, fg_color=GREEN))
            except Exception as e:
                self.say(f"İndirilemedi: {e}")
                self.app.ui(lambda: btn.configure(state="normal", text="İndir"))
        threading.Thread(target=work, daemon=True).start()


class ProfilesPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        page_title(self, "Profiller", "Her profilin kendi mods, saves ve config klasörü var.").grid(
            row=0, column=0, sticky="w", pady=(0, 14))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        bar.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(bar, placeholder_text="Yeni profil / modpaketi adı",
                                  height=42, corner_radius=10, font=font(14),
                                  fg_color=CARD, border_color=CARD_HI)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda _e: self.create())
        ctk.CTkButton(bar, text="+  Oluştur", width=110, height=42,
                      corner_radius=10, font=font(14, "bold"), fg_color=ACCENT,
                      hover_color=ACCENT_HI, command=self.create).grid(
            row=0, column=1, padx=(10, 0))

        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.grid(row=2, column=0, sticky="nsew")
        self.list.grid_columnconfigure(0, weight=1)

    def on_show(self):
        self.refresh()

    def create(self):
        name = self.entry.get().strip()
        try:
            self.app.core.create_modpack(name, self.app.settings())
        except ValueError as e:
            messagebox.showwarning("Olmadı", str(e))
            return
        self.entry.delete(0, "end")
        self.app.select_profile(name)

    def refresh(self):
        for w in self.list.winfo_children():
            w.destroy()
        for i, name in enumerate(self.app.core.profile_names()):
            p = self.app.core.get_profile(name)
            active = (name == self.app.profile)
            c = ctk.CTkFrame(self.list, fg_color=CARD_HI if active else CARD,
                             corner_radius=14,
                             border_width=2 if active else 0, border_color=ACCENT)
            c.grid(row=i, column=0, sticky="ew", pady=5, padx=(0, 6))
            c.grid_columnconfigure(0, weight=1)
            left = ctk.CTkFrame(c, fg_color="transparent")
            left.grid(row=0, column=0, sticky="w", padx=16, pady=12)
            ctk.CTkLabel(left, text=name + ("   ● aktif" if active else ""),
                         font=font(15, "bold"), text_color=TEXT).pack(anchor="w")
            ver = p["version"] or "sürüm seçilmedi"
            ctk.CTkLabel(left, text=f"{p['loader']}  •  {ver}  •  {p['ram']} GB",
                         font=font(12), text_color=MUTED).pack(anchor="w")
            btns = ctk.CTkFrame(c, fg_color="transparent")
            btns.grid(row=0, column=1, padx=12)
            if not active:
                ctk.CTkButton(btns, text="Seç", width=70, height=34,
                              corner_radius=8, fg_color=ACCENT,
                              hover_color=ACCENT_HI,
                              command=lambda n=name: self.app.select_profile(n)
                              ).pack(side="left", padx=3)
            ctk.CTkButton(btns, text="Klasör", width=70, height=34,
                          corner_radius=8, fg_color=CARD, hover_color=CARD_HI,
                          command=lambda n=name: self.open(n)).pack(side="left", padx=3)
            ctk.CTkButton(btns, text="Sil", width=60, height=34, corner_radius=8,
                          fg_color=CARD, hover_color=RED,
                          command=lambda n=name: self.delete(n)).pack(side="left", padx=3)

    def open(self, name):
        try:
            self.app.core.open_folder(self.app.core.instance_dir(name))
        except Exception as e:
            messagebox.showerror("Hata", str(e))

    def delete(self, name):
        if not messagebox.askyesno(
                "Profili sil", f"'{name}' profili silinsin mi?\n"
                "(Klasördeki dünya/mod dosyaları silinmez.)"):
            return
        core = self.app.core
        core.delete_profile(name)
        if not core.profile_names():
            core.save_profile("Varsayılan", dict(DEFAULTS))
        if name == self.app.profile:
            self.app.select_profile(core.profile_names()[0])
        else:
            self.refresh()


class SettingsPage(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)

        page_title(self, "Ayarlar", "Aktif profil için geçerli ayarlar.").grid(
            row=0, column=0, sticky="w", pady=(0, 14))

        # oyuncu adı
        c1 = self.card(1)
        ctk.CTkLabel(c1, text="Oyuncu adı (çevrimdışı mod)", font=font(14, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=20, pady=(16, 6))
        self.name_entry = ctk.CTkEntry(c1, textvariable=app.v_name, height=40,
                                       corner_radius=10, font=font(14),
                                       fg_color=BG, border_color=CARD_HI)
        self.name_entry.pack(fill="x", padx=20, pady=(0, 16))
        self.name_entry.bind("<KeyRelease>", lambda _e: self.app.refresh_account_ui())

        # RAM
        c2 = self.card(2)
        head = ctk.CTkFrame(c2, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 0))
        ctk.CTkLabel(head, text="RAM", font=font(14, "bold"),
                     text_color=TEXT).pack(side="left")
        self.ram_lbl = ctk.CTkLabel(head, text="4 GB", font=font(14, "bold"),
                                    text_color=ACCENT)
        self.ram_lbl.pack(side="right")
        ctk.CTkSlider(c2, from_=1, to=16, number_of_steps=15,
                      variable=app.v_ram, progress_color=ACCENT,
                      button_color=ACCENT, button_hover_color=ACCENT_HI,
                      command=self.on_ram).pack(fill="x", padx=20, pady=(10, 18))
        self.on_ram()

        # performans
        c3 = self.card(3)
        ctk.CTkLabel(c3, text="Performans", font=font(14, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=20, pady=(16, 8))
        ctk.CTkSwitch(c3, text="Performans JVM ayarları (takılmaları azaltır)",
                      variable=app.v_perf, font=font(13), text_color=MUTED,
                      progress_color=ACCENT).pack(anchor="w", padx=20, pady=4)
        ctk.CTkSwitch(c3, text="FPS modlarını otomatik ekle (sadece Fabric)",
                      variable=app.v_permods, font=font(13), text_color=MUTED,
                      progress_color=ACCENT).pack(anchor="w", padx=20, pady=(4, 16))

        ctk.CTkButton(self, text="Kaydet", width=140, height=42, corner_radius=10,
                      font=font(14, "bold"), fg_color=ACCENT, hover_color=ACCENT_HI,
                      command=self.save).grid(row=4, column=0, sticky="w", pady=14)

    def card(self, row):
        f = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        f.grid(row=row, column=0, sticky="ew", pady=6)
        return f

    def on_ram(self, _v=None):
        self.ram_lbl.configure(text=f"{int(round(self.app.v_ram.get()))} GB")

    def save(self):
        self.app.save_current()
        self.app.set_status(f"Ayarlar kaydedildi: {self.app.profile}")
        self.app.show_page("home")


if __name__ == "__main__":
    App().mainloop()
