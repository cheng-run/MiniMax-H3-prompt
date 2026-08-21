# ComfyUI 工作流只读盘点方案与阶段记录

## 当前阶段

阶段 2：真实 ComfyUI 工作流盘点与候选 Profile 核验。

本次整合 Explore 结果后，修正了前一版盘点范围：不能只扫描工作流根目录。实际根目录下存在嵌套子目录，必须递归扫描全部 JSON。

## 盘点范围

- 根目录：`D:\Comfyui\ComfyUI\user\default\workflows`
- 扫描方式：递归遍历全部 `*.json`
- 当前发现：125 个 JSON
- 目标相关初步命中：30 个
- 盘点产物：[workflow-inventory.json](../docs/workflow-inventory.json)
- 生成脚本：[\_build_workflow_inventory.py](../docs/_build_workflow_inventory.py)

## 每个文件必须记录

- 相对路径和绝对路径
- 文件大小
- 修改时间
- 原始字节 SHA-256（不重新序列化）
- UTF-8/JSON 解析结果
- UI/API 格式
- 节点数量、节点 ID、节点类型
- UI links 数量或 API 引用数量
- 嵌套 subgraph 数量（如有）
- 模型文件提示
- 初步 capability 和 classification
- 静态解析错误

文件名和目录只能用于初步分类，不能单独证明工作流能力。最终能力必须依据节点、连接、实际 input/output 和人工确认。

## 首批目标集合

### Z-Image

- `001-JZL基础工作流/01-Z-image/01-Z_image_Turbo-文生图.json` → `zimage_t2i_v1`
- `001-JZL基础工作流/01-Z-image/02-Z_image_Turbo-图生图.json` → `zimage_i2i_v1`
- `001-JZL基础工作流/01-Z-image/03-Z-Image-Turbo-多模态控制生图.json` → `zimage_control_image_v1`
- `003-JZL大艺术家系列/03-漫剧人物创造编辑01/54-Z-Image-Turbo-角色三视图生成工作流.json` → `zimage_character_sheet_v1`，但必须先验证四图输入、subgraph/连接和输出语义。

### Flux.2

- `001-JZL基础工作流/02-Flux-2/11-Flux2-文生图.json` → `flux2_t2i_v1`
- `001-JZL基础工作流/02-Flux-2/05-Flux2-01单图编辑.json` → `flux2_single_edit_v1`
- `001-JZL基础工作流/02-Flux-2/06-Flux2-02双图编辑.json` → `flux2_dual_edit_v1`
- 三图编辑、局部重绘、图片扩展、背景生成先盘点，不默认进入首批。

### MiniMax-H3

- `001-JZL基础工作流/15-MiniMaxH3/62-MiniMax-H3+fl2va+文图首尾帧生视频+V2+工作流.json` → `h3_fl2va_v2`
- `001-JZL基础工作流/15-MiniMaxH3/63-MiniMax-H3+ref2va+多参生音视频+V2+工作流.json` → `h3_ref2va_v2`
- `001-JZL基础工作流/15-MiniMaxH3/64-MiniMax-H3+ref2va+多参考值放大生视频+工作流.json` → `h3_ref2va_upscale_v1`
- `001-JZL基础工作流/15-MiniMaxH3/1141-MiniMax-H3+fl2va+latent放大+二采+V2+工作流.json` → `h3_fl2va_latent_upscale_v1`，先盘点，暂不作为首批默认。
- `56`、`57`、`58`、`59` 必须保留在完整盘点；`59` 是多模式切换工作流，涉及 T2VA/I2VA/FL2VA/L2VA 和多媒体参考，不能遗漏，也不能和 62/63/64 合并。
- 根目录 `h3_t2va.json` 保留为 `h3_t2va_v1`，能力仅为模板 API T2VA。

## 已确认的真实 SHA-256

- H3 56：`59632577141e2d88595dfc5e6260888c4e11492219cafdb5fced097e100da73f`
- H3 57：`4d61b2d402110882d11b18bec3e41dc6445333ffbfe4bf59dbb9c442b7e5731a`
- H3 58：`24ec7b5f65212e594971d69ae6a00e084c386242d9e3bd7286c153a5307d1e1e`
- H3 59：`d2c83466f79b2aba98f7a838df7a43cdc3bbc5e36632417f7c994645d1914c4c`
- H3 62：`7462d774eb0e755421b68ab9aefcbfde2dccb6b6c3c0eb1212e7c1a7e7cc6362`
- H3 63：`f5e208983224dc31187a6a8028c7b96aa935a85d12fbd1fce3eaaafe26ccf383`
- H3 64：`46a7f9d7074799bc2c52def3974f89c28071e0d203777978d33eaaf929870e4a`
- H3 1141：`f4ad5d1247f854791b38b811974dc314d1077f124db108bc9b6ad09c88262842`
- 根目录 `h3_t2va.json`：`b24c7620df97d66e22e031a727bb0caaf3591cdd6202e06aea395751ebb56b47`
- 根目录 `测试工作流.json`：`509cfcf3cf38a35a16f05e612408f77b86900aaa658185011814733d5478a2b1`

## 当前静态解析结果

以下首批目标均可被现有 `inspect_workflow()` 解析为 UI JSON，且没有 JSON 解析错误：

- Z-Image T2I：26 节点
- Z-Image I2I：28 节点
- Z-Image 多模态控制：44 节点
- Flux.2 单图编辑：38 节点
- Flux.2 双图编辑：55 节点
- Flux.2 T2I：32 节点
- H3 1141：56 节点
- H3 62：35 节点
- H3 63：41 节点
- H3 64：35 节点

这只是 JSON 结构解析通过，不等于连接、依赖、节点可达性或运行可用性通过。

## 发现的实现缺口

当前 `workflow_profiles.py` 仍需要增强，才能支撑完整静态核验：

1. UI `links` 尚未与节点 input/output 的 link ID 做交叉校验。
2. API 引用链目前只记录输入名，没有形成完整 source → target 图。
3. `definitions.subgraphs` 尚未展开和核验。
4. 未验证节点 `mode`、禁用节点和输出可达性。
5. 未验证模型文件是否实际存在于 ComfyUI 模型目录。
6. 未解析 custom node 依赖是否安装。
7. 未将绝对路径、相对路径和 workflow hash 形成严格绑定检查。
8. 仅凭文件名生成的 capability 仍属于初步分类，必须由节点/连接契约确认。

## 严格边界

- 只读原始工作流；不修改、不重写、不覆盖 JSON。
- 不运行 ComfyUI，不提交队列。
- 不导入 `D:\Comfyui\ComfyUI\output` 结果。
- 不凭文件名、prefix 或输出序号猜测任务归属。
- 静态核验最多把 Profile 标记为 `verified/static_verified`；没有真实运行和人工验收不能 `approved`。
- 不改变默认 DashScope `qwen3.7-plus` 配置。

## 阶段状态

- 阶段 2A：根目录盘点，已完成，但范围不足。
- 阶段 2A 修订版：递归盘点完整工作流树，已完成。
- 阶段 2B：根目录 `h3_t2va.json` 候选 Profile，已完成。
- 阶段 2C：候选 Profile 离线校验与回归测试，已完成。
- 阶段 2D：递归目标工作流盘点方案，已完成本次设计；首批 Profile 仍待逐个建立和连接契约核验。

## 下一步

先增强静态核验器的 UI links/API 引用图和节点可达性检查，再逐个生成首批 Z-Image、Flux.2、H3 Profile。每完成一个子阶段，必须更新本文件和 `D:\笔记\ClaudeBrain\INDEX.md`。
