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
from PIL import Image

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
    if not custom and not cfg["checkpoint"].strip():
        raise PluginSetupRequired("请在「ComfyUI 生图」配置里填写基础模型文件名，或指定自定义 API 工作流。")
    graph = load_json(package / (custom or "workflow.json"))
    if not graph or not all(isinstance(n, dict) and isinstance(n.get("class_type"), str)
                            and isinstance(n.get("inputs"), dict) for n in graph.values()):
        raise ValueError("请从 ComfyUI 导出 API 格式的工作流 JSON")
    if not custom:
        graph["4"]["inputs"]["ckpt_name"] = cfg["checkpoint"].strip()

    positive, seed, output = (cfg[k].strip() for k in ("prompt_input", "seed_input", "output_node"))
    input_slot(graph, positive, str)
    if seed:
        input_slot(graph, seed, int)
    if output not in graph:
        raise ValueError("工作流中找不到配置的图片输出节点")
    qq_outbox(ctx)
    return ComfyUI(ctx.copy(), server, graph, positive, seed, output)


class ComfyUI:
    def __init__(self, ctx, server, graph, positive, seed, output):
        self.ctx = ctx
        self.server = server
        self.graph = graph
        self.positive = positive
        self.seed = seed
        self.output = output

    def tools(self):
        return [ToolDef(
            name="comfyui_generate",
            description="按画面描述用 ComfyUI 生成一张图片。QQ 会话请求画图时调用；成功后把回执中的 image 交给 qq_send_image，发回发起请求的私聊或群聊。",
            parameters={"type": "object", "properties": {
                "prompt": {"type": "string", "description": "给绘图模型的完整画面描述，保留用户指定的主体、风格与构图。"}},
                "required": ["prompt"], "additionalProperties": False},
            tags=(ToolTag.ACT,), handler=self.generate)]

    async def generate(self, prompt):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请提供画面描述")
        graph = deepcopy(self.graph)
        inputs, key = input_slot(graph, self.positive, str)
        inputs[key] = prompt.strip()
        if self.seed:
            inputs, key = input_slot(graph, self.seed, int)
            inputs[key] = secrets.randbits(53)

        async with httpx.AsyncClient(base_url=self.server + "/", trust_env=False,
                                     timeout=httpx.Timeout(30, connect=5)) as client:
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
