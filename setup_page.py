"""Operator-only Windows deployment actions. Long jobs run outside Core's page lock."""
import asyncio
import ctypes
from contextlib import contextmanager
import io
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser

PACKAGE = Path(__file__).resolve().parent
SOURCE = "https://www.bilibili.com/opus/1233418589167419397"
PASSWORD = "bilibili-秋葉aaaki"
MARKER = "bilibili@秋葉aaaki.txt"
ACTIONS = {"inspect", "choose", "install_bundle", "pillow", "launch", "test_image"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def write_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    try:
        os.replace(f.name, path)
    finally:
        Path(f.name).unlink(missing_ok=True)


def powershell(script, *, env=None, timeout=15):
    import base64
    exe = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script
    cmd = [str(exe), "-NoProfile", "-STA", "-EncodedCommand",
           base64.b64encode(script.encode("utf-16le")).decode()]
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout, check=True, creationflags=NO_WINDOW).stdout.strip()


def local_choice(archive=False):
    if archive:
        script = """$d=New-Object System.Windows.Forms.OpenFileDialog
$d.Title='选择从秋葉aaaki 发布页下载的整合包';$d.Filter='秋叶整合包|*.7z;*.7z.001;*.zip'"""
    else:
        script = """$d=New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description='请选择秋叶整合包目录，或新安装的上级目录'"""
    prop = "FileName" if archive else "SelectedPath"
    path = powershell("Add-Type -AssemblyName System.Windows.Forms\n" + script +
                      "\nif($d.ShowDialog() -eq 'OK'){$d." + prop + "}", timeout=900)
    if not path:
        raise ValueError("已取消选择。")
    return Path(path).resolve(strict=True)


def launcher(root):
    if root and (root / MARKER).is_file() and (root / "python/python.exe").is_file() and (root / "ComfyUI/main.py").is_file():
        return next(iter(sorted(root.glob("*绘世*启动器*.exe"))), None)
    return None


def config(context):
    cfg = read_json(PACKAGE / "config.example.json")
    cfg.update(read_json(Path(context["config_path"]), {}))
    return cfg


def local_server(cfg):
    url = urlsplit(cfg["server"])
    if (url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1")
            or url.username is not None or url.fragment or url.query):
        raise ValueError("Windows 本机部署请在常规配置填写回环地址和秋叶启动器显示的端口。")
    return cfg["server"].rstrip("/"), url.port or (443 if url.scheme == "https" else 80)


def running_bundle(port):
    output = powershell("""
$rows=Get-NetTCPConnection -State Listen -LocalPort ([int]$env:AKI_INSPECT_PORT) -ErrorAction SilentlyContinue
foreach($owner in ($rows.OwningProcess | Sort-Object -Unique)){
  if($owner){(Get-Process -Id $owner -ErrorAction SilentlyContinue).Path}
}""", env={**os.environ, "AKI_INSPECT_PORT": str(port)})
    for line in output.splitlines():
        exe = Path(line)
        if exe.name.lower() in ("python.exe", "pythonw.exe") and launcher(exe.parent.parent):
            return exe.parent.parent.resolve()
    return None


def check_pillow():
    try:
        from PIL import Image, __version__
        if not 11 <= int(__version__.split(".")[0]) < 13:
            raise ValueError("版本应为 >=11,<13")
        buf = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buf, "PNG")
        buf.seek(0)
        with Image.open(buf) as img:
            img.load()
        return True, "Pillow " + __version__ + "，PNG 读写正常"
    except (ImportError, OSError, ValueError) as exc:
        return False, "Core 图片依赖需要修复 · " + str(exc)


def seven_zip():
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if key in os.environ:
            path = Path(os.environ[key]) / "7-Zip/7z.exe"
            if path.is_file():
                return str(path)
    return shutil.which("7z.exe")


def reserved(part):
    if hasattr(ntpath, "isreserved"):
        return ntpath.isreserved(part)
    return PureWindowsPath(part).is_reserved()


def members(listing):
    """Reject links, ADS, device names and parent paths before invoking 7-Zip."""
    roots, total, names = set(), 0, set()
    for block in re.split(r"\n\s*\n", listing.strip()):
        row = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" not in row:
            continue
        raw = row["Path"]
        path = PureWindowsPath(raw)
        attrs = row.get("Attributes", "")
        if (path.anchor or not path.parts or any(part in (".", "..") or part.endswith((" ", "."))
                or reserved(part) or re.search(r'[\x00-\x1f<>:"|?*]', part)
                for part in re.split(r"[/\\]", raw)) or "L" in attrs.split(" ")[0]
                or re.search(r"(?:^| )l[rwx-]{9}", attrs)
                or any(row.get(key) not in (None, "", "-") for key in
                       ("Symbolic Link", "Hard Link", "Reparse Point", "Alternate Stream"))):
            raise ValueError("压缩包包含不安全的路径或链接。")
        roots.add(path.parts[0])
        names.add(path.as_posix().casefold())
        total += int(row.get("Size") or 0)
    if len(roots) != 1:
        raise ValueError("请选择带有单一顶层目录的秋叶原版整合包。")
    root = roots.pop()
    prefix = root.casefold() + "/"
    for name in (MARKER, "python/python.exe", "ComfyUI/main.py"):
        if prefix + name.casefold() not in names:
            raise ValueError("压缩包缺少秋叶整合包文件：" + name)
    return root, total


def install_bundle(archive, parent):
    exe = seven_zip()
    if not exe:
        raise ValueError("解压加密整合包需要 7-Zip。请先使用页面上的「安装 7-Zip」，然后重试。")
    common = [exe, "-sccUTF-8", "-p" + PASSWORD]
    read = subprocess.run([*common, "l", "-slt", "-ba", "--", str(archive)],
                          capture_output=True, encoding="utf-8", errors="strict", creationflags=NO_WINDOW)
    if read.returncode:
        raise ValueError("无法读取整合包，请检查下载是否完整，分卷是否在同一目录。")
    name, size = members(read.stdout)
    target = parent / name
    if launcher(target):
        return target
    if target.exists():
        raise ValueError("目标目录已存在且未通过整合包检查，请选择其他上级目录。")
    if shutil.disk_usage(parent).free < size:
        raise ValueError("目标磁盘空间不足，解压至少需要 %.1f GiB。" % (size / 2**30))
    with tempfile.TemporaryDirectory(dir=parent, prefix=".aki-") as stage:
        result = subprocess.run([*common, "x", "-y", "-o" + stage, "--", str(archive)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        extracted = Path(stage) / name
        if result.returncode or not launcher(extracted):
            raise ValueError("解压或秋叶目录校验失败，请重新核对原版整合包。")
        extracted.rename(target)  # Windows refuses to replace an existing directory.
    return target


def workflow_check(cfg, client):
    graph = read_json(PACKAGE / (cfg["workflow_file"] or "workflow.json"))
    if not graph or not isinstance(graph, dict):
        raise ValueError("请选择 ComfyUI 导出的 API 工作流 JSON。")
    if not cfg["workflow_file"]:
        ckpts = client.get("models/checkpoints").raise_for_status().json()
        name = cfg["checkpoint"] or (ckpts[0] if len(ckpts) == 1 else "")
        if not name or name not in ckpts:
            raise ValueError("请在常规配置选择兼容的 checkpoint，或填写自己的 API 工作流。")
        graph["4"]["inputs"]["ckpt_name"] = name
    for field, kind in (("prompt_input", str), ("seed_input", int)):
        if field == "seed_input" and not cfg[field]:
            continue
        node, _, slot = cfg[field].partition(".")
        if type(graph.get(node, {}).get("inputs", {}).get(slot)) is not kind:
            raise ValueError("请核对工作流输入位置 " + cfg[field])
    if cfg["output_node"] not in graph:
        raise ValueError("工作流中找不到配置的输出节点。")
    from urllib.parse import quote
    schemas = {}
    for node in graph.values():
        cls = node["class_type"]
        if cls not in schemas:
            schemas[cls] = client.get("object_info/" + quote(cls, safe="")).raise_for_status().json().get(cls)
        if not schemas[cls]:
            raise ValueError("ComfyUI 缺少节点 " + cls)
        spec = schemas[cls]["input"]
        for key, val in node["inputs"].items():
            desc = (spec.get("required", {}) | spec.get("optional", {})).get(key)
            if not isinstance(val, str) or not desc:
                continue
            options = desc[0] if isinstance(desc[0], list) else None
            if desc[0] == "COMBO" and len(desc) > 1:
                options = desc[1].get("options")
            if isinstance(options, list) and val not in options:
                raise ValueError(f"{cls}.{key} 找不到选项或模型 {val}")
    return f"{len(graph)} 个节点及其可枚举选项已核对；权重加载需试图验证。"


def inspect(context, folder):
    import httpx
    cfg = config(context)
    saved = read_json(folder / "bundle.json")
    root = Path(saved) if saved else None
    rows = []
    def add(title, ok, detail):
        rows.append({"title": title, "ok": ok, "detail": str(detail)})
    good, note = check_pillow()
    add("Core 图片依赖", good, note)
    active = None
    try:
        server, port = local_server(cfg)
        active = running_bundle(port)
        if active:
            root = active
            write_json(folder / "bundle.json", str(active))
    except Exception as exc:
        add("本机地址", False, exc)
    found = bool(launcher(root))
    add("秋叶整合包", found, root if found else "请选择已有目录，或下载并安装整合包。")
    try:
        if not active:
            raise ValueError("尚未在配置的端口识别到秋叶包内的 Python。请用绘世启动器启动，再检查地址。")
        with httpx.Client(base_url=server + "/", trust_env=False, timeout=6) as client:
            stats = client.get("system_stats").raise_for_status().json()
            system = stats["system"]
            add("ComfyUI 服务", True, f"{server} · ComfyUI {system['comfyui_version']}")
            add("秋叶运行环境", True, f"Python {system['python_version'].split()[0]} · PyTorch {system['pytorch_version']}")
            devices = [str(d["name"]) for d in stats["devices"] if d.get("type") != "cpu"]
            add("推理设备", bool(devices), "、".join(devices) or "服务仅报告 CPU")
            try:
                add("工作流", True, workflow_check(cfg, client))
            except Exception as exc:
                add("工作流", False, exc)
    except Exception as exc:
        add("连接自检", False, exc)
    return {"rows": rows, "pillow": good, "bundle": found, "active": bool(active),
            "workflow": any(r["title"] == "工作流" and r["ok"] for r in rows)}


@contextmanager
def deployment_lock():
    """Serialise installers across Bot processes sharing the Windows user's environment."""
    import msvcrt
    path = Path(os.environ["LOCALAPPDATA"]) / "TelyinCore" / "comfyui-deploy.lock"
    path.parent.mkdir(exist_ok=True, parents=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError("另一个部署任务正在运行，请完成后重试。") from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def test_image(context, folder):
    import base64
    if not inspect(context, folder)["workflow"]:
        raise ValueError("请先完成秋叶服务和工作流检查。")
    module = runpy.run_path(str(PACKAGE / "index.py"))
    bot = context["bot_id"]
    ctx = dict(name="infinity-comfyui", packageDir=str(PACKAGE), root=str(PACKAGE.parent.parent),
               bot_id=bot, pluginSettingsBot=bot, pluginDataDir=context["data_dir"],
               pluginConfigPath=context["config_path"])
    plugin = module["create"](ctx)
    answer = json.loads(asyncio.run(plugin.generate("A small white cat sitting on a windowsill, soft daylight")))
    path = module["qq_outbox"](ctx) / answer["image"]
    from PIL import Image
    buf = io.BytesIO()
    with Image.open(path) as image:
        image.thumbnail((256, 256))
        image.convert("RGB").save(buf, "JPEG", quality=75)
    return {"path": str(path), "preview": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}


def execute(action, context, folder):
    if action == "inspect":
        return inspect(context, folder)
    if action == "choose":
        root = local_choice()
        if not launcher(root):
            raise ValueError("请选择包含绘世启动器、作者标记及 python 和 ComfyUI 子目录的整合包根目录。")
        write_json(folder / "bundle.json", str(root))
        return {"message": "已选用 " + str(root)}
    if action == "launch":
        _, port = local_server(config(context))
        if running_bundle(port):
            return {"message": "秋叶服务已经在运行。"}
        saved = read_json(folder / "bundle.json")
        exe = launcher(Path(saved)) if saved else None
        if not exe:
            raise ValueError("请先选择已有秋叶目录或安装整合包。")
        os.startfile(str(exe), cwd=str(exe.parent))
        return {"message": "已打开绘世启动器。启动 ComfyUI 后点击重新检查。"}
    if action == "seven_zip":
        if not seven_zip():
            winget = shutil.which("winget.exe")
            if not winget:
                webbrowser.open("https://www.7-zip.org/")
                return {"message": "已打开 7-Zip 官网，请安装 Windows 版后继续。"}
            subprocess.run([winget, "install", "--id", "7zip.7zip", "--exact", "--source", "winget",
                            "--accept-source-agreements", "--accept-package-agreements"],
                           timeout=600, check=True)
            if not seven_zip():
                raise ValueError("未找到 7z.exe，请完成 7-Zip 安装后重试。")
        return {"message": "7-Zip 已就绪。"}
    if action == "install_bundle":
        if not seven_zip():
            raise ValueError("请先点击「安装 7-Zip」。")
        archive, parent = local_choice(archive=True), local_choice()
        root = install_bundle(archive, parent)
        write_json(folder / "bundle.json", str(root))
        return {"message": "已校验并选用 " + str(root)}
    if action == "pillow":
        if not check_pillow()[0]:
            subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "install",
                            "--only-binary=:all:", "--no-deps", "--force-reinstall", "Pillow>=11,<13"],
                           timeout=300, check=True)
            # Recheck in a clean interpreter even after a failed/partial previous import.
            cmd = [sys.executable, "-c", "import runpy,sys; ok,msg=runpy.run_path(sys.argv[1])['check_pillow'](); print(msg); sys.exit(0 if ok else 1)", str(PACKAGE / "setup_page.py")]
            subprocess.run(cmd, check=True, timeout=20)
        return {"message": "Core 的 Pillow 已通过 PNG 读写检查，可以重新启用生图插件。"}
    if action == "test_image":
        return test_image(context, folder)
    raise ValueError("未知部署操作。")


def process_alive(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def status(folder):
    job = read_json(folder / "current.json")
    if job:
        result = read_json(folder / (job["id"] + ".json"))
        if result:
            job.update(result)
        elif not process_alive(job["pid"]):
            job.update(done=True, error="上次操作中断，结果未知。请重新检查，核对执行历史后再试图。")
    return {"job": job, "checks": read_json(folder / "checks.json"),
            "sample": read_json(folder / "sample.json")}


def handle(context, action, payload):
    if os.name != "nt":
        return {"error": "首次部署仅支持在 Windows 本机运行的 Core。"}
    if payload:
        return {"error": "部署动作不接受外部命令或路径参数。"}
    folder = Path(context["data_dir"]) / "setup"
    if action == "status":
        return status(folder)
    if action == "source":
        if not webbrowser.open(SOURCE):
            return {"error": "请在 Windows 浏览器打开 " + SOURCE}
        return {"message": "已在 Windows 默认浏览器打开秋葉aaaki 发布页。"}
    if action not in ACTIONS | {"seven_zip"}:
        return {"error": "未知部署操作。"}
    folder.mkdir(parents=True, exist_ok=True)
    previous = status(folder)["job"]
    if previous and not previous.get("done"):
        return {"error": "已有任务正在进行，请等待当前结果。"}
    import telyincore.plugins
    host = str(Path(telyincore.plugins.__file__).resolve().parents[1])
    job = uuid4().hex
    args = [sys.executable, str(PACKAGE / "setup_page.py"), host, context["bot_id"],
            context["config_path"], context["data_dir"], job, action]
    proc = subprocess.Popen(args, cwd=PACKAGE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    write_json(folder / "current.json", {"id": job, "action": action, "pid": proc.pid, "done": False})
    if previous:
        (folder / (previous["id"] + ".json")).unlink(missing_ok=True)
    return status(folder)


def worker(args):
    host, bot, cfg, runtime, job, action = args
    sys.path.insert(0, host)
    folder = Path(runtime) / "setup"
    context = {"bot_id": bot, "config_path": cfg, "data_dir": runtime}
    result = {"done": True}
    try:
        from contextlib import nullcontext
        lock = deployment_lock() if action in {"pillow", "seven_zip", "install_bundle", "test_image", "launch"} else nullcontext()
        from telyincore.plugin_package_lock import PackageLock
        with PackageLock(PACKAGE.parent.parent, "infinity-comfyui"), lock:
            value = execute(action, context, folder)
        if action == "inspect":
            write_json(folder / "checks.json", value)
        elif action == "test_image":
            write_json(folder / "sample.json", value)
        result["message"] = value.get("message", "自检完成" if action == "inspect" else "测试图已生成")
    except subprocess.TimeoutExpired:
        result["error"] = "操作超时，请检查网络、Windows 上的弹窗或启动器，然后重新检查。"
    except subprocess.CalledProcessError as exc:
        result["error"] = f"Windows 子进程返回 {exc.returncode}，请检查网络、安装权限及目标解释器。"
    except Exception as exc:
        result["error"] = str(exc) or type(exc).__name__
    write_json(folder / (job + ".json"), result)


if __name__ == "__main__":
    worker(sys.argv[1:])
