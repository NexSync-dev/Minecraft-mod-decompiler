#!/usr/bin/env python3
"""mc_decompiler.py  --  Remap, decompile, and scaffold Minecraft JARs."""

import sys, json, zipfile, subprocess, argparse, shutil
import threading, urllib.request, os, re, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ──────────────────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────────────────

TOOLS_DIR  = Path("tools")
OUTPUT_DIR = Path("output")

VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
SPECIALSOURCE_URL    = "https://repo.maven.apache.org/maven2/net/md-5/SpecialSource/1.11.6/SpecialSource-1.11.6-shaded.jar"
VINEFLOWER_URL       = "https://github.com/Vineflower/vineflower/releases/download/1.12.0/vineflower-1.12.0.jar"

UNOBFUSCATED_FROM  = (1, 21, 4)
JAVA_TIMEOUT       = 15      # seconds
MANIFEST_TIMEOUT   = 30
DOWNLOAD_TIMEOUT   = 300     # 5 min per file
REMAP_TIMEOUT      = 900     # 15 min
DOWNLOAD_RETRIES   = 3

_UA = {"User-Agent": "mc-decompiler/2.0"}

# ──────────────────────────────────────────────────────────────────────────────
#  Output
# ──────────────────────────────────────────────────────────────────────────────

_print_lock = threading.Lock()

def _print(msg="", end="\n"):
    with _print_lock:
        print(msg, end=end, flush=True)

BANNER = r"""
  __  __  ____
 |  \/  |/ ___|   Decompiler
 | |\/| | |       remap  ->  decompile  ->  gradle
 | |  | | |___
 |_|  |_|\____|"""

def print_header(count=0):
    _print(BANNER)
    if count > 1:
        _print(f"\n  {count} jars queued")
    _print("  " + "=" * 50)
    _print()


class Step:
    def __init__(self, label, prefix=""):
        self.label = label
        self._pfx  = f"  [{prefix}] " if prefix else "  "
        self._t0   = time.monotonic()

    def _ts(self):
        s = time.monotonic() - self._t0
        return f"  ({s:.0f}s)" if s >= 1.0 else ""

    def working(self, note=""):
        extra = f"  ({note})" if note else ""
        _print(f"{self._pfx}[ .. ] {self.label}{extra}")

    def ok(self, note=""):
        extra = f"  {note}" if note else ""
        _print(f"{self._pfx}[OK]   {self.label}{extra}{self._ts()}")

    def skip(self, reason=""):
        extra = f"  ({reason})" if reason else ""
        _print(f"{self._pfx}[--]   {self.label}{extra}")

    def warn(self, note):
        _print(f"{self._pfx}[WARN] {self.label}  ({note})")

    def fail(self, msg):
        _print(f"{self._pfx}[FAIL] {self.label}{self._ts()}")
        for line in str(msg).strip().splitlines()[-12:]:
            _print(f"        | {line}")
        raise SystemExit(1)


# ──────────────────────────────────────────────────────────────────────────────
#  Network
# ──────────────────────────────────────────────────────────────────────────────

def fetch_json(url, timeout=MANIFEST_TIMEOUT):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def download(url, dest, timeout=DOWNLOAD_TIMEOUT, retries=DOWNLOAD_RETRIES):
    if dest.exists():
        return
    tmp = dest.with_suffix(".part")
    last_err = None
    for attempt in range(retries):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                with open(tmp, "wb") as fh:
                    while chunk := r.read(131072):
                        fh.write(chunk)
            tmp.rename(dest)
            return
        except Exception as e:
            last_err = e
            tmp.unlink(missing_ok=True)
    raise RuntimeError(f"download failed after {retries} attempts: {last_err}")


# ──────────────────────────────────────────────────────────────────────────────
#  Version helpers
# ──────────────────────────────────────────────────────────────────────────────

def ver_tuple(s):
    if not s:
        return None
    try:
        return tuple(int(x) for x in re.findall(r"\d+", s.split("-")[0])[:3])
    except Exception:
        return None


def extract_mc_ver(raw):
    """Pull a bare x.y[.z] version string from a semver constraint."""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", raw.strip().strip("\"'"))
    return m.group(1) if m else ""


def resolve_ver(ver, manifest):
    """Return the best matching Mojang manifest version ID for ver, or ''."""
    if not ver:
        return ""
    ids = {v["id"] for v in manifest["versions"]}
    if ver in ids:
        return ver
    if ver.count(".") == 1:
        for patch in range(6):
            if f"{ver}.{patch}" in ids:
                return f"{ver}.{patch}"
    base = ".".join(ver.split(".")[:2])
    cands = sorted(
        [i for i in ids if i == base or i.startswith(base + ".")],
        key=lambda v: [int(x) for x in re.findall(r"\d+", v)],
    )
    return cands[-1] if cands else ""


# ──────────────────────────────────────────────────────────────────────────────
#  Core decompiler
# ──────────────────────────────────────────────────────────────────────────────

class MCDecompiler:

    def __init__(self, jar_path, *, output_path=None, prefix="",
                 vf_threads=None, vf_heap_mb=2048, vf_timeout=3600, force=False):
        self.jar         = Path(jar_path)
        self.out         = Path(output_path) if output_path else OUTPUT_DIR / self.jar.stem
        self.tools       = TOOLS_DIR
        self.version     = None
        self.mappings    = None
        self.remapped    = None
        self._manifest   = None
        self._prefix     = prefix
        self._vf_threads = vf_threads
        self._vf_heap    = vf_heap_mb
        self._vf_timeout = vf_timeout
        self._force      = force

        self.tools.mkdir(exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)

    def _s(self, label):
        return Step(label, self._prefix)

    def manifest(self):
        if not self._manifest:
            self._manifest = fetch_json(VERSION_MANIFEST_URL)
        return self._manifest

    # ── Java check ────────────────────────────────────────────────────────────

    def check_java(self):
        s = self._s("Java")
        try:
            r = subprocess.run(
                ["java", "-version"], capture_output=True, text=True, timeout=JAVA_TIMEOUT
            )
            line = next(
                (l for l in (r.stderr + r.stdout).splitlines() if "version" in l), ""
            )
            major = 0
            if '"' in line:
                raw = line.split('"')[1]
                major = int(raw.split(".")[1]) if raw.startswith("1.") else int(raw.split(".")[0])
            if major and major < 17:
                s.fail(f"Java {major} detected; need >= 17")
            s.ok(f"Java {major}" if major else line.strip() or "version unknown")
        except FileNotFoundError:
            s.fail("java not in PATH")
        except subprocess.TimeoutExpired:
            s.fail("java -version timed out")

    # ── Tools ─────────────────────────────────────────────────────────────────

    def fetch_tools(self):
        s = self._s("Tools")
        needed = [
            (SPECIALSOURCE_URL, self.tools / "SpecialSource.jar"),
            (VINEFLOWER_URL,    self.tools / "Vineflower.jar"),
        ]
        needed = [(u, d) for u, d in needed if not d.exists()]
        if not needed:
            s.skip("cached")
            return
        s.working(f"downloading {len(needed)} file(s)")
        errors = []
        with ThreadPoolExecutor(max_workers=len(needed)) as ex:
            futs = {ex.submit(download, u, d): d.name for u, d in needed}
            for f in as_completed(futs):
                if f.exception():
                    errors.append(f"{futs[f]}: {f.exception()}")
        if errors:
            s.fail("\n".join(errors))
        s.ok()

    # ── Version detection ─────────────────────────────────────────────────────

    def detect_version(self):
        s   = self._s("Version")
        ver = ""
        src = ""

        with zipfile.ZipFile(self.jar) as z:
            names = set(z.namelist())

            if "version.json" in names:
                with z.open("version.json") as f:
                    ver = json.load(f).get("id", "")
                src = "version.json"

            if not ver and "fabric.mod.json" in names:
                with z.open("fabric.mod.json") as f:
                    data = json.load(f)
                mc  = (data.get("depends") or {}).get("minecraft", "")
                ver = extract_mc_ver(mc)
                src = "fabric"

            if not ver:
                for toml in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                    if toml not in names:
                        continue
                    with z.open(toml) as f:
                        content = f.read().decode("utf-8", errors="replace")
                    for line in content.splitlines():
                        lo = line.lower()
                        if "minecraft" in lo and "=" in line:
                            raw = line.split("=", 1)[1].strip()
                            ver = extract_mc_ver(raw)
                            if ver:
                                src = "forge/neoforge"
                                break
                    if ver:
                        break

            if not ver and "quilt.mod.json" in names:
                with z.open("quilt.mod.json") as f:
                    data = json.load(f)
                deps = (data.get("quilt_loader") or {}).get("depends", [])
                for dep in (deps if isinstance(deps, list) else []):
                    if isinstance(dep, dict) and dep.get("id") == "minecraft":
                        ver = extract_mc_ver(dep.get("versions", ""))
                        if ver:
                            src = "quilt"
                        break

            if not ver and "META-INF/MANIFEST.MF" in names:
                with z.open("META-INF/MANIFEST.MF") as f:
                    for line in f.read().decode("utf-8", errors="replace").splitlines():
                        if "Implementation-Version" in line and ":" in line:
                            raw = line.split(":", 1)[1].strip()
                            if re.match(r"\d+\.\d+", raw):
                                ver = raw
                                src = "MANIFEST.MF"
                                break

        if not ver:
            m = re.search(r"[-_ ](\d+\.\d+(?:\.\d+)?)", self.jar.stem)
            if m:
                ver = m.group(1)
                src = "filename"

        if ver:
            resolved = resolve_ver(ver, self.manifest())
            if resolved and resolved != ver:
                src = f"{src} -> {resolved}"
                ver = resolved
            elif not resolved:
                s.warn(f"'{ver}' not in Mojang manifest; mappings unavailable")

        self.version = ver or None

        if self.version:
            s.ok(f"{self.version}  [{src}]")
        else:
            s.skip("not detected")
            self.version = input("    MC version (e.g. 1.20.1): ").strip() or None

    # ── Mappings ──────────────────────────────────────────────────────────────

    def fetch_mappings(self):
        t = ver_tuple(self.version or "")
        if t and t >= UNOBFUSCATED_FROM:
            self._s("Mappings").skip(f"{self.version} ships with readable names")
            return

        s     = self._s("Mappings")
        entry = next((v for v in self.manifest()["versions"] if v["id"] == self.version), None)
        if not entry:
            s.skip(f"'{self.version}' not in manifest")
            return

        meta = fetch_json(entry["url"])
        key  = "server_mappings" if "server" in self.jar.name.lower() else "client_mappings"
        if key not in meta.get("downloads", {}):
            s.skip(f"no {key} for {self.version}")
            return

        dest = self.tools / f"{self.version}-{key}.txt"
        if not dest.exists():
            s.working("downloading")
        download(meta["downloads"][key]["url"], dest)
        self.mappings = dest
        s.ok(dest.name)

    # ── Remap ─────────────────────────────────────────────────────────────────

    def remap(self):
        if not self.mappings:
            return

        s             = self._s("Remap")
        self.remapped = self.out / f"{self.jar.stem}-remapped.jar"

        if not self._force and self.remapped.exists():
            s.skip("cached")
            return

        s.working()
        cmd = [
            "java", "-Xmx1g",
            "-jar", str(self.tools / "SpecialSource.jar"),
            "--live",
            "-i", str(self.jar),
            "-o", str(self.remapped),
            "-m", str(self.mappings),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=REMAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            s.fail(f"SpecialSource timed out after {REMAP_TIMEOUT}s")
        if r.returncode:
            s.fail(r.stderr[-600:])
        s.ok()

    # ── Decompile ─────────────────────────────────────────────────────────────

    def decompile(self):
        s   = self._s("Decompile")
        src = self.out / "src" / "main" / "java"

        if not self._force and src.exists() and any(src.rglob("*.java")):
            count = sum(1 for _ in src.rglob("*.java"))
            s.skip(f"sources exist  ({count:,} files)")
            return

        src.mkdir(parents=True, exist_ok=True)
        source_jar = self.remapped or self.jar
        threads    = str(self._vf_threads or max(1, (os.cpu_count() or 2) - 1))

        cmd = [
            "java", f"-Xmx{self._vf_heap}m",
            "-jar", str(self.tools / "Vineflower.jar"),
            f"--threads={threads}",
            "--decompile-generics=1",
            "--remove-synthetic=1",
            "--ignore-invalid-bytecode=1",
            "--verify-anonymous-classes=1",
            "--unsupported-exceptions=0",
            "--decompile-inner=1",
            "--include-classpath=0",
            "--log-level=error",
            "--indent-string=    ",
            str(source_jar),
            str(src),
        ]

        s.working(f"threads={threads}  heap={self._vf_heap}m")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self._vf_timeout)
        except subprocess.TimeoutExpired:
            s.fail(f"Vineflower timed out after {self._vf_timeout}s  (raise with --timeout)")

        files = list(src.rglob("*.java"))
        if not files:
            tail = r.stderr[-800:] if r.stderr else "(no output; JAR may have no classes)"
            s.fail(tail)
        s.ok(f"{len(files):,} files")

    # ── Resources ─────────────────────────────────────────────────────────────

    def copy_resources(self):
        s    = self._s("Resources")
        dest = self.out / "src" / "main" / "resources"
        SKIP = {".class"}

        if not self._force and dest.exists() and any(f for f in dest.rglob("*") if f.is_file()):
            count = sum(1 for f in dest.rglob("*") if f.is_file())
            s.skip(f"already extracted  ({count} files)")
            return

        copied = 0
        with zipfile.ZipFile(self.jar) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                p = Path(info.filename)
                if p.suffix.lower() in SKIP:
                    continue
                out_path = dest / info.filename
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(z.read(info))
                copied += 1

        if copied:
            s.ok(f"{copied} files")
        else:
            s.skip("no non-class resources")

    # ── Gradle scaffold ───────────────────────────────────────────────────────

    def gradle(self):
        s = self._s("Gradle")
        t = ver_tuple(self.version or "")
        if   t and t >= (1, 21): java_ver = 21
        elif t and t >= (1, 18): java_ver = 17
        elif t and t >= (1, 17): java_ver = 16
        else:                    java_ver = 8

        jar_ref = self.remapped or self.jar
        libs    = self.out / "libs"
        libs.mkdir(exist_ok=True)
        shutil.copy2(jar_ref, libs / jar_ref.name)

        (self.out / "settings.gradle").write_text(
            f"rootProject.name = '{self.jar.stem}'\n"
        )
        (self.out / "build.gradle").write_text(
            "plugins {\n"
            "    id 'java'\n"
            "}\n\n"
            "group   = 'org.example'\n"
            "version = '1.0-SNAPSHOT'\n\n"
            "repositories {\n"
            "    mavenCentral()\n"
            "}\n\n"
            "dependencies {\n"
            f"    implementation files('libs/{jar_ref.name}')\n"
            "    testImplementation 'org.junit.jupiter:junit-jupiter-api:5.10.0'\n"
            "    testRuntimeOnly    'org.junit.jupiter:junit-jupiter-engine:5.10.0'\n"
            "}\n\n"
            "test {\n"
            "    useJUnitPlatform()\n"
            "}\n\n"
            "java {\n"
            "    toolchain {\n"
            f"        languageVersion = JavaLanguageVersion.of({java_ver})\n"
            "    }\n"
            "}\n"
        )
        (self.out / "src" / "main" / "resources").mkdir(parents=True, exist_ok=True)
        s.ok(f"Java {java_ver}")

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, show_jar=True):
        t0 = time.monotonic()
        if show_jar:
            _print(f"\n  {self.jar.name}")
            _print("  " + "-" * 50)
        self.check_java()
        self.fetch_tools()
        self.detect_version()
        self.fetch_mappings()
        self.remap()
        self.decompile()
        self.copy_resources()
        self.gradle()
        elapsed = time.monotonic() - t0
        mins, secs = divmod(int(elapsed), 60)
        t_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        _print(f"\n  Done  {self.out.resolve()}  [{t_str}]")


# ──────────────────────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_jars(tokens):
    jars = []
    for t in tokens:
        p = Path(t.strip().strip('"'))
        if p.suffix.lower() == ".jar":
            if p.exists():
                jars.append(p)
            else:
                _print(f"  [!] not found: {p}")
    return jars


if __name__ == "__main__":
    cpu = os.cpu_count() or 2

    ap = argparse.ArgumentParser(
        description="Decompile Minecraft JARs and scaffold Gradle projects.",
    )
    ap.add_argument("jars",                nargs="*",                        help=".jar file(s)")
    ap.add_argument("-o", "--output",      metavar="DIR",                    help="output directory (single-jar) or parent directory (multi-jar)")
    ap.add_argument("-j", "--jobs",        metavar="N",  type=int, default=1,    help="max concurrent JAR jobs (default: 1)")
    ap.add_argument("-t", "--threads",     metavar="N",  type=int, default=None, help="Vineflower threads per job (default: auto)")
    ap.add_argument("-m", "--mem",         metavar="MB", type=int, default=2048, help="JVM heap per Vineflower job in MB (default: 2048)")
    ap.add_argument("--timeout",           metavar="MIN",type=int, default=60,   help="decompile timeout per JAR in minutes (default: 60)")
    ap.add_argument("--force",             action="store_true",                  help="re-run even if output already exists")
    args = ap.parse_args()

    jars = parse_jars(args.jars)
    if not jars:
        raw  = input("JAR path(s): ").strip()
        jars = parse_jars(raw.split())
    if not jars:
        _print("No valid .jar files found.")
        sys.exit(1)

    multi = len(jars) > 1
    jobs  = min(args.jobs, len(jars))

    if args.threads:
        vf_threads = args.threads
    elif multi and jobs > 1:
        vf_threads = max(1, cpu // jobs)
    else:
        vf_threads = max(1, cpu - 1)

    print_header(count=len(jars))

    if multi:
        _print(f"  jobs={jobs}  threads/job={vf_threads}  heap={args.mem}m  timeout={args.timeout}min")
        _print()

    try:
        manifest = fetch_json(VERSION_MANIFEST_URL)
    except Exception as e:
        _print(f"  [FAIL] Cannot fetch version manifest: {e}")
        sys.exit(1)

    ok_list, fail_list = [], []
    t_wall = time.monotonic()

    def process(pair):
        idx, jar = pair
        pfx = f"{idx+1}/{len(jars)}" if multi else ""

        if multi:
            _print(f"\n  [{pfx}] {jar.name}")
            _print("  " + "-" * 50)

        if multi and args.output:
            out = Path(args.output) / jar.stem
        elif not multi and args.output:
            out = args.output
        else:
            out = None

        d = MCDecompiler(
            jar,
            output_path = out,
            prefix      = pfx,
            vf_threads  = vf_threads,
            vf_heap_mb  = args.mem,
            vf_timeout  = args.timeout * 60,
            force       = args.force,
        )
        d._manifest = manifest
        try:
            d.run(show_jar=not multi)
            return (jar.name, True)
        except SystemExit:
            return (jar.name, False)
        except Exception as e:
            _print(f"  [!] {jar.name}: {e}")
            return (jar.name, False)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for name, ok in ex.map(process, enumerate(jars)):
            (ok_list if ok else fail_list).append(name)

    if multi or fail_list:
        elapsed = time.monotonic() - t_wall
        mins, secs = divmod(int(elapsed), 60)
        t_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        _print(f"\n  {'=' * 50}")
        _print(f"  {len(ok_list)}/{len(jars)} completed in {t_str}")
        for name in ok_list:
            _print(f"  [OK]   {name}")
        for name in fail_list:
            _print(f"  [FAIL] {name}")
        _print()

    input("\nPress Enter to exit...")
