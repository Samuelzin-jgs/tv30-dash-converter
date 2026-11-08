import json
import os
import re
import shutil
import struct
import subprocess
import threading
import xml.etree.ElementTree as ET
from fractions import Fraction

MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
SEG_RE = re.compile(r"^(\w+)-(\d+)\.m4s$")
STAGE_NAME = "_tv30_stage"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_HDR = {
    "mastering": "G(0.1700,0.7970)B(0.1310,0.0460)R(0.7080,0.2920)WP(0.3127,0.3290)L(1000,0.0050)",
    "light": "1000,300",
}

AUDIO_MODES = {
    "5.1.2 - FLAC (sem perdas)": ("5.1.2", ["-c:a", "flac"]),
    "5.1 - FLAC (sem perdas)": ("5.1(side)", ["-c:a", "flac"]),
    "Estereo - FLAC (sem perdas)": ("stereo", ["-c:a", "flac"]),
    "Estereo - AAC 256k": ("stereo", ["-c:a", "aac", "-b:a", "256k"]),
    "Sem audio": (None, []),
}


class ConvError(Exception):
    pass


def _boxes(b, o=0, e=None):
    e = len(b) if e is None else e
    while o + 8 <= e:
        size, typ = struct.unpack(">I4s", b[o:o + 8])
        head = 8
        if size == 1:
            size = struct.unpack(">Q", b[o + 8:o + 16])[0]
            head = 16
        elif size == 0:
            size = e - o
        if size < head:
            return
        yield typ.decode("latin1"), o + head, min(o + size, e)
        o += size


def _child(b, s, e, name):
    for t, cs, ce in _boxes(b, s, e):
        if t == name:
            return cs, ce
    return None


def _path(b, names):
    s, e = 0, len(b)
    for n in names:
        r = _child(b, s, e, n)
        if r is None:
            return None
        s, e = r
    return s, e


def init_info(path):
    b = open(path, "rb").read()
    info = {"kind": None, "entry": None, "timescale": None}
    for t, s, e in _boxes(b):
        if t != "moov":
            continue
        for t2, s2, e2 in _boxes(b, s, e):
            if t2 != "trak":
                continue
            tb = b[s2:e2]
            hdlr = _path(tb, ["mdia", "hdlr"])
            mdhd = _path(tb, ["mdia", "mdhd"])
            stsd = _path(tb, ["mdia", "minf", "stbl", "stsd"])
            if hdlr:
                info["kind"] = {"vide": "video", "soun": "audio"}.get(tb[hdlr[0] + 8:hdlr[0] + 12].decode("latin1"))
            if mdhd:
                p = mdhd[0]
                p += 20 if tb[p] == 1 else 12
                info["timescale"] = struct.unpack(">I", tb[p:p + 4])[0]
            if stsd:
                info["entry"] = tb[stsd[0] + 12:stsd[0] + 16].decode("latin1")
    return info


def segment_info(path):
    with open(path, "rb") as f:
        b = f.read(262144)
    for t, s, e in _boxes(b):
        if t != "moof":
            continue
        for t2, s2, e2 in _boxes(b, s, e):
            if t2 != "traf":
                continue
            base = None
            dflt = None
            durs = None
            for t3, s3, e3 in _boxes(b, s2, e2):
                if t3 == "tfhd":
                    fl = struct.unpack(">I", b[s3:s3 + 4])[0] & 0xFFFFFF
                    p = s3 + 8
                    if fl & 1:
                        p += 8
                    if fl & 2:
                        p += 4
                    if fl & 8:
                        dflt = struct.unpack(">I", b[p:p + 4])[0]
                elif t3 == "tfdt":
                    if b[s3] == 1:
                        base = struct.unpack(">Q", b[s3 + 4:s3 + 12])[0]
                    else:
                        base = struct.unpack(">I", b[s3 + 4:s3 + 8])[0]
                elif t3 == "trun":
                    fl = struct.unpack(">I", b[s3:s3 + 4])[0] & 0xFFFFFF
                    n = struct.unpack(">I", b[s3 + 4:s3 + 8])[0]
                    p = s3 + 8
                    if fl & 1:
                        p += 4
                    if fl & 4:
                        p += 4
                    durs = 0
                    for _ in range(n):
                        d = dflt or 0
                        if fl & 0x100:
                            d = struct.unpack(">I", b[p:p + 4])[0]
                            p += 4
                        if fl & 0x200:
                            p += 4
                        if fl & 0x400:
                            p += 4
                        if fl & 0x800:
                            p += 4
                        durs += d
            if base is not None and durs is not None:
                return base, durs
    raise ConvError("Segmento invalido: " + path)


class Recording:
    def __init__(self, name, root, leaves):
        self.name = name
        self.root = root
        self.leaves = leaves
        self.info = None

    def label(self):
        if not self.info:
            return self.name
        i = self.info
        return "%s  |  %s  |  %d seg. video, %d seg. audio" % (
            self.name, fmt_time(i["duration"]), len(i["video_t"]), len(i["audio_t"]))


def fmt_time(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60)


def find_recordings(root):
    root = os.path.abspath(root)
    leaves = []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != STAGE_NAME]
        if any(SEG_RE.match(f) for f in files):
            leaves.append(dp)
    if not leaves:
        return []
    leaves.sort()
    if root in leaves or all(os.path.dirname(x) == root for x in leaves):
        return [Recording(os.path.basename(root), root, leaves)]
    groups = {}
    for x in leaves:
        groups.setdefault(os.path.dirname(x), []).append(x)
    return [Recording(os.path.basename(k), k, v) for k, v in sorted(groups.items())]


def analyze(rec, log=print):
    segs = {}
    inits = {}
    mpd = None
    for leaf in rec.leaves:
        for f in sorted(os.listdir(leaf)):
            p = os.path.join(leaf, f)
            m = SEG_RE.match(f)
            if m:
                d = segs.setdefault(m.group(1), {})
                t = int(m.group(2))
                if t not in d or os.path.getsize(p) > os.path.getsize(d[t]):
                    d[t] = p
            elif f.endswith("-init.mp4"):
                inits.setdefault(f, p)
            elif f.lower().endswith(".mpd") and mpd is None:
                mpd = p
    if mpd is None:
        raise ConvError("Nenhum arquivo .mpd encontrado em " + rec.root)

    reps = {}
    for rep in segs:
        init = inits.get(rep + "-decoder-init.mp4") or inits.get(rep + "-init.mp4")
        if not init:
            log("Aviso: representacao %s sem init, ignorada" % rep)
            continue
        reps[rep] = {"info": init_info(init), "init": inits.get(rep + "-init.mp4") or init}
    video = [r for r in reps if reps[r]["info"]["kind"] == "video"]
    audio = [r for r in reps if reps[r]["info"]["kind"] == "audio"]
    base = [r for r in video if reps[r]["info"]["entry"] != "lvc1"]
    enh = [r for r in video if reps[r]["info"]["entry"] == "lvc1"]
    if not base:
        raise ConvError("Nenhuma trilha de video base encontrada")
    base = base[0]
    enh = enh[0] if enh else None

    tl = {}
    for rep in [base] + ([enh] if enh else []) + audio[:1]:
        tl[rep] = [(t, segment_info(segs[rep][t])[1]) for t in sorted(segs[rep])]
    if enh:
        common = set(t for t, _ in tl[base]) & set(t for t, _ in tl[enh])
        for rep in (base, enh):
            dropped = len(tl[rep]) - len(common)
            if dropped:
                log("Aviso: %d segmento(s) de %s sem par na outra camada de video foram ignorados" % (dropped, rep))
            tl[rep] = [x for x in tl[rep] if x[0] in common]
    issues = {}
    for rep, items in tl.items():
        gaps = overl = 0
        for (t, d), (t2, _) in zip(items, items[1:]):
            if t2 > t + d:
                gaps += 1
            elif t2 < t + d:
                overl += 1
        issues[rep] = (gaps, overl)
        if gaps or overl:
            log("Aviso: %s tem %d lacuna(s) e %d sobreposicao(oes) na linha do tempo" % (rep, gaps, overl))
    vts = reps[base]["info"]["timescale"]
    rec.info = {
        "mpd": mpd, "segs": segs, "reps": reps, "base": base, "enh": enh,
        "audio": audio[0] if audio else None, "tl": tl, "issues": issues,
        "video_t": tl[base], "audio_t": tl[audio[0]] if audio else [],
        "vts": vts,
        "ats": reps[audio[0]]["info"]["timescale"] if audio else None,
        "duration": sum(d for _, d in tl[base]) / float(vts),
    }
    return rec.info


def _link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def stage(rec, log=print, progress=None, cancel=None):
    i = rec.info
    sd = os.path.join(rec.root, STAGE_NAME)
    if os.path.isdir(sd):
        shutil.rmtree(sd)
    os.makedirs(sd)
    vreps = [r for r in (i["base"], i["enh"]) if r]
    for rep in vreps:
        for name in (rep + "-init.mp4", rep + "-decoder-init.mp4"):
            for leaf in rec.leaves:
                p = os.path.join(leaf, name)
                if os.path.exists(p):
                    shutil.copy2(p, os.path.join(sd, name))
                    break
    total = len(i["video_t"]) * len(vreps) + len(i["audio_t"])
    done = 0
    for rep in vreps:
        for t, _ in i["tl"][rep]:
            if cancel and cancel.is_set():
                raise ConvError("Cancelado")
            _link_or_copy(i["segs"][rep][t], os.path.join(sd, "%s-%d.m4s" % (rep, t)))
            done += 1
            if progress and done % 200 == 0:
                progress(done / float(total))

    ET.register_namespace("", MPD_NS)
    tree = ET.parse(i["mpd"])
    ns = {"m": MPD_NS}
    for aset in tree.getroot().iter("{%s}AdaptationSet" % MPD_NS):
        if aset.get("contentType") != "video":
            continue
        rid = aset.find("m:Representation", ns).get("id")
        if rid not in i["tl"]:
            continue
        st = aset.find("m:SegmentTemplate", ns)
        old = st.find("m:SegmentTimeline", ns)
        if old is not None:
            st.remove(old)
        newtl = ET.SubElement(st, "{%s}SegmentTimeline" % MPD_NS)
        for t, d in i["tl"][rid]:
            ET.SubElement(newtl, "{%s}S" % MPD_NS, {"t": str(t), "d": str(d)})
    tree.write(os.path.join(sd, "completo.mpd"), encoding="utf-8", xml_declaration=True)

    audio_path = None
    if i["audio"]:
        audio_path = os.path.join(sd, "audio.mp4")
        with open(audio_path, "wb") as w:
            with open(i["reps"][i["audio"]]["init"], "rb") as f:
                shutil.copyfileobj(f, w)
            for t, _ in i["audio_t"]:
                if cancel and cancel.is_set():
                    raise ConvError("Cancelado")
                with open(i["segs"][i["audio"]][t], "rb") as f:
                    shutil.copyfileobj(f, w)
                done += 1
                if progress and done % 200 == 0:
                    progress(done / float(total))
    if progress:
        progress(1.0)
    return os.path.join(sd, "completo.mpd"), audio_path


def cleanup_stage(rec):
    sd = os.path.join(rec.root, STAGE_NAME)
    if os.path.isdir(sd):
        shutil.rmtree(sd, ignore_errors=True)


def find_ffmpeg(custom=None, app_dir=None):
    cands = []
    if custom:
        cands.append(custom)
    if app_dir:
        cands.append(os.path.join(app_dir, "ffmpeg", "bin", "ffmpeg.exe"))
        cands.append(os.path.join(app_dir, "ffmpeg.exe"))
    cands.append("C:/ffmpeg-mpegh/bin/ffmpeg.exe")
    w = shutil.which("ffmpeg")
    if w:
        cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)


def check_ffmpeg(ffmpeg):
    return {
        "mpegh": "libmpeghdec" in _run([ffmpeg, "-hide_banner", "-decoders"]).stdout,
        "lcevc": " lcevc " in _run([ffmpeg, "-hide_banner", "-filters"]).stdout,
        "svtav1": "libsvtav1" in _run([ffmpeg, "-hide_banner", "-encoders"]).stdout,
    }


def _ffprobe_of(ffmpeg):
    p = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
    return p if os.path.isfile(p) else shutil.which("ffprobe")


def _frac(s):
    return float(Fraction(s))


def probe_hdr(fp, mpd, index):
    hdr = dict(DEFAULT_HDR)
    try:
        r = _run([fp, "-v", "error", "-select_streams", str(index), "-read_intervals", "%+#1",
                  "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", mpd])
        frames = json.loads(r.stdout).get("frames", [])
        for sd in (frames[0].get("side_data_list") or []) if frames else []:
            t = sd.get("side_data_type", "")
            if t.startswith("Mastering display"):
                hdr["mastering"] = "G(%.4f,%.4f)B(%.4f,%.4f)R(%.4f,%.4f)WP(%.4f,%.4f)L(%g,%.4f)" % (
                    _frac(sd["green_x"]), _frac(sd["green_y"]), _frac(sd["blue_x"]), _frac(sd["blue_y"]),
                    _frac(sd["red_x"]), _frac(sd["red_y"]), _frac(sd["white_point_x"]),
                    _frac(sd["white_point_y"]), _frac(sd["max_luminance"]), _frac(sd["min_luminance"]))
            elif t.startswith("Content light"):
                hdr["light"] = "%d,%d" % (int(sd["max_content"]), int(sd["max_average"]))
    except Exception:
        pass
    return hdr


def build_command(ffmpeg, rec, mpd, audio_path, out_path, opts, log=print):
    fp = _ffprobe_of(ffmpeg)
    if not fp:
        raise ConvError("ffprobe nao encontrado ao lado do ffmpeg")
    r = _run([fp, "-v", "error", "-show_streams", "-show_entries", "format=start_time", "-of", "json", mpd])
    data = json.loads(r.stdout or "{}")
    vidx = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and s.get("codec_name") != "lcevc":
            vidx = s["index"]
            break
    if vidx is None:
        raise ConvError("Nao encontrei a trilha de video base no MPD")
    hdr = probe_hdr(fp, mpd, vidx)
    log("HDR10: " + hdr["mastering"] + " | MaxCLL/MaxFALL " + hdr["light"])
    vstart = float((data.get("format") or {}).get("start_time") or 0)
    info = rec.info
    svt = ("color-primaries=9:transfer-characteristics=16:matrix-coefficients=9:color-range=0:"
           "mastering-display=%s:content-light=%s" % (hdr["mastering"], hdr["light"]))
    if opts.get("lp"):
        svt += ":lp=%d" % opts["lp"]
    layout, acodec = AUDIO_MODES[opts["audio"]]
    use_audio = bool(audio_path and layout)
    v_off = a_off = 0.0
    if use_audio:
        delta = info["audio_t"][0][0] / float(info["ats"]) - vstart
        if delta >= 0:
            a_off = delta
        else:
            v_off = -delta
    cmd = [ffmpeg, "-hide_banner", "-y", "-nostdin", "-nostats", "-progress", "pipe:1"]
    if v_off:
        cmd += ["-itsoffset", "%.6f" % v_off]
    cmd += ["-i", mpd]
    if use_audio:
        if a_off:
            cmd += ["-itsoffset", "%.6f" % a_off]
        cmd += ["-c:a", "libmpeghdec", "-ch_layout", layout, "-i", audio_path]
    cmd += ["-map", "0:%d" % vidx]
    if use_audio:
        cmd += ["-map", "1:a:0"]
    vf = "lcevc,setparams=colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:range=tv"
    cmd += ["-vf", vf, "-c:v", "libsvtav1", "-preset", str(opts["preset"]), "-crf", str(opts["crf"]),
            "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
            "-color_range", "tv", "-svtav1-params", svt]
    cmd += acodec if use_audio else ["-an"]
    cmd += [out_path]
    return cmd


class Job:
    def __init__(self):
        self.cancel = threading.Event()
        self.proc = None

    def kill(self):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)


def output_duration(ffmpeg, path):
    fp = _ffprobe_of(ffmpeg)
    try:
        r = _run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "json", path])
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def run_encode(cmd, duration, job, on_progress, log_path, ffmpeg=None, out_path=None):
    with open(log_path, "w", errors="replace") as errf:
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True,
                                    errors="replace", creationflags=NO_WINDOW)
        cur = {}
        for line in job.proc.stdout:
            if "=" not in line:
                continue
            k, v = line.strip().split("=", 1)
            cur[k] = v
            if k == "progress":
                try:
                    done = int(cur.get("out_time_us", "0")) / 1e6
                except ValueError:
                    done = 0
                on_progress(min(1.0, max(0.0, done / duration)) if duration else 0.0,
                            cur.get("fps", "0"), cur.get("speed", "?"), done)
                cur = {}
        rc = job.proc.wait()
    if job.cancel.is_set():
        raise ConvError("Cancelado")
    tail = ""
    try:
        with open(log_path, errors="replace") as f:
            tail = "".join(f.readlines()[-8:])
    except OSError:
        pass
    if rc != 0 and "Error flushing BSFs" in tail and out_path and os.path.isfile(out_path):
        got = output_duration(ffmpeg, out_path)
        if duration and abs(got - duration) <= max(2.0, duration * 0.005):
            return "ffmpeg reportou um erro ao fechar a camada LCEVC, mas o arquivo tem a duracao esperada (%s)" % fmt_time(got)
        raise ConvError("Saida incompleta: %s de %s" % (fmt_time(got), fmt_time(duration)))
    if rc != 0:
        raise ConvError("ffmpeg falhou (codigo %s):\n%s" % (rc, tail))
    return None
