# ComfyUI 本机件：24 图槽参考编码（V3 工作流）

这里是 **仓库内副本**。真正生效的文件在 ComfyUI 目录（仓库外），本目录用于换机复现与守卫测试。
决策背景见 `docs/adr/0017-ref-slots-are-a-ui-limit.md`：9 是**节点 UI 上限**，不是模型上限；
2026-10-22 的真实运行需要 24 张素材，因此本机新建 24 槽包与 V3 工作流，**不改上游**
`ComfyUI-JZL-MiniMax-H3`，**V2 工作流原样保留作退路**。

## 文件清单

| 文件 | 作用 |
|---|---|
| `MiniMaxH3-Refs24/__init__.py` | 节点注册（3 个节点） |
| `MiniMaxH3-Refs24/nodes_refs24.py` | 24 图槽总线打包/解包 + 编码节点；编码实现**借上游**，不复制 |
| `generate_workflow_v3.py` | 从 V2 工作流生成 V3；换机可重跑 |
| `59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V3（24图槽）.json` | 生成结果副本（与 ComfyUI 目录那份一致） |
| `verify_nodes_import.py` | 导入级检查（不启动服务、不跑生成） |

## 安装（本机）

1. 把 `MiniMaxH3-Refs24/` 整个目录复制到 `<ComfyUI>/custom_nodes/MiniMaxH3-Refs24/`。
2. **不要修改** `custom_nodes/ComfyUI-JZL-MiniMax-H3/` 的任何文件（只读依赖）。
3. 重启 ComfyUI；节点菜单出现 `Refs24 - 🔗 24图槽参考总线（打包/解包）` 与
   `Refs24 - 🎬 MiniMax H3 参考编码（24图槽）`。

本包执行期从 ComfyUI 注册表按键 `JZL_MiniMaxH3ReferenceToVideo` 取上游编码类；
上游升级编码逻辑时本包自动跟随。上游若改键名，执行时会报出明确病因，而不是静默少喂图。

## 生成 / 更新 V3 工作流

只读 V2、另存 V3（脚本会拒绝把输出写成 V2 路径）：

```bash
python docs/comfyui/generate_workflow_v3.py \
  --v2 "<ComfyUI>/user/default/workflows/001-JZL基础工作流/15-MiniMax-H3/59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V2 .json" \
  --out "<同目录>/59-(多Lora改)MiniMax-H3 ref2va 多参生视频 V3（24图槽）.json"
```

生成规则：②参考资产子图把 9 张图扩到 24（新增 15 个 `LoadImage` + `Reroute`，
单独成组「🖼️启用图片9-23（V3 新增）」）；③参数子图换 24 槽解包节点与编码节点，
视频/音轨/音频/提示词/尺寸的槽位下标随图槽数量整体平移；模型、LoRA、采样、尺寸、
输出等参数与 V2 逐节点一致。

## 接线步骤（用户视角）

1. 打开 **V3（24图槽）**；V2 不要改，出问题时直接切回 V2。
2. 注入卡按 `<Picture 1>`…`<Picture N>` 列出映射：`<Picture N>` → `ref_image_{N-1}`
   （画布上第 N 个图槽）。
3. 在 ②子图里逐个替换 24 个 `LoadImage` 的图片；新增槽位默认占位 `1.png`。
4. 用不到的槽位留空即可：未接线的槽位是 `None`，编码节点会跳过，不会伪造参考。
5. 视频/音轨/音频槽位仍是各 3 个，保持 V2 的接法。

## 验证

```bash
# 1) 导入级检查（用 ComfyUI 自带 Python；不启动服务、不跑生成）
"<ComfyUI>/python_embeded/python.exe" docs/comfyui/verify_nodes_import.py \
  --node-dir "<ComfyUI>/custom_nodes/MiniMaxH3-Refs24"

# 2) 仓库守卫：24 槽/连线自洽/参数对齐/脚本可复现
pytest tests/test_comfyui_v3_artifacts.py -q
```

**未做真跑生成**：24 张参考图的实际耗时与显存峰值由人按票 #73 在 ComfyUI 里跑一次
（ADR 0017 的代价：`ref_image_size=match` 下每张约 1008 packed token，24 张约 +65%
序列，注意力超线性）。

## 风险与边界

- 上游键名是唯一耦合点：键在则编码逻辑自动跟随，键改名则执行期报错（不会静默）。
- 24 槽是本次需求的边界；素材再涨先量显存与耗时，再决定扩槽还是按段调度（P2）。
- 自定义总线类型 `MINIMAX_H3_REFS24_BUS` 故意不复用 `JZL_REF2VA_BUS`：
  18 槽与 24 槽总线互插只会静默少喂图，独立类型让这种接法直接接不上。
