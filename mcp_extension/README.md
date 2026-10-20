# RenderDoc MCP Server

一个 qrenderdoc UI 扩展：在 **Tools → RenderDoc MCP Server** 菜单中启动一个可见的服务窗口，
通过 MCP（Model Context Protocol，Streamable HTTP）向 AI Agent 暴露当前打开抓帧的分析能力。

## 功能

服务窗口启动后，AI Agent 可以调用 12 个工具分析**当前在 RenderDoc UI 中打开的抓帧**：

| 工具 | 作用 |
|------|------|
| `get_capture_info` | 抓帧概览：文件路径、图形 API、帧号、资源数量、调试消息 |
| `get_current_state` | 当前 UI 状态：是否加载抓帧、当前选中的事件 |
| `list_drawcalls` | 列出 drawcall / dispatch / copy 动作树（扁平列表） |
| `get_drawcall_params` | 指定 EID 的完整管线状态：输出目标、绑定 Shader 及反射、描述符、视口等 |
| `get_screenshot` | 将纹理存为 PNG 并返回图片，供 Agent 视觉检查渲染结果 |
| `get_pixel_history` | 单像素完整修改历史（诊断"这个像素为什么不对"） |
| `get_shader_disassembly` | 指定阶段 Shader 的反汇编 |
| `get_texture_list` / `get_buffer_list` | 资源列表（id、名字、尺寸、格式） |
| `get_buffer_data` | 读取 Buffer 原始字节（hexdump） |
| `get_event_usage` | 资源在整帧中的所有使用点 |
| `get_debug_messages` | 图形 API 调试层消息 |

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

默认端口 `30007`，全系统仅一个实例能占用。若想让 Agent 同时连接多个 RenderDoc
（例如对比两份抓帧），给不同实例配不同端口后分别连接：

```powershell
# 启动第二个 qrenderdoc 前设置端口
$env:RENDERDOC_MCP_PORT = "30008"
& D:\MyCodes\renderdoc\x64\Development\qrenderdoc.exe
```

Agent 侧再配一个 `renderdoc2` 指向 `http://127.0.0.1:30008/mcp`。

## 故障排查

- **调试版 qrenderdoc 断在 `entry_points.cpp:290`**：这不是崩溃，是
  `DEBUGBREAK_ON_ERROR_LOG` 对任何 Error 日志的断点。真实原因看
  `%TEMP%\RenderDoc\RenderDoc_*.log` 中 `QTRD ... Error` 行。
- **端口被占用 / bind 失败**：另一实例已占用该端口（互斥锁在窗口状态里有提示），
  或残留进程未退出。用 `netstat -ano | findstr 30007` 排查。
- **Agent 连不上**：确认服务窗口状态为 RUNNING；`curl http://127.0.0.1:30007/mcp`
  应返回 405（正常，只接受 POST）。
- **工具调用报 "No capture is loaded"**：先在 RenderDoc UI 中打开一个抓帧。

## 已知限制（内嵌 Python 3.6）

qrenderdoc 内嵌 Python 缺少 `_socket` C 扩展，且本构建无 PySide2 Python 绑定，
因此传输层用 **ctypes 直调 Winsock** 自实现 HTTP 服务器（见 `mcp_server.py`），
标准库的 `http.server` / `socket` 均不可用。给 `qrenderdoc.exe` 目录补上
Python 3.6 官方的 `_socket.pyd` + `select.pyd` 可恢复标准 socket 支持，但非必需。

## 文件结构

```
mcp_extension/
├── install.ps1                  # 一键安装脚本
├── README.md                    # 本文件
└── renderdoc_mcp/
    ├── extension.json           # 扩展清单
    ├── __init__.py              # 菜单入口 + 服务窗口 + 互斥锁
    ├── mcp_server.py            # MCP 协议 + Winsock HTTP 服务器
    └── rd_tools.py              # 12 个抓帧分析工具（经 BlockInvoke 执行）
```
