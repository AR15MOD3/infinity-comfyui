"""Integration tests against the installed TelyinCore host and bundled QQ adapter."""
import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from telyincore import market, plugins
from plugin_scope_host import make_plugin_scope

NAME = "infinity-comfyui"
QQ = "core-infinity-qq"
PACKAGE = Path(__file__).resolve().parents[1]
CORE = Path(plugins.__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "core"
    root.mkdir()
    scope = make_plugin_scope(root, "test-bot", "test-bot")
    assert market.install_from(root, PACKAGE)["ok"]
    try:
        yield root, scope
    finally:
        plugins.shutdown_root(root)


def enable(root, scope, name, **kwargs):
    result = plugins.switch(root, name, on=True, scope=scope, host={"bot_id": scope.bot} | kwargs)
    assert next(p for p in result["plugins"] if p["name"] == name)["loaded"], result
    tools = plugins.gathered(root, scope=scope)
    assert not tools["said"]
    return {t.name: t.handler for t in tools["specs"]}


def mock_comfy(monkeypatch, callback):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(callback), **kw))


@pytest.mark.parametrize(("scope_name", "target"), [("private", "123"), ("group", "456")])
def test_generate_then_send(host, monkeypatch, scope_name, target):
    root, scope = host
    scope.save_config(NAME, {"server": "http://windows.test:8188"})
    assert market.install_from(root, CORE / "builtin/infinity" / QQ)["ok"]
    scope.save_config(QQ, {"httpUrl":"http://127.0.0.1:3000", "privates":["123"], "groups":["456"]})
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "orange").save(image, "PNG")
    calls = []
    def comfy(request):
        calls.append(request.url.path)
        assert request.url.host == "windows.test"
        if request.url.path == "/models/checkpoints":
            return httpx.Response(200, json=["测试 模型/任选.safetensors"])
        if request.url.path == "/prompt":
            graph = json.loads(request.content)["prompt"]
            assert graph["6"]["inputs"]["text"] == "月光下的白猫"
            assert graph["4"]["inputs"]["ckpt_name"] == "测试 模型/任选.safetensors"
            return httpx.Response(200, json={"prompt_id": "one"})
        if request.url.path == "/history/one":
            if calls.count("/history/one") == 1:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"one":{"outputs":{"9":{"images":[
                {"filename": "猫 1.png", "subfolder":"TelyinCore/QQ","type":"output"}]}}}})
        assert request.url.path == "/view"
        assert request.url.params["filename"] == "猫 1.png"
        return httpx.Response(200, content=image.getvalue())
    mock_comfy(monkeypatch, comfy)
    posted = []
    def onebot(request):
        payload = json.loads(request.content)
        assert request.url.path == f"/send_{scope_name}_msg"
        assert payload["user_id" if scope_name=="private" else "group_id"] == int(target)
        file = payload["message"][0]["data"]["file"]
        assert base64.b64decode(file.removeprefix("base64://")) == image.getvalue()
        posted.append(payload)
        return httpx.Response(200, json={"status":"ok", "retcode":0, "data":{"message_id":42}})
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(onebot), **kw))
    event = SimpleNamespace(source="qq", origin="external", meta={"conversation": f"{scope_name}:{target}", "message_id":"8"})
    enable(root, scope, QQ, active_events=lambda: (event,), qq_config_path=str(scope.config_path(QQ)))
    tools = enable(root, scope, NAME)
    receipt = json.loads(asyncio.run(tools["comfyui_generate"](prompt="月光下的白猫")))
    assert not posted
    assert (scope.root / QQ / "runtime/media/outbox" / receipt["image"]).is_file()
    assert "message_id=42" in tools["qq_send_image"](scope_name, target, receipt["image"])
    assert len(posted) == 1
    assert calls == ["/models/checkpoints", "/prompt", "/history/one", "/history/one", "/view"]


@pytest.mark.parametrize("failure", ["rejected", "execution", "invalid_image", "missing_output", "lost_receipt"])
def test_failure_never_resubmits_or_publishes(host, monkeypatch, failure):
    root, scope = host
    submissions = []
    def respond(request):
        if request.url.path == "/models/checkpoints":
            return httpx.Response(200, json=["local.ckpt"])
        if request.url.path == "/prompt":
            submissions.append(request)
            if failure == "lost_receipt":
                raise httpx.ReadTimeout("test")
            if failure == "rejected":
                return httpx.Response(400)
            return httpx.Response(200, json={"prompt_id":"one"})
        if request.url.path == "/view":
            return httpx.Response(200, content=b"not-an-image")
        output = {"images":[{"filename":"bad.png","type":"output"}]}
        return httpx.Response(200, json={"one":{"outputs":{} if failure=="missing_output" else {"9":output},
                                                "status":{"status_str":"error" if failure=="execution" else "success"}}})
    mock_comfy(monkeypatch, respond)
    tools = enable(root, scope, NAME)
    with pytest.raises(ValueError):
        asyncio.run(tools["comfyui_generate"]("draw a cat"))
    assert len(submissions) == 1
    assert not (scope.root / QQ / "runtime/media/outbox").exists()


def test_custom_workflow_and_seed_preservation(host, monkeypatch):
    root, scope = host
    graph = {"10": {"class_type":"Text", "inputs":{"value":"template"}},
             "20": {"class_type":"Sampler","inputs":{"seed":123}},
             "30": {"class_type":"SaveImage","inputs":{}}}
    custom = root / "custom.json"
    custom.write_text(json.dumps(graph), encoding="utf-8")
    scope.save_config(NAME, {"workflow_file":str(custom), "prompt_input":"10.value",
                             "seed_input":"", "output_node":"30"})
    captured = []
    def respond(request):
        captured.append(json.loads(request.content)["prompt"])
        return httpx.Response(422)
    mock_comfy(monkeypatch, respond)
    tool = enable(root, scope, NAME)["comfyui_generate"]
    with pytest.raises(ValueError, match="422"):
        asyncio.run(tool("first"))
    assert captured[0]["10"]["inputs"]["value"] == "first"
    assert captured[0]["20"]["inputs"]["seed"] == 123
    assert json.loads(custom.read_text()) == graph


def test_timeout_points_to_existing_job(host, monkeypatch):
    import sys
    root, scope = host
    tool = enable(root, scope, NAME)["comfyui_generate"]
    monkeypatch.setattr(sys.modules[f"core_infinity_{NAME}"], "WAIT_SECONDS", 0.02)
    calls = []
    def respond(request):
        if request.url.path == "/models/checkpoints":
            return httpx.Response(200, json=["local.ckpt"])
        calls.append(request.method)
        return httpx.Response(200, json={"prompt_id":"queued-job"} if request.method=="POST" else {})
    mock_comfy(monkeypatch, respond)
    with pytest.raises(ValueError, match="queued-job.*超时"):
        asyncio.run(tool("test"))
    assert calls.count("POST") == 1


def test_configuration_save_conflict_and_reload(host):
    from plugin_config import PluginConfig
    root, scope = host

    def reload_plugin(name):
        plugins.put_out(root, name, scope=scope)
        enable(root, scope, name)

    config = PluginConfig(root, lambda: plugins.installed(root, scope=scope),
                          reload_plugin, scope=scope)
    initial = config.read(NAME)
    change = {"revision": initial["revision"], "changes": [
        {"path": ["server"], "value": "http://test.invalid:18188"}]}
    assert config.save(NAME, change)["ok"]
    assert not plugins.gathered(root, scope=scope)["specs"]
    with pytest.raises(ValueError, match="修改"):
        config.save(NAME, change)
    assert next(f["value"] for f in config.read(NAME)["fields"]
                if f["path"] == ["server"]) == "http://test.invalid:18188"
    tool = enable(root, scope, NAME)["comfyui_generate"]
    assert config.save(NAME, {"revision": config.read(NAME)["revision"], "changes": []})["ok"]
    with pytest.raises(RuntimeError):
        asyncio.run(tool("旧实例"))
    assert {t.name for t in plugins.gathered(root, scope=scope)["specs"]} == {"comfyui_generate", "comfyui_models"}
    plugins.switch(root, NAME, on=False, host={"bot_id":scope.bot}, scope=scope)
    assert not plugins.gathered(root, scope=scope)["specs"]


def test_cancel_after_submission(host, monkeypatch):
    root, scope = host
    tool = enable(root, scope, NAME)["comfyui_generate"]
    received = asyncio.Event()
    calls = []

    async def respond(request):
        if request.url.path == "/models/checkpoints":
            return httpx.Response(200, json=["local.ckpt"])
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"prompt_id": "accepted"})
        received.set()
        await asyncio.Event().wait()

    async def cancel():
        task = asyncio.create_task(tool("雨天"))
        await asyncio.wait_for(received.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    mock_comfy(monkeypatch, respond)
    asyncio.run(cancel())
    assert calls == ["POST", "GET"]
    assert not (scope.root / QQ / "runtime/media/outbox").exists()


def test_upgrade_preserves_explicit_workflow_and_bot_scope(host):
    root, scope = host
    other = make_plugin_scope(root, "second-bot", scope.bot)
    scope.save_config(NAME, {"workflow_file":"anima.json"})
    existing = plugins.carry(root, NAME, host={"bot_id":scope.bot}, scope=scope)
    assert existing["ok"] and existing["got"].checkpoint is None
    assert existing["got"].graph["1"]["class_type"] == "UNETLoader"
    plugins.put_out(root, NAME, scope=scope)
    second = plugins.carry(root, NAME, host={"bot_id":other.bot}, scope=other)
    assert second["ok"] and second["got"].checkpoint == ""
    assert second["got"].graph["4"]["class_type"] == "CheckpointLoaderSimple"
    import sys
    module = sys.modules[f"core_infinity_{NAME}"]
    ctx = second["got"].ctx
    assert module.qq_outbox(ctx) == other.root / QQ / "runtime/media/outbox"
    with pytest.raises(ValueError):
        module.qq_outbox(dict(ctx, bot_id=scope.bot))
    assert scope.read_config(NAME)["workflow_file"] == "anima.json"

@pytest.mark.parametrize(("names", "wanted", "message"), [
    ([], "", "models/checkpoints"),
    (["alpha.ckpt", "beta.ckpt"], "", "多个 checkpoint"),
    (["beta.ckpt"], "missing.ckpt", "找不到指定"),
])
def test_first_run_stops_before_queue(host, monkeypatch, names, wanted, message):
    root, scope = host
    def respond(request):
        assert request.method == "GET" and request.url.path == "/models/checkpoints"
        return httpx.Response(200, json=names)
    mock_comfy(monkeypatch, respond)
    tools = enable(root, scope, NAME)
    assert json.loads(asyncio.run(tools["comfyui_models"]()))["checkpoints"] == names
    with pytest.raises(ValueError, match=message):
        asyncio.run(tools["comfyui_generate"]("雨", checkpoint=wanted))


@pytest.mark.parametrize(("wanted", "expected"), [("", "first.ckpt"), ("第二个.ckpt", "第二个.ckpt")])
def test_selected_model_uses_exact_server_name(host, monkeypatch, wanted, expected):
    root, scope = host
    scope.save_config(NAME, {"checkpoint": "first.ckpt"})
    submitted = []
    def respond(request):
        if request.url.path == "/models/checkpoints":
            return httpx.Response(200, json=["first.ckpt", "第二个.ckpt"])
        submitted.append(json.loads(request.content)["prompt"]["4"]["inputs"]["ckpt_name"])
        return httpx.Response(422)
    mock_comfy(monkeypatch, respond)
    tools = enable(root, scope, NAME)
    assert json.loads(asyncio.run(tools["comfyui_models"]("第二")))["checkpoints"] == ["第二个.ckpt"]
    with pytest.raises(ValueError, match="422"):
        asyncio.run(tools["comfyui_generate"]("夜空", checkpoint=wanted))
    assert submitted == [expected]
