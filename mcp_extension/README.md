# RenderDoc MCP Server

一个 qrenderdoc UI 扩展：在 **Tools → RenderDoc MCP Server** 菜单中启动一个可见的服务窗口，
通过 MCP（Model Context Protocol，Streamable HTTP）向 AI Agent 暴露当前打开抓帧的分析能力。

当前版本 **v0.4.0**（broker 一机一实例 + 多 RenderDoc 实例注册路由）。

## 自测

```powershell
# 端到端自测（起独立 broker + 4 个假实例，54 项断言；勿占用正在使用的 30007）
python mcp_extension\test_client.py --selftest --port 31177
```

## 功能

服务窗口启动后，AI Agent 可以调用 20+ 个工具分析**当前在 RenderDoc UI 中打开的抓帧**：

| 工具 | 作用 |
|------|------|
| `get_capture_info` | 抓帧概览：文件路径、图形 API、帧号、资源数量、调试消息 |
| `get_current_state` | 当前 UI 状态：是否加载抓帧、当前选中的事件 |
| `list_drawcalls` | 列出 drawcall / dispatch / copy 动作树（扁平列表，带过滤） |
| `get_drawcall_params` | 指定 EID 的完整管线状态：输出目标、绑定 Shader 及反射、描述符、视口等 |
| `get_screenshot` | 纹理存 PNG 返回图片；`region` 裁剪 / `max_width` 缩放 / `save_to` 落盘防超限 |
| `get_pixel_history` | 单像素完整修改历史（诊断"这个像素为什么不对"） |
| `get_shader_disassembly` | 指定阶段 Shader 的反汇编 |
| `get_texture_list` / `get_buffer_list` | 资源列表（id、名字、尺寸、格式） |
| `get_buffer_data` | 读取 Buffer 原始字节（hexdump） |
| `get_buffer_regions` | Buffer 按值分类区域画像（0x00 / 0xCD 调试堆填充 / uniform / mixed） |
| `buffer_search` | Buffer 内模式搜索（u32 / f32 / hex，支持 stride 对齐） |
| `get_vertex_data` | stride 感知顶点解码（attrs 布局 → 每顶点命名值） |
| `get_event_usage` | 资源在整帧中的所有使用点（注：宿主持久映射 memcpy 写入不可见） |
| `get_debug_messages` | 图形 API 调试层消息 |
| `get_texture_stats` / `compare_regions` | 区域数值统计 / 双纹理区域对比 |
| `get_constant_buffer` | cbuffer 反射布局 + 内容导出 |

Broker 级工具（无需实例连接）：`list_instances`、`start_renderdoc`、
`shutdown_instance`、`list_capture_files`。

抓帧控制：`launch_app` → `trigger_capture` / `capture_and_open` →
`list_captures` / `open_capture` / `close_capture`。

### 手动抓帧分析（REQ-01/02/03）

被测程序由**人工手动启动并手动抓帧**（不经 `launch_app`）时，抓帧完全不可达于
连接层，但 .rdc 文件落在 `%TEMP%\RenderDoc\`，按
`<程序名>_<启动时间戳>_frame<帧号>.rdc` 命名。分析路径：

1. `list_capture_files` — 无需任何实例连接，枚举磁盘上的 .rdc
   （默认扫 `%TEMP%\RenderDoc`，支持 `dir` / `name_filter` / `max_results`）；
2. `open_capture_file(path, instance?)` — 在**已连接的实例内**加载/换帧
   （异步加载，`wait_seconds>0` 时等完为止），同一实例可连续换
   frame23414 → frame23423，避免一帧一个新进程；
3. 没有可用实例时 `start_renderdoc(args="<path.rdc>")` 拉起一个（用
   `SW_SHOWNOACTIVATE`，不抢焦点）。

`list_captures` / `open_capture` / `trigger_capture` 等在无连接时的报错都会
直接给出上述出路。注意：手动启动的 RenderDoc 只要装了本扩展就会**每 2 秒重试
注册**，broker 起来后自动出现在 `list_instances`（含 `last_seen` /
`state: active|stale`）。

约束说明：

- 服务**只监听 127.0.0.1**，无鉴权，仅限本机使用，不要暴露到局域网。
- 通过 Win32 命名互斥锁 + 端口绑定双保险保证**系统中只有一个服务实例**；
  第二个 qrenderdoc 实例打开窗口会提示"已在其他实例运行"。
- 关闭窗口或点击 Stop Server 即停止服务并释放互斥锁。

## 安装

要求：Windows，qrenderdoc ≥ 1.6（源码或发行版均可）。

```powershell
powershell -ExecutionPolicy Bypass -File D:\MyCodes\renderdoc\mcp_extension\install.ps1
```

脚本会把扩展复制到 `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp`。

然后：

1. 启动（或重启）qrenderdoc
2. **Tools → Manage Extensions** → 勾选 `RenderDoc MCP Server` 的 **Enabled**
3. 重启 qrenderdoc（启用扩展需要重启生效）

### 修改代码后更新

直接重新执行上面的 `install.ps1`，或在 Python Scripting 窗口点状态栏的扩展重载按钮。
更新后建议删除旧缓存：`%APPDATA%\qrenderdoc\extensions\renderdoc_mcp\__pycache__`。

## 使用

1. 打开一个抓帧
2. **Tools → RenderDoc MCP Server**，弹出服务窗口并自动启动，状态显示
   `RUNNING at http://127.0.0.1:30007/mcp`
3. 窗口内有实时请求日志，Stop Server / 关闭窗口可随时停止

## 在 Agent 中配置 MCP

### CodeBuddy IDE（HTTP 类型）

Settings → **MCP** 标签页 → **Add MCP**，加入：

```json
{
  "mcpServers": {
    "renderdoc": {
      "type": "http",
      "url": "http://127.0.0.1:30007/mcp",
      "description": "Inspect the capture open in RenderDoc UI"
    }
  }
}
```

保存后条目变绿即连接成功，可用 **Try to Run** 验证。之后在 Agent 对话中直接下任务，
例如：*“列出当前抓帧的所有 drawcall，找出最后一个输出到背板的 drawcall 并截图看看。”*

### 通用方案：mcp-remote 桥接（stdio → HTTP）

如果 Agent 只支持 stdio 类型（如 Claude Desktop），用 Node.js 的 `mcp-remote` 桥接：

```json
{
  "mcpServers": {
    "renderdoc": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:30007/mcp"],
      "description": "RenderDoc capture inspector"
    }
  }
}
```

### 自研 Agent（MCP Python SDK）

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://127.0.0.1:30007/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("list_drawcalls", {"max_results": 100})
            print(result)
```

## 多 RenderDoc 实例

全机只有一个 broker（互斥锁保证），**每个**运行本扩展的 qrenderdoc 实例都自动
注册进来，共享同一个 MCP 端点：

- `list_instances` 列出全部实例（id、pid、可执行文件、抓帧状态、`last_seen`、
  `state: active|stale`）；所有实例级工具都接受 `instance` 参数选择目标，
  只连一个实例时可省略。
- broker 不在时，第一个实例自举为 broker；broker 所在实例退出后其它实例自动接管。
- 实例在 broker 未启动期间会每 2 秒重试注册，broker 起来后自动出现。

也可以在启动 qrenderdoc 前先起独立 broker（推荐，Agent 无需依赖任何实例存活）：

```powershell
python D:\MyCodes\renderdoc\mcp_extension\rdmcp_broker.py --exe D:\MyCodes\renderdoc\x64\Development\qrenderdoc.exe
```

停止：`python rdmcp_broker.py --quit`。

## 故障排查

- **调试版 qrenderdoc 断在 `entry_points.cpp:290`**：这不是崩溃，是
  `DEBUGBREAK_ON_ERROR_LOG` 对任何 Error 日志的断点。真实原因看
  `%TEMP%\RenderDoc\RenderDoc_*.log` 中 `QTRD ... Error` 行。
- **端口被占用 / bind 失败**：另一实例已占用该端口（互斥锁在窗口状态里有提示），
  或残留进程未退出。用 `netstat -ano | findstr 30007` 排查。
- **Agent 连不上**：确认服务窗口状态为 RUNNING；`curl http://127.0.0.1:30007/mcp`
  应返回 405（正常，只接受 POST）。
- **工具调用报 "No capture is loaded"**：先在 RenderDoc UI 中打开一个抓帧，或
  用 `open_capture_file(path)` 从磁盘加载（`list_capture_files` 可枚举候选）。
- **手动抓的帧在 Agent 眼里"不存在"**：不经 `launch_app` 的抓帧不进连接列表，
  .rdc 落在 `%TEMP%\RenderDoc\` —— `list_capture_files` →
  `open_capture_file(path)`（详见"手动抓帧分析"一节）。
- **实例不在 `list_instances` 里**：确认该实例装了本扩展且已启用；实例每 2 秒
  重试注册，broker 起来后会自动出现（无需重启实例）。`state: stale` 表示
  `last_seen` 超过 120 秒没有信号（实例可能假死或被挂起）。

## 已知限制（内嵌 Python 3.6）

qrenderdoc 内嵌 Python 缺少 `_socket` C 扩展，且本构建无 PySide2 Python 绑定，
因此传输层用 **ctypes 直调 Winsock** 自实现 HTTP 服务器（见 `mcp_server.py`），
标准库的 `http.server` / `socket` 均不可用。给 `qrenderdoc.exe` 目录补上
Python 3.6 官方的 `_socket.pyd` + `select.pyd` 可恢复标准 socket 支持，但非必需。

## 文件结构

```
mcp_extension/
├── install.ps1                       # 一键安装脚本
├── README.md                         # 本文件
├── rdmcp_broker.py                   # 独立 broker（先于 qrenderdoc 启动）
├── test_client.py                    # 手动客户端 + 端到端自测
├── MCP_IMPROVEMENT_REQUIREMENTS*.md  # 需求文档（V2 已全部实现）
└── renderdoc_mcp/
    ├── extension.json                # 扩展清单
    ├── __init__.py                   # 菜单入口 + 服务窗口 + spoke 启动
    ├── mcp_server.py                 # MCP 协议 + Winsock HTTP 服务器
    ├── broker.py                     # broker：路由/注册/last_seen/文件枚举
    ├── spoke.py                      # 实例侧注册客户端（断线重试保活）
    ├── tool_defs.py                  # 工具定义（broker 与实例共享）
    ├── wire.py                       # broker<->实例 帧协议（ctypes winsock）
    ├── winprocess.py                 # 进程拉起（SW_SHOWNOACTIVATE 支持）
    └── rd_tools.py                   # 全部实例侧工具实现（replay 线程）
```
