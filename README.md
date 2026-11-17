# ComfyUI 生图

ComfyUI 生图是 TelyinCore 的独立附属插件，为 QQ 会话增加画图功能。它依赖 Core 运行。在同一个 Bot 上，「QQ 连接」（`core-infinity-qq`）负责接收私聊或群聊消息，并通过 `qq_send_image` 回图。这个插件提供 `comfyui_generate`，把画面描述交给你自己的 ComfyUI。模型在 ComfyUI 所在机器上运行。

## 安装与连接

1. 在 Core 的插件市场刷新列表，搜索 `infinity-comfyui`，选择来源为 `AR15MOD3/infinity-comfyui` 的条目并安装。在已装插件中可以看到「ComfyUI 生图」，初始为停用。仅安装时无需启动 ComfyUI、准备模型或登录 QQ。离线时解压源码，在插件管理中从包含 `package.json` 的本机目录安装。
2. 准备在 QQ 使用时，在目标 Bot 上配置并启用随 Core 提供的「QQ 连接」，确认能收到目标私聊或群聊的消息。市场安装生图插件不会代为配置 QQ。
3. 在同一 Bot 的「ComfyUI 生图」配置页填写服务地址，并选用下文的一种工作流。保存后启用插件。首次启用时如果尚未选工作流或填写 checkpoint，Core 会提示待配置。ComfyUI 服务和模型就绪后，在 QQ 里说「帮我画一只趴在窗台上的白猫」即可发起请求。
4. TelyinCore 与 ComfyUI 都在 Windows 上时，保留默认服务地址 `http://127.0.0.1:8188`。如果启动器显示其他端口，以实际地址为准。
5. TelyinCore 在 Linux、ComfyUI 在 Windows 时，在生图插件的配置页填入 Linux 可以访问的地址，然后保存。如果 Windows 服务已监听局域网，可填 `http://<Windows 局域网 IP>:8188`。如果只监听回环地址且已有 SSH 登录，可在 Linux 执行下列命令并保持连接，插件地址填 `http://127.0.0.1:18188`。

```bash
ssh -N -L 127.0.0.1:18188:127.0.0.1:8188 用户名@Windows主机
```

## 工作流

新安装默认留空，可使用自己的 API 工作流，也可为内置 SD 1.5 工作流填写兼容的 checkpoint。由 0.1.0 升级且原先依赖默认 Anima 预设的用户，请在「API 工作流」中明确填写 `anima.json`。

可选示例 `anima.json` 使用标准节点，每次生成一张 768 × 768 图片，12 步采样，随机种子。要使用这个示例，在「API 工作流」中填写 `anima.json`，并在 ComfyUI 准备以下三个文件。

| ComfyUI 模型目录 | 文件 |
| --- | --- |
| models/unet | miaomiaoHarem_anima15.safetensors |
| models/text_encoders | qwen_3_06b_base.safetensors |
| models/vae | qwen_image_vae.safetensors |

要沿用自己的采样、LoRA 或尺寸，在 ComfyUI 中先运行工作流，再导出 **API 格式** JSON，将文件放到 TelyinCore 所在机器。在配置页填写「API 工作流」的文件路径、正向提示词位置（如 `6.text`）、随机种子位置（如 `3.seed`，留空保留原值）和成品输出节点 ID（如 `9`）。提示词位置应指向可直接写文本的输入，种子位置应指向整数输入。插件读取指定节点返回的第一张 output 图片。API 调用方式见 [ComfyUI 官方示例](https://github.com/Comfy-Org/ComfyUI/blob/master/script_examples/websockets_api_example.py)。

使用 SD 1.5 checkpoint 时，清空「API 工作流」，填写 ComfyUI 下拉框中模型的完整文件名。配套的基础工作流使用 512 × 512，三个节点位置沿用默认值。

## 运行环境

TelyinCore 需要支持 Infinity API 1、异步工具和当前 Bot 的 QQ 发图目录。已在 Windows（Python 3.11.9）和 Linux（Python 3.14）上使用 TelyinCore beta.0.5.3 系列代码、httpx 0.28.1 和 Pillow 12.3.0 进行集成测试。插件使用 Python 3.11 及以上版本提供的异步超时接口。请在宿主 Python 环境中准备 httpx（>=0.27）和 Pillow（>=11,<13），并启用提供 `qq_send_image` 的 QQ 连接。

推理在独立运行的 ComfyUI 服务上完成。可以从 [ComfyUI 官方仓库](https://github.com/Comfy-Org/ComfyUI) 获取运行时。请按模型发布方的说明和授权准备模型，或使用自己已有的 API 工作流。已配置的服务直接复用现有权重，插件额外下载的权重为 0 字节。新装服务所需磁盘空间取决于模型版本与精度，还应为每次生成的原图和发送副本预留空间。

## 停用、升级与数据

- 在目标 Bot 的插件页停用「ComfyUI 生图」后，宿主撤下绘图工具。ComfyUI 服务由其操作者管理，已经提交的任务仍可在服务端执行历史中核对。
- 更新前先备份配置和自定义工作流，等待当前调用结束。宿主会拒绝覆盖同名安装目录。使用宿主的升级入口，或停用并卸载旧程序后再安装新标签，重新核对配置并启用。自定义工作流建议存放在插件安装目录之外。
- 使用档案隔离的宿主将配置保存在当前 Bot 的 Infinity 插件目录，QQ 发送副本保存在该 Bot 的 `infinity/plugins/core-infinity-qq/runtime/media/outbox/`。旧式宿主使用插件目录的 `config.json` 和 Core 的 `data/media/outbox/`。升级旧式安装前须单独备份该配置文件。
- 原图在 ComfyUI 的输出目录中。卸载插件会移除共享安装副本；已配置的模型、ComfyUI 原图和 QQ outbox 的现有图片由各自的操作者管理。

## 运行与排查

- 排队和生成最多等待五分钟。已取得任务编号时，取图错误会附上该编号，可以在 ComfyUI 执行历史核对。服务端可能在等待超时后继续生成。
- ComfyUI 的原图按工作流的 SaveImage 配置保存。用于发送的副本按 QQ 图片策略处理，写入当前 Bot 的 QQ outbox。普通 HTTP 地址即可跨机取图，双方无需共享文件夹。
- 如果启用时提示缺少 Pillow，在运行 Core 的 Python 环境中准备该依赖。例如在 Windows 的 Core 源码目录使用 PowerShell 执行 `.\.venv\Scripts\python.exe -m pip install "Pillow>=11,<13"`，然后重新启用。
- 模型拒绝加载时，检查对应文件是否能在 ComfyUI 节点的下拉框选到。生图已完成但 QQ 未返回消息编号时，先检查原会话的发送权限和 QQ 连接状态。


## 测试与反馈

使用 TelyinCore 的 Python 环境，在本仓库根目录将 `PYTHONPATH` 指向宿主的 `core` 源码目录，然后运行 `python -m pytest tests`。测试在临时 Bot 档案中运行，使用真实插件装载器与 QQ 插件，ComfyUI 和 OneBot 的网络响应由测试提供。两端分别通过 12 项测试，覆盖私聊与群聊回执、配置保存与冲突、重载、Bot 隔离、取消、超时和失败处理。实际 GPU 出图与真实 QQ 收图仍待验证。

本项目是独立开发的 TelyinCore 附属插件，运行时依赖宿主提供的工具与发图能力。仓库仅分发插件源码、工作流参数、文档与测试。TelyinCore、QQ 连接、ComfyUI、依赖及模型各自适用其授权。本插件由 AR15MOD3 发布与维护，借助 Codex 开发。反馈时请提供插件与宿主版本、复现步骤和已脱敏的错误信息。[提交问题](https://github.com/AR15MOD3/infinity-comfyui/issues)

## 许可证

本插件以 MIT 许可证发布，可用于商业项目。复制或再分发时须保留版权声明和许可声明。[许可证全文](LICENSE)
