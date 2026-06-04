import sys
import json
import zipfile
import subprocess
import argparse
import shutil
import threading
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

TOOLS_DIR  = Path("tools")
OUTPUT_DIR = Path("output")

VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
SPECIALSOURCE_URL    = "https://repo.maven.apache.org/maven2/net/md-5/SpecialSource/1.11.6/SpecialSource-1.11.6-shaded.jar"
VINEFLOWER_URL       = "https://github.com/Vineflower/vineflower/releases/download/1.12.0/vineflower-1.12.0.jar"

UNOBFUSCATED_FROM = (1, 21, 4)


class Step:
    FRAMES = r"|/-\\"

    def __init__(self, label):
        self._label = label
        self._done  = threading.Event()
        self._th    = None

    def spin(self):
        self._done.clear()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def ok(self, note=""):
        self._stop()
        tail = f"  ({note})" if note else ""
        print(f"  [OK]   {self._label}{tail}")

    def skip(self, reason=""):
        self._stop()
        tail = f"  ({reason})" if reason else ""
        print(f"  [--]   {self._label}{tail}")

    def fail(self, msg):
        self._stop()
        print(f"  [FAIL] {self._label}\n         {msg}")
        raise SystemExit(1)

    def _loop(self):
        i = 0
        while not self._done.wait(0.12):
            print(f"  [ {self.FRAMES[i % 4]} ]  {self._label}", end="\r", flush=True)
            i += 1

    def _stop(self):
        if self._th:
            self._done.set()
            self._th.join()
            print(" " * (len(self._label) + 12), end="\r", flush=True)


def ver_tuple(s):
    try:
        return tuple(int(x) for x in s.lstrip("v").split(".")[:3])
    except Exception:
        return None


def fetch_json(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def download(url, dest):
    if dest.exists():
        return
    tmp = dest.with_suffix(".part")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


class MCDecompiler:

    def __init__(self, jar_path, output_path=None):
        self.jar        = Path(jar_path)
        self.out        = Path(output_path) if output_path else OUTPUT_DIR / self.jar.stem
        self.tools      = TOOLS_DIR
        self.version    = None
        self.mappings   = None
        self.remapped   = None
        self._manifest  = None

        self.tools.mkdir(exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)

    def manifest(self):
        if not self._manifest:
            self._manifest = fetch_json(VERSION_MANIFEST_URL)
        return self._manifest

    def check_java(self):
        s = Step("Java")
        try:
            r = subprocess.run(["java", "-version"], capture_output=True, text=True)
            line = next((l for l in (r.stderr + r.stdout).splitlines() if "version" in l), "")
            major = 0
            if '"' in line:
                raw = line.split('"')[1]
                major = int(raw.split(".")[1]) if raw.startswith("1.") else int(raw.split(".")[0])
            if major and major < 17:
                s.fail(f"Java {major} found, need 17+")
            s.ok(f"Java {major}" if major else "unknown version")
        except FileNotFoundError:
            s.fail("java not in PATH")

    def fetch_tools(self):
        s = Step("Tools")
        jobs = [
            (SPECIALSOURCE_URL, self.tools / "SpecialSource.jar"),
            (VINEFLOWER_URL,    self.tools / "Vineflower.jar"),
        ]
        needed = [(u, d) for u, d in jobs if not d.exists()]
        if not needed:
            s.skip("cached")
            return
        s.spin()
        errors = []
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(download, u, d): d.name for u, d in needed}
            for f in as_completed(futs):
                if f.exception():
                    errors.append(f"{futs[f]}: {f.exception()}")
        if errors:
            s.fail("; ".join(errors))
        s.ok()

    def detect_version(self):
        s = Step("Version")
        with zipfile.ZipFile(self.jar) as z:
            names = z.namelist()

            if "version.json" in names:
                with z.open("version.json") as f:
                    self.version = json.load(f).get("id")
                if self.version:
                    s.ok(self.version)
                    return

            if "fabric.mod.json" in names:
                with z.open("fabric.mod.json") as f:
                    data = json.load(f)
                mc = (data.get("depends") or {}).get("minecraft", "")
                if mc:
                    clean = mc.lstrip(">=~^").split()[0].split(",")[0]
                    if clean:
                        self.version = clean
                        s.ok(f"{self.version} (fabric)")
                        return

            for path in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if path in names:
                    with z.open(path) as f:
                        content = f.read().decode("utf-8", errors="replace")
                    for line in content.splitlines():
                        if "minecraft" in line.lower() and "=" in line:
                            raw = line.split("=", 1)[1].strip().strip('"\'')
                            clean = raw.lstrip("[>=~").split(",")[0].split(")")[0].strip()
                            if clean and clean[0].isdigit():
                                self.version = clean
                                s.ok(f"{self.version} (forge/neoforge)")
                                return

            if "quilt.mod.json" in names:
                with z.open("quilt.mod.json") as f:
                    data = json.load(f)
                deps = (data.get("quilt_loader") or {}).get("depends", [])
                for dep in (deps if isinstance(deps, list) else []):
                    if isinstance(dep, dict) and dep.get("id") == "minecraft":
                        raw = dep.get("versions", "")
                        if isinstance(raw, str):
                            clean = raw.lstrip(">=~^").split()[0]
                            if clean:
                                self.version = clean
                                s.ok(f"{self.version} (quilt)")
                                return

            try:
                with z.open("META-INF/MANIFEST.MF") as f:
                    for line in f.read().decode("utf-8", errors="replace").splitlines():
                        if "Implementation-Version" in line:
                            self.version = line.split(":", 1)[1].strip()
                            s.ok(f"{self.version} (manifest)")
                            return
            except KeyError:
                pass

        s.skip("not found")
        self.version = input("    MC version (e.g. 1.20.1): ").strip() or None

    def fetch_mappings(self):
        t = ver_tuple(self.version or "")
        if t and t >= UNOBFUSCATED_FROM:
            Step("Mappings").skip(f"{self.version} uses readable names")
            return

        s = Step("Mappings")
        entry = next((v for v in self.manifest()["versions"] if v["id"] == self.version), None)
        if not entry:
            s.skip(f"{self.version} not in Mojang manifest")
            return

        meta = fetch_json(entry["url"])
        key  = "client_mappings" if "server" not in self.jar.name.lower() else "server_mappings"
        if key not in meta.get("downloads", {}):
            s.skip(f"no {key} for {self.version}")
            return

        dest = self.tools / f"{self.version}-{key}.txt"
        download(meta["downloads"][key]["url"], dest)
        self.mappings = dest
        s.ok(dest.name)

    def remap(self):
        if not self.mappings:
            return

        s = Step("Remap")
        self.remapped = self.out / f"{self.jar.stem}-remapped.jar"
        if self.remapped.exists():
            s.skip("cached")
            return

        cmd = [
            "java", "-jar", str(self.tools / "SpecialSource.jar"),
            "--live",
            "-i", str(self.jar),
            "-o", str(self.remapped),
            "-m", str(self.mappings),
        ]
        s.spin()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            s.fail(r.stderr[-400:])
        s.ok()

    def decompile(self):
        s = Step("Decompile")
        src = self.out / "src" / "main" / "java"

        if src.exists() and any(src.rglob("*.java")):
            s.skip("sources exist")
            return

        src.mkdir(parents=True, exist_ok=True)
        source_jar = self.remapped or self.jar

        import os
        threads = str(max(1, (os.cpu_count() or 2) - 1))

        cmd = [
            "java", "-Xmx2g",
            "-jar", str(self.tools / "Vineflower.jar"),
            f"--threads={threads}",
            "--decompile-generics=1",
            "--remove-synthetic=1",
            "--ignore-invalid-bytecode=1",
            "--verify-anonymous-classes=1",
            "--unsupported-exceptions=0",
            "--log-level=error",
            "--indent-string=    ",
            str(source_jar),
            str(src),
        ]
        s.spin()
        r = subprocess.run(cmd, capture_output=True, text=True)
        files = list(src.rglob("*.java"))
        if not files:
            s.fail(f"no .java files produced\n{r.stderr[-500:]}")
        s.ok(f"{len(files):,} files")

    def gradle(self):
        s = Step("Gradle project")
        t = ver_tuple(self.version or "")
        java_ver = 21 if t and t >= (1, 21) else 17 if t and t >= (1, 18) else 16 if t and t >= (1, 17) else 8

        jar_ref = self.remapped or self.jar
        libs    = self.out / "libs"
        libs.mkdir(exist_ok=True)
        shutil.copy2(jar_ref, libs / jar_ref.name)

        (self.out / "build.gradle").write_text(f"""\
plugins {{
    id 'java'
}}

group   = 'org.example'
version = '1.0-SNAPSHOT'

repositories {{
    mavenCentral()
}}

dependencies {{
    implementation files('libs/{jar_ref.name}')
    testImplementation 'org.junit.jupiter:junit-jupiter-api:5.10.0'
    testRuntimeOnly    'org.junit.jupiter:junit-jupiter-engine:5.10.0'
}}

test {{
    useJUnitPlatform()
}}

java {{
    toolchain {{
        languageVersion = JavaLanguageVersion.of({java_ver})
    }}
}}
""")
        (self.out / "src" / "main" / "resources").mkdir(parents=True, exist_ok=True)
        s.ok(f"Java {java_ver}")

    def run(self, header=True):
        if header:
            print(f"\nMC Decompiler  ->  {self.jar.name}\n")
        self.check_java()
        self.fetch_tools()
        self.detect_version()
        self.fetch_mappings()
        self.remap()
        self.decompile()
        self.gradle()
        print(f"  Done.  {self.out.resolve()}")



def parse_jar_args(raw):
    jars = []
    for token in raw:
        p = Path(token.strip().strip('"'))
        if p.suffix.lower() == ".jar" and p.exists():
            jars.append(p)
    return jars


if __name__ == "__main__":
    import os

    ap = argparse.ArgumentParser(description="Decompile Minecraft JARs and set up Gradle projects.")
    ap.add_argument("jars", nargs="*", help="One or more .jar files")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()

    jars = parse_jar_args(args.jars)
    if not jars:
        raw = input("JAR path(s): ").strip()
        jars = parse_jar_args([raw])
    if not jars:
        print("No valid .jar files found.")
        sys.exit(1)

    shared_manifest = None
    ok, failed = [], []

    multi = len(jars) > 1
    if multi:
        print(f"\nMC Decompiler  ->  {len(jars)} jars\n")

    for i, jar in enumerate(jars):
        if multi:
            print(f"[{i+1}/{len(jars)}]  {jar.name}")

        d = MCDecompiler(jar, args.output if not multi else None)
        if shared_manifest:
            d._manifest = shared_manifest

        try:
            d.run(header=not multi)
            shared_manifest = d._manifest
            ok.append(jar.name)
        except SystemExit:
            failed.append(jar.name)
            if multi:
                print("  Skipping due to error above.")
            continue

    if len(jars) > 1:
        print(f"\n{'='*44}")
        print(f"  {len(ok)}/{len(jars)} completed")
        for name in ok:
            print(f"  [OK]   {name}")
        for name in failed:
            print(f"  [FAIL] {name}")
        print()

    input("Press Enter to exit...")

