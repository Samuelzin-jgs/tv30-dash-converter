import ctypes
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import xml.etree.ElementTree as ET
from ctypes import wintypes
from fractions import Fraction

MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
SEG_RE = re.compile(r"^(\w+)-(\d+)\.m4s$")
STAGE_NAME = "_tv30_stage"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_HDR = {
    "mastering": "G(0.1700,0.7970)B(0.1310,0.0460)R(0.7080,0.2920)WP(0.3127,0.3290)L(1000,0.0050)",
    "light": "1000,300",
}

LAYOUTS = {
    "5.1.2 (3D, 8 canais)": "5.1.2",
    "5.1 (6 canais)": "5.1(side)",
    "Estereo": "stereo",
    "Sem audio": None,
}
_ALL = ("5.1.2", "5.1(side)", "stereo")
_SURR = ("5.1(side)", "stereo")
AUDIO_CODECS = {
    "FLAC (sem perdas)": ("flac", {k: ["-c:a", "flac"] for k in _ALL}, _ALL),
    "PCM 24 bits (sem perdas)": ("pcm", {"5.1.2": ["-c:a", "pcm_s24le"], "5.1(side)": ["-c:a", "pcm_s24le"],
                                         "stereo": ["-c:a", "pcm_s24le"]}, _ALL),
    "AAC": ("aac", {"5.1.2": ["-c:a", "aac", "-b:a", "768k"], "5.1(side)": ["-c:a", "aac", "-b:a", "512k"],
                    "stereo": ["-c:a", "aac", "-b:a", "256k"]}, _ALL),
    "Opus": ("opus", {"5.1(side)": ["-af", "aformat=channel_layouts=5.1", "-c:a", "libopus", "-b:a", "384k"],
                      "stereo": ["-c:a", "libopus", "-b:a", "160k"]}, _SURR),
    "Dolby Digital (AC3)": ("ac3", {"5.1(side)": ["-c:a", "ac3", "-b:a", "448k"],
                                    "stereo": ["-c:a", "ac3", "-b:a", "256k"]}, _SURR),
    "Dolby Digital Plus (E-AC3)": ("eac3", {"5.1(side)": ["-c:a", "eac3", "-b:a", "640k"],
                                            "stereo": ["-c:a", "eac3", "-b:a", "256k"]}, _SURR),
    "MP3": ("mp3", {"stereo": ["-c:a", "libmp3lame", "-b:a", "256k"]}, ("stereo",)),
}

VIDEO_CODECS = {
    "AV1 (SVT-AV1)": "av1",
    "HEVC / H.265 (x265)": "hevc",
    "H.264 (x264)": "h264",
}
GPU_CODECS = {
    "HEVC - NVIDIA NVENC": "hevc_nvenc",
    "AV1 - NVIDIA NVENC (RTX 40+)": "av1_nvenc",
    "HEVC - AMD AMF": "hevc_amf",
    "AV1 - AMD AMF (RX 7000+)": "av1_amf",
    "HEVC - Intel QSV": "hevc_qsv",
    "AV1 - Intel QSV (Arc)": "av1_qsv",
}
_Q = {"crf_range": (10, 40), "crf": 24}
_X = ["veryfast", "faster", "fast", "medium", "slow", "slower"]
VIDEO_INFO = {
    "av1": {"suffix": "AV1", "crf": 18, "crf_range": (10, 40), "presets": [str(i) for i in range(2, 11)],
            "preset": "6"},
    "hevc": {"suffix": "HEVC", "crf": 20, "crf_range": (14, 34),
             "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower"], "preset": "medium"},
    "h264": {"suffix": "H264", "crf": 18, "crf_range": (12, 30),
             "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower"], "preset": "medium"},
}
for _id in GPU_CODECS.values():
    _b, _k = _id.split("_")
    VIDEO_INFO[_id] = dict(_Q, suffix=_b.upper() + "_" + _k.upper(), gpu=True, **{
        "nvenc": {"presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"], "preset": "p5"},
        "amf": {"presets": ["speed", "balanced", "quality"], "preset": "quality"},
        "qsv": {"presets": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                "preset": "slow"},
    }[_k])


def base_codec(vid):
    return vid.split("_")[0]


GPU_WHY = {}


def probe_gpu(ffmpeg):
    GPU_WHY.clear()
    ok = {}
    try:
        r = _run([ffmpeg, "-hide_banner", "-encoders"])
        have = r.stdout + r.stderr
    except Exception:
        return ok
    for name, vid in GPU_CODECS.items():
        if vid not in have:
            continue
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                "color=c=black:s=1280x720:r=30:d=0.3", "-vf", "format=p010le", "-frames:v", "3",
                                "-c:v", vid, "-pix_fmt", "p010le", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            ok[name] = r.returncode == 0
            if r.returncode != 0:
                lines = [l for l in r.stderr.splitlines() if l.strip()]
                why = next((l for l in lines if "river" in l or "capable" in l or "ailed" in l or "not " in l), "")
                GPU_WHY[name] = why.split("] ", 1)[-1] if why else "falhou ao abrir o encoder"
        except Exception:
            ok[name] = False
            GPU_WHY[name] = "falhou ao testar"
    return {k: v for k, v in ok.items() if v}
CONTAINERS = {
    "MKV (Matroska)": "mkv",
    "MP4": "mp4",
    "MOV (QuickTime)": "mov",
    "WebM": "webm",
}
CONTAINER_INFO = {
    "mkv": {"video": ("av1", "hevc", "h264"),
            "audio": ("flac", "pcm", "aac", "opus", "ac3", "eac3", "mp3")},
    "mp4": {"video": ("av1", "hevc", "h264"), "audio": ("flac", "aac", "opus", "ac3", "eac3", "mp3")},
    "mov": {"video": ("hevc", "h264"), "audio": ("pcm", "aac", "ac3", "eac3", "mp3")},
    "webm": {"video": ("av1",), "audio": ("opus",)},
}


def audio_codec_ids():
    return {v[0]: k for k, v in AUDIO_CODECS.items()}


def check_combo(opts):
    ext = opts["container"]
    ci = CONTAINER_INFO[ext]
    if base_codec(opts["vcodec"]) not in ci["video"]:
        return "O conteiner %s nao aceita esse codec de video." % ext.upper()
    layout = LAYOUTS[opts["layout"]]
    if layout is None:
        return None
    ac = AUDIO_CODECS[opts["acodec"]]
    if ac[0] not in ci["audio"]:
        return "O conteiner %s nao aceita esse codec de audio." % ext.upper()
    if layout not in ac[2]:
        return "Esse codec de audio nao suporta esse numero de canais."
    return None


MEM_PRESETS = {
    "Ilimitado": None,
    "8 GB": ("8", "90", 8),
    "6 GB": ("4", "40", 6),
    "4 GB": ("1", "40", 4),
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
    audio.sort()
    for rep in [base] + ([enh] if enh else []) + audio:
        tl[rep] = [(t, segment_info(segs[rep][t])[1]) for t in sorted(segs[rep])]
    vstart = segment_info(segs[base][tl[base][0][0]])[0]
    if enh:
        common = set(t for t, _ in tl[base]) & set(t for t, _ in tl[enh])
        for rep in (base, enh):
            dropped = len(tl[rep]) - len(common)
            if dropped:
                log("Aviso: %d segmento(s) de %s sem par na outra camada de video foram ignorados" % (dropped, rep))
            tl[rep] = [x for x in tl[rep] if x[0] in common]
        vstart = segment_info(segs[base][tl[base][0][0]])[0]
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
    audios = []
    for rep in audio:
        ats = reps[rep]["info"]["timescale"]
        cut = vstart / float(vts)
        items = [x for x in tl[rep] if (x[0] + x[1]) / float(ats) > cut + 1e-6]
        if len(items) != len(tl[rep]):
            log("Aviso: %d segmento(s) de audio %s anteriores ao inicio do video foram descartados" % (
                len(tl[rep]) - len(items), rep))
        tl[rep] = items
        if items:
            audios.append({"rep": rep, "ats": ats, "audio_t": items})
    rec.info = {
        "mpd": mpd, "segs": segs, "reps": reps, "base": base, "enh": enh,
        "tl": tl, "issues": issues,
        "video_t": tl[base], "audios": audios,
        "vts": vts,
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
    total = len(i["video_t"]) * len(vreps) + sum(len(a["audio_t"]) for a in i["audios"])
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

    audio_paths = []
    for n, a in enumerate(i["audios"]):
        path = os.path.join(sd, "audio_%d.mp4" % n)
        with open(path, "wb") as w:
            with open(i["reps"][a["rep"]]["init"], "rb") as f:
                shutil.copyfileobj(f, w)
            for t, _ in a["audio_t"]:
                if cancel and cancel.is_set():
                    raise ConvError("Cancelado")
                with open(i["segs"][a["rep"]][t], "rb") as f:
                    shutil.copyfileobj(f, w)
                done += 1
                if progress and done % 200 == 0:
                    progress(done / float(total))
        audio_paths.append(path)
    if progress:
        progress(1.0)
    return os.path.join(sd, "completo.mpd"), audio_paths


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


def _x26x_master(mastering):
    m = re.match(r"G\(([\d.]+),([\d.]+)\)B\(([\d.]+),([\d.]+)\)R\(([\d.]+),([\d.]+)\)"
                 r"WP\(([\d.]+),([\d.]+)\)L\(([\d.]+),([\d.]+)\)", mastering)
    v = [float(x) for x in m.groups()]
    c = [int(round(x * 50000)) for x in v[:8]]
    return "G(%d,%d)B(%d,%d)R(%d,%d)WP(%d,%d)L(%d,%d)" % (
        c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], int(round(v[8] * 10000)), int(round(v[9] * 10000)))


def _video_args(vcodec, opts, hdr):
    crf, preset = str(opts["crf"]), str(opts["preset"])
    common = ["-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "smpte2084",
              "-color_range", "tv"]
    limit = float(opts.get("bitrate_limit") or 0)
    rate = []
    if limit:
        rate = ["-maxrate", "%dk" % int(limit * 1000), "-bufsize", "%dk" % int(limit * 2000)]
    if vcodec == "av1":
        svt = ("color-primaries=9:transfer-characteristics=16:matrix-coefficients=9:color-range=0:"
               "mastering-display=%s:content-light=%s" % (hdr["mastering"], hdr["light"]))
        mem = MEM_PRESETS.get(opts.get("mem") or "Ilimitado")
        if mem:
            svt += ":lp=%s:lookahead=%s" % (mem[0], mem[1])
        return ["-c:v", "libsvtav1", "-preset", preset, "-crf", crf] + rate + common + ["-svtav1-params", svt]
    gpu = ["-pix_fmt", "p010le", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-color_range", "tv"]
    if vcodec.endswith("_nvenc"):
        return ["-c:v", vcodec, "-preset", preset, "-rc", "vbr", "-cq", crf, "-b:v", "0"] + rate + gpu
    if vcodec.endswith("_amf"):
        if limit:
            return ["-c:v", vcodec, "-quality", preset, "-rc", "vbr_peak", "-b:v", "%dk" % int(limit * 1000),
                    "-maxrate", "%dk" % int(limit * 1000)] + gpu
        return ["-c:v", vcodec, "-quality", preset, "-rc", "cqp", "-qp_i", crf, "-qp_p", crf, "-qp_b", crf] + gpu
    if vcodec.endswith("_qsv"):
        if limit:
            return ["-c:v", vcodec, "-preset", preset, "-b:v", "%dk" % int(limit * 1000),
                    "-maxrate", "%dk" % int(limit * 1000)] + gpu
        return ["-c:v", vcodec, "-preset", preset, "-global_quality", crf] + gpu
    master = _x26x_master(hdr["mastering"])
    if vcodec == "hevc":
        xp = ("colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:range=limited:hdr10=1:"
              "repeat-headers=1:master-display=%s:max-cll=%s" % (master, hdr["light"]))
        return ["-c:v", "libx265", "-preset", preset, "-crf", crf] + rate + common + ["-x265-params", xp]
    xp = ("colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
          "mastering-display=%s:cll=%s" % (master, hdr["light"]))
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf] + rate + common + ["-x264-params", xp]


def build_command(ffmpeg, rec, mpd, audio_paths, out_path, opts, log=print):
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
    vcodec = opts["vcodec"]
    err = check_combo(opts)
    if err:
        raise ConvError(err)
    layout = LAYOUTS[opts["layout"]]
    acodec = AUDIO_CODECS[opts["acodec"]][1].get(layout, []) if layout else []
    tracks = list(zip(info["audios"], audio_paths or [])) if layout else []
    if tracks and not opts.get("all_audio"):
        tracks = tracks[:1]
    use_audio = bool(tracks)
    deltas = [a["audio_t"][0][0] / float(a["ats"]) - vstart for a, _ in tracks]
    v_off = max([0.0] + [-d for d in deltas])
    offs = [d + v_off for d in deltas]
    cmd = [ffmpeg, "-hide_banner", "-y", "-nostdin", "-nostats", "-progress", "pipe:1"]
    if v_off:
        cmd += ["-itsoffset", "%.6f" % v_off]
    cmd += ["-i", mpd]
    for (a, path), off in zip(tracks, offs):
        if off:
            cmd += ["-itsoffset", "%.6f" % off]
        cmd += ["-c:a", "libmpeghdec", "-ch_layout", layout, "-i", path]
    cmd += ["-map", "0:%d" % vidx]
    for n, (a, _) in enumerate(tracks):
        cmd += ["-map", "%d:a:0" % (n + 1), "-metadata:s:a:%d" % n, "title=%s" % a["rep"]]
    vf = "lcevc,scale=3840:2160:flags=lanczos,setparams=colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:range=tv"
    cmd += ["-vf", vf] + _video_args(vcodec, opts, hdr)
    if opts["container"] in ("mp4", "mov"):
        cmd += ["-movflags", "+faststart"]
        if base_codec(vcodec) == "hevc":
            cmd += ["-tag:v", "hvc1"]
    cmd += acodec if use_audio else ["-an"]
    cmd += [out_path]
    return cmd


class Job:
    def __init__(self):
        self.cancel = threading.Event()
        self.proc = None
        self.mem_limit = 0

    def kill(self):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW)


JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_ulonglong),
                ("PerJobUserTimeLimit", ctypes.c_ulonglong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _EXT_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                ("IoCounters", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _apply_memory_limit(pid, limit_bytes):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = _EXT_LIMIT()
    info.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY
    info.JobMemoryLimit = limit_bytes
    h = k32.CreateJobObjectW(None, None)
    if not h:
        return
    if not k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(h)
        return
    proc = k32.OpenProcess(0x1FFFFF, False, pid)
    if proc:
        k32.AssignProcessToJobObject(h, proc)
        k32.CloseHandle(proc)
    k32.CloseHandle(h)


def output_duration(ffmpeg, path):
    fp = _ffprobe_of(ffmpeg)
    try:
        r = _run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "json", path])
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def run_encode(cmd, duration, job, on_progress, log_path, ffmpeg=None, out_path=None, mem_limit=0):
    with open(log_path, "w", errors="replace") as errf:
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True,
                                    errors="replace", creationflags=NO_WINDOW)
        if mem_limit:
            _apply_memory_limit(job.proc.pid, mem_limit * 2 ** 30)
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
    tail = full = ""
    try:
        with open(log_path, errors="replace") as f:
            lines = f.readlines()
        tail = "".join(lines[-8:])
        full = "".join(lines)
    except OSError:
        pass
    if rc != 0 and "Error flushing BSFs" in full and out_path and os.path.isfile(out_path):
        got = output_duration(ffmpeg, out_path)
        if duration and abs(got - duration) <= max(2.0, duration * 0.005):
            return "ffmpeg reportou um erro ao fechar a camada LCEVC, mas o arquivo tem a duracao esperada (%s)" % fmt_time(got)
        raise ConvError("Saida incompleta: %s de %s" % (fmt_time(got), fmt_time(duration)))
    if rc != 0:
        if mem_limit and ("allocate memory failed" in tail or "Cannot allocate" in tail or "Cannot allocate memory" in tail):
            raise ConvError("A conversao passou do limite de memoria escolhido. "
                            "Escolha um limite maior ou 'Ilimitado'.")
        raise ConvError("ffmpeg falhou (codigo %s):\n%s" % (rc, tail))
    return None
