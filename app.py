import ctypes
import faulthandler
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

import dashconv as dc

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
WM_DROPFILES = 0x0233


def log_dir():
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TV30Converter")
    os.makedirs(d, exist_ok=True)
    return d


def setup_crash_log():
    try:
        path = os.path.join(log_dir(), "crash.log")
        f = open(path, "a", buffering=1, errors="replace")
        f.write("\n--- %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        faulthandler.enable(file=f, all_threads=True)

        def hook(et, ev, tb):
            f.write("".join(traceback.format_exception(et, ev, tb)))

        sys.excepthook = hook
        threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
        return f
    except Exception:
        return None


def enable_drop(root, drops):
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        comctl = ctypes.windll.comctl32
        LRESULT = ctypes.c_ssize_t
        SUBPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                                     ctypes.c_size_t, ctypes.c_size_t)
        comctl.SetWindowSubclass.restype = wintypes.BOOL
        comctl.SetWindowSubclass.argtypes = [wintypes.HWND, SUBPROC, ctypes.c_size_t, ctypes.c_size_t]
        comctl.DefSubclassProc.restype = LRESULT
        comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
        shell32.DragQueryFileW.restype = wintypes.UINT
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        root.update_idletasks()
        top = root.winfo_id()
        hwnds = {top, user32.GetParent(top) or top}

        def proc(h, msg, wp, lp, uid, ref):
            if msg == WM_DROPFILES:
                try:
                    paths = []
                    n = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                    for k in range(n):
                        size = shell32.DragQueryFileW(wp, k, None, 0) + 1
                        buf = ctypes.create_unicode_buffer(size)
                        shell32.DragQueryFileW(wp, k, buf, size)
                        paths.append(buf.value)
                    drops.put(paths)
                except Exception:
                    traceback.print_exc()
                finally:
                    try:
                        shell32.DragFinish(wp)
                    except Exception:
                        pass
                return 0
            return comctl.DefSubclassProc(h, msg, wp, lp)

        cb = SUBPROC(proc)
        for hwnd in hwnds:
            try:
                user32.ChangeWindowMessageFilterEx(hwnd, WM_DROPFILES, 1, None)
                user32.ChangeWindowMessageFilterEx(hwnd, 0x0049, 1, None)
            except Exception:
                pass
            if not comctl.SetWindowSubclass(hwnd, cb, 1, 0):
                return False
            shell32.DragAcceptFiles(hwnd, True)
        root._drop_cb = cb
        return True
    except Exception:
        traceback.print_exc()
        return False


def app_icon_path():
    base = getattr(sys, "_MEIPASS", APP_DIR)
    for p in (os.path.join(base, "icon.ico"), os.path.join(APP_DIR, "installer", "icon.ico")):
        if os.path.exists(p):
            return p
    return None


PENDING, ANALYZING, RUNNING, DONE, FAILED, CANCELED = "Pendente", "Analisando", "Convertendo", "Concluido", "Erro", "Cancelado"


class Item:
    _n = 0

    def __init__(self, rec, opts, outdir):
        Item._n += 1
        self.id = str(Item._n)
        self.rec = rec
        self.opts = opts
        self.outdir = outdir
        self.status = PENDING
        self.detail = ""
        self.out = None


def opts_text(o):
    ac = "sem audio" if dc.LAYOUTS[o["layout"]] is None else o["acodec"].split(" (")[0] + " " + o["layout"].split(" (")[0]
    v = dc.VIDEO_INFO[o["vcodec"]]["suffix"]
    return "%s / %s / %s / CRF %s" % (v, o["container"].upper(), ac, o["crf"])


class App:
    def __init__(self, root):
        self.root = root
        root.title("TV 3.0 DASH Converter")
        root.geometry("760x780")
        root.minsize(680, 680)
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("tv30.dash.converter.1")
        except Exception:
            pass
        ic = app_icon_path()
        if ic:
            try:
                root.iconbitmap(ic)
            except Exception:
                pass
        self.q = queue.Queue()
        self.drops = queue.Queue()
        self.items = []
        self.lock = threading.Lock()
        self.job = None
        self.running = False
        self.stop_flag = False
        self.ffmpeg = None
        self.t0 = 0.0

        pad = {"padx": 10, "pady": 4}
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.ff_label = ttk.Label(top, text="FFmpeg: procurando...")
        self.ff_label.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Trocar FFmpeg...", command=self.pick_ffmpeg).pack(side="right")

        self.drop = tk.Label(root, text="Solte aqui uma ou mais pastas de gravacao\n(ou clique para escolher)",
                             relief="groove", bd=2, height=3, bg="#f2f2f2", cursor="hand2")
        self.drop.pack(fill="x", **pad)
        self.drop.bind("<Button-1>", lambda e: self.pick_folder())

        qf = ttk.LabelFrame(root, text="Fila de processamento")
        qf.pack(fill="both", expand=True, **pad)
        tf = ttk.Frame(qf)
        tf.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree = ttk.Treeview(tf, columns=("rec", "cfg", "st"), show="headings", height=6, selectmode="extended")
        self.tree.heading("rec", text="Gravacao")
        self.tree.heading("cfg", text="Configuracao")
        self.tree.heading("st", text="Status")
        self.tree.column("rec", width=190)
        self.tree.column("cfg", width=300)
        self.tree.column("st", width=110)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        qb = ttk.Frame(qf)
        qb.pack(fill="x", padx=6, pady=(0, 4))
        for text, cmd in (("Subir", lambda: self.move(-1)), ("Descer", lambda: self.move(1)),
                          ("Remover", self.remove), ("Limpar concluidos", self.clear_done),
                          ("Aplicar opcoes aos selecionados", self.apply_opts)):
            ttk.Button(qb, text=text, command=cmd).pack(side="left", padx=(0, 6))

        opt = ttk.LabelFrame(root, text="Opcoes (valem para as gravacoes adicionadas a partir de agora)")
        opt.pack(fill="x", **pad)
        self.container = tk.StringVar(value=list(dc.CONTAINERS)[0])
        self.vcodec = tk.StringVar(value=list(dc.VIDEO_CODECS)[0])
        self.crf = tk.IntVar(value=18)
        self.preset = tk.StringVar(value="6")
        self.mem = tk.StringVar(value="Ilimitado")
        self.layout = tk.StringVar(value=list(dc.LAYOUTS)[0])
        self.acodec = tk.StringVar(value=list(dc.AUDIO_CODECS)[0])

        def lbl(text, r, c):
            ttk.Label(opt, text=text).grid(row=r, column=c, sticky="w", padx=6, pady=3)

        lbl("Conteiner:", 0, 0)
        self.cb_container = ttk.Combobox(opt, textvariable=self.container, values=list(dc.CONTAINERS), width=18,
                                         state="readonly")
        self.cb_container.grid(row=0, column=1, sticky="w")
        lbl("Codec de video:", 0, 2)
        self.cb_vcodec = ttk.Combobox(opt, textvariable=self.vcodec, values=list(dc.VIDEO_CODECS), width=22,
                                      state="readonly")
        self.cb_vcodec.grid(row=0, column=3, sticky="w")
        lbl("Qualidade (CRF, menor = melhor):", 1, 0)
        self.sp_crf = ttk.Spinbox(opt, from_=10, to=40, textvariable=self.crf, width=6)
        self.sp_crf.grid(row=1, column=1, sticky="w")
        lbl("Velocidade (preset):", 1, 2)
        self.cb_preset = ttk.Combobox(opt, textvariable=self.preset, width=10, state="readonly")
        self.cb_preset.grid(row=1, column=3, sticky="w")
        lbl("Memoria (so AV1):", 2, 0)
        self.cb_mem = ttk.Combobox(opt, textvariable=self.mem, values=list(dc.MEM_PRESETS), width=12,
                                   state="readonly")
        self.cb_mem.grid(row=2, column=1, sticky="w")
        lbl("Canais de audio:", 3, 0)
        self.cb_layout = ttk.Combobox(opt, textvariable=self.layout, values=list(dc.LAYOUTS), width=18,
                                      state="readonly")
        self.cb_layout.grid(row=3, column=1, sticky="w")
        lbl("Codec de audio:", 3, 2)
        self.cb_acodec = ttk.Combobox(opt, textvariable=self.acodec, width=26, state="readonly")
        self.cb_acodec.grid(row=3, column=3, sticky="w")
        self.hint = ttk.Label(opt, text="", foreground="#666")
        self.hint.grid(row=4, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))
        self.cb_container.bind("<<ComboboxSelected>>", lambda e: self.on_container())
        self.cb_vcodec.bind("<<ComboboxSelected>>", lambda e: self.on_vcodec())
        self.cb_layout.bind("<<ComboboxSelected>>", lambda e: self.refresh_audio())
        self.on_vcodec()

        out = ttk.Frame(root)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="Pasta de saida:").pack(side="left")
        self.outdir = tk.StringVar(value="")
        ttk.Entry(out, textvariable=self.outdir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Escolher...", command=self.pick_outdir).pack(side="left")
        ttk.Label(root, text="(vazio = ao lado da pasta da gravacao)", foreground="#666").pack(anchor="w", padx=10)

        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Iniciar fila", command=self.start)
        self.start_btn.pack(side="left")
        self.skip_btn = ttk.Button(btns, text="Cancelar atual", command=self.cancel_current, state="disabled")
        self.skip_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(btns, text="Parar fila", command=self.stop_queue, state="disabled")
        self.stop_btn.pack(side="left")
        self.summary = ttk.Label(btns, text="")
        self.summary.pack(side="right")

        self.bar = ttk.Progressbar(root, maximum=1000)
        self.bar.pack(fill="x", **pad)
        self.status = ttk.Label(root, text="Pronto.")
        self.status.pack(anchor="w", padx=10)
        self.log = tk.Text(root, height=6, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, **pad)

        self.dnd = enable_drop(root, self.drops)
        if not self.dnd:
            self.drop.config(text="Clique aqui para escolher a pasta da gravacao")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        threading.Thread(target=self.detect_ffmpeg, args=(None,), daemon=True).start()

    def say(self, msg):
        self.q.put(("log", msg))

    def on_container(self):
        ext = dc.CONTAINERS[self.container.get()]
        allowed = dc.CONTAINER_INFO[ext]["video"]
        cur = dc.VIDEO_CODECS[self.vcodec.get()]
        if cur not in allowed:
            name = [k for k, v in dc.VIDEO_CODECS.items() if v == allowed[0]][0]
            self.vcodec.set(name)
            self.on_vcodec(keep_container=True)
        self.refresh_audio()

    def on_vcodec(self, keep_container=False):
        vid = dc.VIDEO_CODECS[self.vcodec.get()]
        info = dc.VIDEO_INFO[vid]
        self.crf.set(info["crf"])
        self.sp_crf.config(from_=info["crf_range"][0], to=info["crf_range"][1])
        self.cb_preset.config(values=info["presets"])
        self.preset.set(info["preset"])
        self.cb_mem.config(state="readonly" if vid == "av1" else "disabled")
        ext = dc.CONTAINERS[self.container.get()]
        if not keep_container and vid not in dc.CONTAINER_INFO[ext]["video"]:
            self.container.set(list(dc.CONTAINERS)[0])
        notes = {"av1": "", "hevc": "HEVC: boa compatibilidade com TVs e players, HDR10 preservado.",
                 "h264": "H.264 nao e padrao para HDR10: a maioria dos players vai mostrar cores lavadas."}
        self.hint.config(text=notes[vid])
        self.refresh_audio()

    def refresh_audio(self):
        ext = dc.CONTAINERS[self.container.get()]
        layout = dc.LAYOUTS[self.layout.get()]
        names = [k for k, v in dc.AUDIO_CODECS.items()
                 if v[0] in dc.CONTAINER_INFO[ext]["audio"] and (layout is None or layout in v[2])]
        self.cb_acodec.config(values=names, state="disabled" if layout is None else "readonly")
        if self.acodec.get() not in names and names:
            self.acodec.set(names[0])

    def current_opts(self):
        return {"container": dc.CONTAINERS[self.container.get()], "vcodec": dc.VIDEO_CODECS[self.vcodec.get()],
                "crf": int(self.crf.get()), "preset": self.preset.get(), "mem": self.mem.get(),
                "layout": self.layout.get(), "acodec": self.acodec.get()}

    def refresh_row(self, it):
        vals = (it.rec.name, opts_text(it.opts), it.status if not it.detail else "%s %s" % (it.status, it.detail))
        if self.tree.exists(it.id):
            self.tree.item(it.id, values=vals)
        else:
            self.tree.insert("", "end", iid=it.id, values=vals)
        with self.lock:
            done = sum(1 for x in self.items if x.status in (DONE, FAILED, CANCELED))
            self.summary.config(text="%d de %d processados" % (done, len(self.items)) if self.items else "")

    def poll(self):
        try:
            while True:
                try:
                    paths = self.drops.get_nowait()
                except queue.Empty:
                    break
                self.on_drop(paths)
            while True:
                k, a = self.q.get_nowait()
                if k == "log":
                    self.log.config(state="normal")
                    self.log.insert("end", a + "\n")
                    self.log.see("end")
                    self.log.config(state="disabled")
                elif k == "status":
                    self.status.config(text=a)
                elif k == "bar":
                    self.bar["value"] = a * 1000
                elif k == "ffmpeg":
                    self.ffmpeg, text = a
                    self.ff_label.config(text=text)
                elif k == "item":
                    self.refresh_row(a)
                elif k == "idle":
                    self.running = False
                    self.start_btn.config(state="normal")
                    self.skip_btn.config(state="disabled")
                    self.stop_btn.config(state="disabled")
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        self.root.after(100, self.poll)

    def detect_ffmpeg(self, custom):
        ff = dc.find_ffmpeg(custom, APP_DIR)
        if not ff:
            self.q.put(("ffmpeg", (None, "FFmpeg: nao encontrado (veja o README)")))
            return
        c = dc.check_ffmpeg(ff)
        miss = [n for n, ok in (("MPEG-H", c["mpegh"]), ("LCEVC", c["lcevc"]), ("SVT-AV1", c["svtav1"])) if not ok]
        text = "FFmpeg: " + ff + ("" if not miss else "  (FALTA: " + ", ".join(miss) + ")")
        self.q.put(("ffmpeg", (ff, text)))

    def pick_ffmpeg(self):
        p = filedialog.askopenfilename(title="ffmpeg.exe", filetypes=[("ffmpeg", "ffmpeg.exe")])
        if p:
            threading.Thread(target=self.detect_ffmpeg, args=(p,), daemon=True).start()

    def pick_outdir(self):
        p = filedialog.askdirectory(title="Pasta de saida")
        if p:
            self.outdir.set(p)

    def pick_folder(self):
        p = filedialog.askdirectory(title="Pasta da gravacao")
        if p:
            self.add_folders([p])

    def on_drop(self, paths):
        try:
            folders = []
            for p in paths:
                f = p if os.path.isdir(p) else os.path.dirname(p)
                if f and f not in folders:
                    folders.append(f)
            self.add_folders(folders)
        except Exception:
            traceback.print_exc()
            self.say("Erro ao processar o item solto: " + traceback.format_exc().splitlines()[-1])

    def add_folders(self, folders):
        err = dc.check_combo(self.current_opts())
        if err:
            messagebox.showerror("TV 3.0 DASH Converter", err)
            return
        opts = self.current_opts()
        outdir = self.outdir.get().strip()
        threading.Thread(target=self.scan, args=(folders, opts, outdir), daemon=True).start()

    def scan(self, folders, opts, outdir):
        for folder in folders:
            self.q.put(("status", "Analisando " + folder + " ..."))
            try:
                recs = dc.find_recordings(folder)
                if not recs:
                    self.say("Nenhuma gravacao DASH (.m4s) encontrada em " + folder)
                for r in recs:
                    with self.lock:
                        dup = any(x.rec.root == r.root and x.status in (PENDING, ANALYZING, RUNNING) for x in self.items)
                    if dup:
                        self.say("%s ja esta na fila." % r.name)
                        continue
                    it = Item(r, dict(opts), outdir)
                    it.status = ANALYZING
                    with self.lock:
                        self.items.append(it)
                    self.q.put(("item", it))
                    self.say("Analisando %s ..." % r.name)
                    try:
                        dc.analyze(r, self.say)
                        it.status = PENDING
                    except Exception as e:
                        it.status = FAILED
                        it.detail = "(analise)"
                        self.say("Erro em %s: %s" % (r.name, e))
                    self.q.put(("item", it))
            except Exception as e:
                self.say("Erro: %s" % e)
        self.q.put(("status", "Pronto."))

    def selected(self):
        ids = set(self.tree.selection())
        with self.lock:
            return [x for x in self.items if x.id in ids]

    def move(self, d):
        sel = self.selected()
        if not sel:
            return
        with self.lock:
            order = sorted(sel, key=lambda x: self.items.index(x), reverse=d > 0)
            for it in order:
                i = self.items.index(it)
                j = i + d
                if 0 <= j < len(self.items) and self.items[j] not in sel:
                    self.items[i], self.items[j] = self.items[j], self.items[i]
            for n, it in enumerate(self.items):
                self.tree.move(it.id, "", n)

    def remove(self):
        for it in self.selected():
            if it.status in (RUNNING, ANALYZING):
                self.say("Cancele '%s' antes de remover." % it.rec.name)
                continue
            with self.lock:
                self.items.remove(it)
            self.tree.delete(it.id)
        self.refresh_summary()

    def refresh_summary(self):
        with self.lock:
            done = sum(1 for x in self.items if x.status in (DONE, FAILED, CANCELED))
            self.summary.config(text="%d de %d processados" % (done, len(self.items)) if self.items else "")

    def clear_done(self):
        with self.lock:
            gone = [x for x in self.items if x.status in (DONE, FAILED, CANCELED)]
            self.items = [x for x in self.items if x not in gone]
        for it in gone:
            self.tree.delete(it.id)
        self.refresh_summary()

    def apply_opts(self):
        err = dc.check_combo(self.current_opts())
        if err:
            messagebox.showerror("TV 3.0 DASH Converter", err)
            return
        n = 0
        for it in self.selected():
            if it.status in (PENDING, FAILED, CANCELED):
                it.opts = self.current_opts()
                it.outdir = self.outdir.get().strip()
                if it.status != PENDING and it.detail != "(analise)":
                    it.status = PENDING
                self.refresh_row(it)
                n += 1
        self.say("Opcoes aplicadas a %d item(ns)." % n)

    def start(self):
        if not self.ffmpeg:
            messagebox.showerror("TV 3.0 DASH Converter", "FFmpeg nao encontrado. Use 'Trocar FFmpeg...'.")
            return
        with self.lock:
            has = any(x.status == PENDING for x in self.items)
        if not has:
            messagebox.showinfo("TV 3.0 DASH Converter", "Adicione ao menos uma gravacao pendente a fila.")
            return
        self.running = True
        self.stop_flag = False
        self.start_btn.config(state="disabled")
        self.skip_btn.config(state="normal")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self.work, daemon=True).start()

    def cancel_current(self):
        if self.job:
            self.job.kill()
            self.say("Cancelando o item atual...")

    def stop_queue(self):
        self.stop_flag = True
        self.cancel_current()

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("TV 3.0 DASH Converter", "A fila esta em andamento. Cancelar e sair?"):
                return
            self.stop_flag = True
            if self.job:
                self.job.kill()
        self.root.destroy()

    def next_pending(self):
        with self.lock:
            for x in self.items:
                if x.status == PENDING:
                    return x
        return None

    def work(self):
        while not self.stop_flag:
            it = self.next_pending()
            if it is None:
                break
            self.process(it)
        self.q.put(("status", "Fila parada." if self.stop_flag else "Fila concluida."))
        self.q.put(("bar", 0 if self.stop_flag else 1))
        self.q.put(("idle", None))

    def process(self, it):
        rec, opts = it.rec, it.opts
        with self.lock:
            pos = self.items.index(it) + 1
            total = len(self.items)
        tag = "[%d/%d] %s" % (pos, total, rec.name)
        self.job = dc.Job()
        it.status = RUNNING
        it.detail = ""
        self.q.put(("item", it))
        self.q.put(("bar", 0))
        try:
            folder = it.outdir or os.path.dirname(rec.root)
            os.makedirs(folder, exist_ok=True)
            ext = opts["container"]
            suffix = dc.VIDEO_INFO[opts["vcodec"]]["suffix"]
            out = os.path.join(folder, "%s_%s_4K.%s" % (rec.name, suffix, ext))
            k = 1
            while os.path.exists(out):
                k += 1
                out = os.path.join(folder, "%s_%s_4K_%d.%s" % (rec.name, suffix, k, ext))
            it.out = out
            self.q.put(("status", tag + ": preparando segmentos..."))
            self.say(tag + ": preparando segmentos (links, sem copiar quando possivel)")
            mpd, audio = dc.stage(rec, self.say, lambda p: self.q.put(("bar", p)), self.job.cancel)
            self.q.put(("bar", 0))
            cmd = dc.build_command(self.ffmpeg, rec, mpd, audio, out, opts, self.say)
            self.say("Saida: " + out)
            self.t0 = time.time()

            def prog(p, fps, speed, done):
                el = time.time() - self.t0
                eta = (el / p - el) if p > 0.002 else 0
                self.q.put(("bar", p))
                self.q.put(("status", "%s: %.1f%%  |  %s fps  |  %s  |  faltam ~%s" % (
                    tag, p * 100, fps, speed, dc.fmt_time(eta) if eta else "--")))

            mem = (dc.MEM_PRESETS.get(opts["mem"]) if opts["vcodec"] == "av1" else None) or (0, 0, 0)
            note = dc.run_encode(cmd, rec.info["duration"], self.job, prog, out + ".log", self.ffmpeg, out,
                                 mem_limit=mem[2])
            if note:
                self.say("Aviso: " + note)
            try:
                os.remove(out + ".log")
            except OSError:
                pass
            it.status = DONE
            self.say(tag + ": concluido -> " + out)
        except Exception as e:
            canceled = self.job.cancel.is_set()
            it.status = CANCELED if canceled else FAILED
            it.detail = "" if canceled else "(veja o log)"
            self.say("%s: %s" % (tag, e))
            self.q.put(("status", "%s: %s" % (tag, str(e).splitlines()[0])))
            if it.out:
                for p in (it.out, it.out + ".log"):
                    if canceled and os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
        finally:
            dc.cleanup_stage(rec)
            self.q.put(("item", it))


def main():
    setup_crash_log()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
