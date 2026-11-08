import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

import dashconv as dc

APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
WM_DROPFILES = 0x0233
GWL_WNDPROC = -4


def enable_drop(root, callback):
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetParent.restype = wintypes.HWND
        shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        shell32.DragFinish.argtypes = [wintypes.HANDLE]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        state = {}

        def proc(h, msg, wp, lp):
            if msg == WM_DROPFILES:
                n = shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)
                paths = []
                for k in range(n):
                    buf = ctypes.create_unicode_buffer(32768)
                    shell32.DragQueryFileW(wp, k, buf, 32768)
                    paths.append(buf.value)
                shell32.DragFinish(wp)
                root.after(0, callback, paths)
                return 0
            return user32.CallWindowProcW(state["old"], h, msg, wp, lp)

        state["cb"] = WNDPROC(proc)
        state["old"] = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, ctypes.cast(state["cb"], ctypes.c_void_p))
        shell32.DragAcceptFiles(hwnd, True)
        root._drop_state = state
        return True
    except Exception:
        return False


class App:
    def __init__(self, root):
        self.root = root
        root.title("TV 3.0 DASH Converter")
        root.geometry("640x600")
        root.minsize(560, 520)
        self.q = queue.Queue()
        self.recs = []
        self.job = None
        self.busy = False
        self.ffmpeg = None
        self.t0 = 0.0

        pad = {"padx": 10, "pady": 4}
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.ff_label = ttk.Label(top, text="FFmpeg: procurando...")
        self.ff_label.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Trocar FFmpeg...", command=self.pick_ffmpeg).pack(side="right")

        self.drop = tk.Label(root, text="Solte aqui a pasta da gravacao\n(ou clique para escolher)",
                             relief="groove", bd=2, height=4, bg="#f2f2f2", cursor="hand2")
        self.drop.pack(fill="x", **pad)
        self.drop.bind("<Button-1>", lambda e: self.pick_folder())

        self.listbox = tk.Listbox(root, selectmode="extended", height=4, exportselection=False)
        self.listbox.pack(fill="x", **pad)

        opt = ttk.LabelFrame(root, text="Opcoes")
        opt.pack(fill="x", **pad)
        self.crf = tk.IntVar(value=18)
        self.preset = tk.StringVar(value="6")
        self.lp = tk.StringVar(value="12")
        self.audio = tk.StringVar(value=list(dc.AUDIO_MODES)[0])
        ttk.Label(opt, text="Qualidade (CRF, menor = melhor):").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Spinbox(opt, from_=10, to=40, textvariable=self.crf, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(opt, text="Velocidade (preset, maior = mais rapido):").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(opt, textvariable=self.preset, values=[str(i) for i in range(2, 11)], width=5,
                     state="readonly").grid(row=1, column=1, sticky="w")
        ttk.Label(opt, text="Threads do encoder (menos = menos memoria):").grid(row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(opt, textvariable=self.lp, values=["4", "6", "8", "12", "16", "24"], width=5,
                     state="readonly").grid(row=2, column=1, sticky="w")
        ttk.Label(opt, text="Audio:").grid(row=3, column=0, sticky="w", padx=6, pady=3)
        ttk.Combobox(opt, textvariable=self.audio, values=list(dc.AUDIO_MODES), width=30,
                     state="readonly").grid(row=3, column=1, sticky="w")

        out = ttk.Frame(root)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="Pasta de saida:").pack(side="left")
        self.outdir = tk.StringVar(value="")
        ttk.Entry(out, textvariable=self.outdir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Escolher...", command=self.pick_outdir).pack(side="left")
        ttk.Label(root, text="(vazio = ao lado da pasta da gravacao)", foreground="#666").pack(anchor="w", padx=10)

        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Converter", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancelar", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        self.bar = ttk.Progressbar(root, maximum=1000)
        self.bar.pack(fill="x", **pad)
        self.status = ttk.Label(root, text="Pronto.")
        self.status.pack(anchor="w", padx=10)
        self.log = tk.Text(root, height=8, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, **pad)

        self.dnd = enable_drop(root, self.on_drop)
        if not self.dnd:
            self.drop.config(text="Clique aqui para escolher a pasta da gravacao")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        threading.Thread(target=self.detect_ffmpeg, args=(None,), daemon=True).start()

    def say(self, msg):
        self.q.put(("log", msg))

    def poll(self):
        try:
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
                elif k == "recs":
                    self.recs = a
                    self.listbox.delete(0, "end")
                    for r in a:
                        self.listbox.insert("end", r.label())
                    self.listbox.select_set(0, "end")
                    self.drop.config(text="%d gravacao(oes) encontrada(s). Solte outra pasta para trocar." % len(a)
                                     if a else "Nenhuma gravacao DASH (.m4s) encontrada nessa pasta.")
                elif k == "done":
                    self.busy = False
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
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
        if self.busy:
            return
        p = filedialog.askdirectory(title="Pasta da gravacao")
        if p:
            self.scan(p)

    def on_drop(self, paths):
        if self.busy or not paths:
            return
        p = paths[0]
        self.scan(p if os.path.isdir(p) else os.path.dirname(p))

    def scan(self, folder):
        self.q.put(("status", "Analisando " + folder + " ..."))
        self.q.put(("recs", []))

        def work():
            try:
                recs = dc.find_recordings(folder)
                for r in recs:
                    self.say("Analisando %s ..." % r.name)
                    dc.analyze(r, self.say)
                self.q.put(("recs", recs))
                self.q.put(("status", "Pronto."))
            except Exception as e:
                self.say("Erro: %s" % e)
                self.q.put(("status", "Erro na analise."))

        threading.Thread(target=work, daemon=True).start()

    def start(self):
        sel = [self.recs[i] for i in self.listbox.curselection()]
        if not sel:
            messagebox.showinfo("TV 3.0 DASH Converter", "Solte uma pasta e selecione ao menos uma gravacao.")
            return
        if not self.ffmpeg:
            messagebox.showerror("TV 3.0 DASH Converter", "FFmpeg nao encontrado. Use 'Trocar FFmpeg...'.")
            return
        opts = {"crf": int(self.crf.get()), "preset": int(self.preset.get()), "lp": int(self.lp.get()),
                "audio": self.audio.get()}
        self.busy = True
        self.job = dc.Job()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        threading.Thread(target=self.work, args=(sel, opts, self.outdir.get().strip()), daemon=True).start()

    def cancel(self):
        if self.job:
            self.job.kill()
            self.say("Cancelando...")

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("TV 3.0 DASH Converter", "Uma conversao esta em andamento. Cancelar e sair?"):
                return
            self.job.kill()
        self.root.destroy()

    def work(self, recs, opts, outdir):
        for n, rec in enumerate(recs, 1):
            tag = "[%d/%d] %s" % (n, len(recs), rec.name)
            try:
                folder = outdir or os.path.dirname(rec.root)
                os.makedirs(folder, exist_ok=True)
                out = os.path.join(folder, rec.name + "_AV1_4K.mkv")
                k = 1
                while os.path.exists(out):
                    k += 1
                    out = os.path.join(folder, "%s_AV1_4K_%d.mkv" % (rec.name, k))
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

                note = dc.run_encode(cmd, rec.info["duration"], self.job, prog, out + ".log", self.ffmpeg, out)
                if note:
                    self.say("Aviso: " + note)
                try:
                    os.remove(out + ".log")
                except OSError:
                    pass
                self.say(tag + ": concluido -> " + out)
            except Exception as e:
                self.say("%s: %s" % (tag, e))
                self.q.put(("status", "Parado: %s" % str(e).splitlines()[0]))
                if self.job.cancel.is_set():
                    dc.cleanup_stage(rec)
                    break
            finally:
                dc.cleanup_stage(rec)
        else:
            self.q.put(("status", "Tudo concluido."))
            self.q.put(("bar", 1))
        self.q.put(("done", None))


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
