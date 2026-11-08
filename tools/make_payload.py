import os
import shutil
import subprocess
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "C:/ffmpeg-mpegh/bin"
MSYS = sys.argv[2] if len(sys.argv) > 2 else "C:/msys64/ucrt64/bin"
DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "payload", "ffmpeg", "bin")
OBJDUMP = os.path.join(MSYS, "objdump.exe")


def find(name):
    for d in (SRC, MSYS):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


need = set()
todo = ["ffmpeg.exe", "ffprobe.exe", "liblcevc_dec_pipeline_cpu.dll", "liblcevc_dec_api.dll"]
while todo:
    name = todo.pop()
    if name.lower() in need:
        continue
    path = find(name)
    if not path:
        continue
    need.add(name.lower())
    out = subprocess.run([OBJDUMP, "-p", path], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "DLL Name:" in line:
            todo.append(line.split(":", 1)[1].strip())

shutil.rmtree(os.path.dirname(os.path.dirname(DEST)), ignore_errors=True)
os.makedirs(DEST)
for name in sorted(need):
    shutil.copy2(find(name), DEST)
shutil.copy2(os.path.join(DEST, "liblcevc_dec_pipeline_cpu.dll"), os.path.join(DEST, "lcevc_dec_pipeline_cpu.dll"))
size = sum(os.path.getsize(os.path.join(DEST, f)) for f in os.listdir(DEST)) // 2 ** 20
print("payload: %d arquivos, %d MB em %s" % (len(os.listdir(DEST)), size, DEST))
