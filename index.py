"""TelyinCore Infinity adapter for a configured ComfyUI API workflow."""
import asyncio
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import secrets
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
try:
    from PIL import Image
except ImportError:
    Image = None

from telyincore.image_policy import normalize_image
from telyincore.plugin_media_scope import qq_media_paths
from telyincore.plugins import PluginSetupRequired
from telyincore.tools import ToolDef, ToolTag


WAIT_SECONDS = 300
POLL_SECONDS = 1.5
MAX_DOWNLOAD = 32 * 1024 * 1024


def load_json(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 须为 JSON 对象")
    return value


def input_slot(graph, location, kind):
    node, sep, key = location.partition(".")
    inputs = graph.get(node, {}).get("inputs", {})
    if not sep or type(inputs.get(key)) is not kind:
        raise ValueError(f"工作流中找不到对应类型的输入 {location}，请核对节点 ID 与输入名")
    return inputs, key


def qq_outbox(ctx):
    # The QQ adapter validates the expected runtime against the host's Bot ID.
    if "pluginDataDir" in ctx:
        own = Path(ctx["pluginDataDir"])
        if own.parent.name != ctx["name"] or own.name != "runtime":
            raise ValueError("插件运行目录与宿主声明不符")
        qq_runtime = own.parent.parent / "core-infinity-qq" / "runtime"
        ctx = dict(ctx, pluginDataDir=str(qq_runtime))
    return qq_media_paths(ctx)[1]


def create(ctx):
    package = Path(ctx["packageDir"])
    cfg = load_json(package / "config.example.json")
    path = Path(ctx.get("pluginConfigPath", package / "config.json"))
    if path.is_file():
        cfg.update(load_json(path))

    server = cfg["server"].strip().rstrip("/")
    url = urlsplit(server)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None or url.query or url.fragment:
        raise ValueError("请填写有效的 ComfyUI HTTP(S) 地址")
    custom = cfg["workflow_file"].strip()
    if Image is None:
        raise PluginSetupRequired("请在运行 Core 的 Python 环境中安装 QQ 图片依赖 Pillow（>=11,<13），然后重新启用插件。")
    graph = load_json(package / (custom or "workflow.json"))
    if not graph or not all(isinstance(n, dict) and isinstance(n.get("class_type"), str)
                            and isinstance(n.get("inputs"), dict) for n in graph.values()):
        raise ValueError("请从 ComfyUI 导出 API 格式的工作流 JSON")

    positive, seed, output = (cfg[k].strip() for k in ("prompt_input", "seed_input", "output_node"))
    input_slot(graph, positive, str)
    if seed:
        input_slot(graph, seed, int)
    if output not in graph:
        raise ValueError("工作流中找不到配置的图片输出节点")
    qq_outbox(ctx)
    return ComfyUI(ctx.copy(), server, graph, positive, seed, output,
                   None if custom else cfg["checkpoint"].strip())


class ComfyUI:
    def __init__(self, ctx, server, graph, positive, seed, output, checkpoint):
        self.ctx = ctx
        self.server = server
        self.graph = graph
        self.positive = positive
        self.seed = seed
        self.output = output
        self.checkpoint = checkpoint  # None means the custom workflow owns its model loaders.

    def client(self):
        return httpx.AsyncClient(base_url=self.server + "/", trust_env=False,
                                 timeout=httpx.Timeout(30, connect=5))

    async def checkpoints(self, client):
        try:
            response = await client.get("models/checkpoints")
            response.raise_for_status()
            names = response.json()
        except httpx.ConnectError as exc:
            raise ValueError(f"无法连接 {self.server}。请先启动 Windows 上的 ComfyUI，并核对窗口中显示的地址和端口。") from exc
        except (ValueError, httpx.HTTPError) as exc:
            raise ValueError("无法读取 ComfyUI 的 checkpoints 列表，请核对服务地址并检查 /models/checkpoints。") from exc
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("ComfyUI 返回的 checkpoints 列表格式异常")
        return list(dict.fromkeys(names))

    async def models(self, query=""):
        async with self.client() as client:
            names = await self.checkpoints(client)
        matches = [name for name in names if query.casefold() in name.casefold()]
        return json.dumps({"checkpoints": matches[:30], "matched": len(matches),
                           "total": len(names)}, ensure_ascii=False)

    async def choose_checkpoint(self, client, requested):
        names = await self.checkpoints(client)
        if not names:
            raise ValueError("ComfyUI 已连接，但没有发现 checkpoint。请把与基础文生图工作流兼容的完整模型放进 ComfyUI 的 models/checkpoints，刷新模型列表后重试；分体模型请配置配套的 API 工作流。")
        selected = requested or self.checkpoint
        if selected:
            if selected not in names:
                raise ValueError("找不到指定的 checkpoint。用 comfyui_models 查询 ComfyUI 返回的完整名称后重试。")
            return selected
        if len(names) == 1:
            return names[0]
        raise ValueError("检测到多个 checkpoint，请让用户选择后通过 checkpoint 参数传入，或在插件配置中保存常用模型。可选名称：" +
                         json.dumps(names[:10], ensure_ascii=False) + "。完整列表可用 comfyui_models 按名称查找。")

    def tools(self):
        return [ToolDef(
            name="comfyui_generate",
            description="按画面描述用 ComfyUI 生成一张图片。QQ 会话请求画图时调用；成功后把回执中的 image 交给 qq_send_image，发回发起请求的私聊或群聊。",
            parameters={"type": "object", "properties": {
                "checkpoint": {"type": "string", "description": "可选。基础工作流所用的本机完整模型名称，多模型时按用户选择填写。自定义工作流请省略。"},
                "prompt": {"type": "string", "description": "给绘图模型的完整画面描述，保留用户指定的主体、风格与构图。"}},
                "required": ["prompt"], "additionalProperties": False},
            tags=(ToolTag.ACT,), handler=self.generate), ToolDef(
            name="comfyui_models",
            description="查询 ComfyUI 当前识别的 checkpoint 名称，可按名称过滤，每次返回前 30 项。",
            parameters={"type": "object", "properties": {"query": {"type": "string"}},
                        "additionalProperties": False}, tags=(ToolTag.READ,), handler=self.models)]

    async def generate(self, prompt, checkpoint=""):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请提供画面描述")
        graph = deepcopy(self.graph)
        inputs, key = input_slot(graph, self.positive, str)
        inputs[key] = prompt.strip()
        if self.seed:
            inputs, key = input_slot(graph, self.seed, int)
            inputs[key] = secrets.randbits(53)

        if checkpoint and self.checkpoint is None:
            raise ValueError("自定义 API 工作流已定义模型，请省略 checkpoint 参数或修改工作流。")
        async with self.client() as client:
            if self.checkpoint is not None:
                graph["4"]["inputs"]["ckpt_name"] = await self.choose_checkpoint(client, checkpoint.strip())
            try:
                response = await client.post("prompt", json={"prompt": graph, "client_id": str(uuid4())})
            except httpx.ConnectError as exc:
                raise ValueError("无法连接 ComfyUI，请检查服务地址和端口") from exc
            except httpx.RequestError as exc:
                raise ValueError("提交回执未知，请先查看 ComfyUI 队列，避免重复生图") from exc
            if 400 <= response.status_code < 500:
                raise ValueError(f"ComfyUI 拒绝工作流（HTTP {response.status_code}），请核对模型文件名、节点和输入")
            if not response.is_success:
                raise ValueError(f"提交回执未知（HTTP {response.status_code}），请核对 ComfyUI 队列")
            try:
                job = response.json()["prompt_id"]
                if not isinstance(job, str) or not job.strip():
                    raise ValueError()
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("提交回执缺少任务编号，请核对 ComfyUI 队列，避免重复生图") from exc

            try:
                async with asyncio.timeout(WAIT_SECONDS):
                    image = await self.wait_image(client, job)
                    raw, mime = await download_image(client, image)
                filename = save_image(qq_outbox(self.ctx), raw, mime)
            except TimeoutError as exc:
                raise ValueError(f"任务 {job} 已提交，等待出图超时，请在 ComfyUI 核对该任务") from exc
            except (ValueError, OSError, httpx.HTTPError) as exc:
                raise ValueError(f"任务 {job} 已提交，取图失败：{exc}") from exc
        return json.dumps({"image": filename, "next": "用 qq_send_image 把这张图发回原 QQ 会话。"}, ensure_ascii=False)

    async def wait_image(self, client, job):
        while True:
            response = await client.get("history/" + quote(job, safe=""))
            response.raise_for_status()
            history = response.json()
            if not isinstance(history, dict):
                raise ValueError("ComfyUI 历史响应格式异常")
            record = history.get(job)
            if record is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            if not isinstance(record, dict):
                raise ValueError("ComfyUI 任务记录格式异常")
            status = record.get("status", {})
            if status.get("status_str") == "error" or any(
                msg[0] in {"execution_error", "execution_interrupted"}
                for msg in status.get("messages", []) if isinstance(msg, list) and msg
            ):
                raise ValueError("ComfyUI 执行失败或被中断，请在执行历史查看节点错误")
            images = record.get("outputs", {}).get(self.output, {}).get("images", [])
            for item in images:
                if isinstance(item, dict) and item.get("type") == "output" and item.get("filename"):
                    return {key: item.get(key, "") for key in ("filename", "subfolder", "type")}
            raise ValueError(f"节点 {self.output} 未返回成品图，请核对 SaveImage 节点 ID")


async def download_image(client, params):
    chunks = bytearray()
    async with client.stream("GET", "view", params=params) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(65536):
            if len(chunks) + len(chunk) > MAX_DOWNLOAD:
                raise ValueError("成品图超过 32 MiB，请调低工作流分辨率")
            chunks.extend(chunk)
    # Some ComfyUI installations serve /view as application/octet-stream.
    with Image.open(BytesIO(chunks), formats=("PNG", "JPEG", "WEBP", "GIF")) as picture:
        mime = Image.MIME[picture.format]
    return normalize_image(bytes(chunks), mime)


def save_image(outbox, raw, mime):
    outbox.mkdir(parents=True, exist_ok=True)
    filename = f"comfyui-{uuid4().hex}{'.jpg' if mime == 'image/jpeg' else '.png'}"
    tmp = NamedTemporaryFile(dir=outbox, delete=False, suffix=".part")
    temp = Path(tmp.name)
    try:
        with tmp:
            tmp.write(raw)
        temp.replace(outbox / filename)
    finally:
        temp.unlink(missing_ok=True)
    return filename
