"""产物载荷清单（issue #18）契约测试。

用**合成载荷**（自己造 PNG 的 ``prompt`` chunk）钉住契约，不依赖真实产物——
产物是仓库外的机器专属素材（见 ``docs/agents`` 与 conftest 的约定）。

本工具要解决的问题：产物元数据里模型只记**文件名**，而文件名是可变引用。
2026-09-23 用户把整套 LoRA 重下（``8step_v1.0_comfyui`` → ``8step_v1.0_768p_comfyui``、
``4step_v1.1_768p`` → ``4step_v1.2_768p``），当天 16:10 之前所有跑次引用的文件在本机
解析不到任何东西，**而没有任何地方报错或警告**；``00003``/``00005`` 那对质检最严的
对照因此永久作废。所以「解析不到」必须报红并非零退出，不能只打 warning 就过。
"""
from __future__ import annotations

import json

from PIL import Image, PngImagePlugin

from tools.loadout_manifest import extract_loadout, main, render_report, resolve_refs


def write_artifact(path, payload, *, size=(8, 8)):
    """写一张带 ``prompt`` tEXt chunk 的假产物（ComfyUI 的真实存法）。"""
    image = Image.new("RGB", size, (10, 20, 30))
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", json.dumps(payload, ensure_ascii=False))
    image.save(path, pnginfo=info)
    return path


def payload(**overrides):
    """一份贴近真产物的 API 格式载荷（字段名逐字取自 2026-09-23 真产物）。"""
    nodes = {
        "114": {"class_type": "LoadImage", "inputs": {"image": "图片放大_00004_.png"}},
        "125": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "126": {"class_type": "BasicScheduler",
                "inputs": {"scheduler": "simple", "steps": 4, "denoise": 1.0}},
        "131": {"class_type": "RandomNoise", "inputs": {"noise_seed": 199287832709942}},
        "183": {"class_type": "UNETLoader",
                "inputs": {"unet_name": "MiniMax-H3\\unet.safetensors"}},
        "184": {"class_type": "LoraLoaderModelOnly",
                "inputs": {"lora_name": "MiniMax-H3\\lora.safetensors",
                           "strength_model": 1}},
        "189": {"class_type": "CLIPLoader",
                "inputs": {"clip_name": "MiniMax-H3\\clip.safetensors"}},
        "191": {"class_type": "VAELoader",
                "inputs": {"vae_name": "MiniMax-H3\\vae.safetensors"}},
    }
    nodes.update(overrides)
    return nodes


def fake_comfy_root(tmp_path, *, files=("MiniMax-H3/lora.safetensors",
                                        "MiniMax-H3/unet.safetensors",
                                        "MiniMax-H3/clip.safetensors",
                                        "MiniMax-H3/vae.safetensors",
                                        "图片放大_00004_.png")):
    """造一个最小的 ComfyUI 目录树：各模型目录 + input/。"""
    root = tmp_path / "ComfyUI"
    for rel in files:
        sub = "input" if rel.endswith(".png") else {
            "lora": "models/loras", "unet": "models/diffusion_models",
            "clip": "models/text_encoders", "vae": "models/vae",
        }[rel.split("/")[-1].split(".")[0]]
        target = root / sub / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 32 + rel.encode())
    return root


# --- 提取 ---------------------------------------------------------------

def test_extract_reads_sampling_config(tmp_path):
    art = write_artifact(tmp_path / "a.png", payload())
    loadout = extract_loadout(art)
    assert loadout.sampling.steps == 4
    assert loadout.sampling.scheduler == "simple"
    assert loadout.sampling.denoise == 1.0
    assert loadout.sampling.sampler_name == "euler"
    assert loadout.sampling.noise_seed == 199287832709942


def test_extract_collects_model_and_image_refs_with_kinds(tmp_path):
    art = write_artifact(tmp_path / "a.png", payload())
    refs = {r.name: r for r in extract_loadout(art).refs}
    assert refs["MiniMax-H3\\lora.safetensors"].kind == "lora"
    assert refs["MiniMax-H3\\unet.safetensors"].kind == "unet"
    assert refs["MiniMax-H3\\clip.safetensors"].kind == "clip"
    assert refs["MiniMax-H3\\vae.safetensors"].kind == "vae"
    assert refs["图片放大_00004_.png"].kind == "image"


def test_missing_prompt_chunk_is_an_error_not_an_empty_loadout(tmp_path):
    """没有 prompt chunk 的 PNG 必须显式失败——静默返回空清单等于「绝不静默失败」的反面。"""
    plain = tmp_path / "plain.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(plain)
    result = extract_loadout(plain)
    assert result.error is not None
    assert "prompt" in result.error


def test_unknown_model_loader_is_still_collected(tmp_path):
    """PDDAcc 那类加速节点的 widget 键名不在已知表里，但同样是模型文件引用。"""
    art = write_artifact(tmp_path / "a.png", payload(**{
        "205": {"class_type": "MiniMaxH3PDDAccApply",
                "inputs": {"acc_name": "pdd_acc_8step.safetensors", "steps": "8"}},
    }))
    names = {r.name for r in extract_loadout(art).refs}
    assert "pdd_acc_8step.safetensors" in names


# --- 解析 ---------------------------------------------------------------

def test_resolves_names_against_comfyui_subdirs(tmp_path):
    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    report = resolve_refs([extract_loadout(art)], root)
    assert report.ok
    by_name = {r.name: r for r in report.resolved}
    # 元数据里是反斜杠分隔的 Windows 风格名，必须归一到 ComfyUI 的目录布局
    assert by_name["MiniMax-H3\\lora.safetensors"].path == \
        root / "models/loras/MiniMax-H3/lora.safetensors"
    assert by_name["图片放大_00004_.png"].path == root / "input/图片放大_00004_.png"


def test_model_refs_get_size_and_sha256(tmp_path):
    """哈希必须是真的文件摘要（换版后同名文件的唯一分辨手段），不是占位符。"""
    import hashlib

    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    report = resolve_refs([extract_loadout(art)], root)
    lora_path = root / "models/loras/MiniMax-H3/lora.safetensors"
    lora = next(r for r in report.resolved if r.kind == "lora")
    assert lora.size == lora_path.stat().st_size
    assert lora.sha256 == hashlib.sha256(lora_path.read_bytes()).hexdigest()


def test_input_image_gets_size_and_mtime_but_no_hash(tmp_path):
    """2GB × N 的哈希会慢到没人用；输入图只要大小 + mtime 就够指认。"""
    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    report = resolve_refs([extract_loadout(art)], root)
    image = next(r for r in report.resolved if r.kind == "image")
    assert image.size > 0
    assert image.mtime
    assert image.sha256 is None


def test_missing_model_file_is_flagged_and_makes_report_not_ok(tmp_path):
    root = fake_comfy_root(tmp_path, files=())  # 目录在但文件全没了
    (root / "models/loras/MiniMax-H3").mkdir(parents=True, exist_ok=True)
    art = write_artifact(tmp_path / "a.png", payload())
    report = resolve_refs([extract_loadout(art)], root)
    assert not report.ok
    assert "MiniMax-H3\\lora.safetensors" in {r.name for r in report.missing}


def test_missing_comfy_root_is_reported_as_such(tmp_path):
    """根目录本身不存在 ≠ 文件被删。两者要分清，否则判读会指错方向。"""
    art = write_artifact(tmp_path / "a.png", payload())
    report = resolve_refs([extract_loadout(art)], tmp_path / "nope")
    assert not report.ok
    assert report.root_missing


def test_no_hash_skips_hashing_but_still_resolves(tmp_path):
    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    report = resolve_refs([extract_loadout(art)], root, want_hash=False)
    lora = next(r for r in report.resolved if r.kind == "lora")
    assert lora.sha256 is None
    assert lora.size > 0


# --- 渲染与 CLI ---------------------------------------------------------

def test_render_lists_sampling_and_refs(tmp_path):
    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    text = render_report(resolve_refs([extract_loadout(art)], root))
    assert "steps" in text and "4" in text
    assert "euler" in text
    assert "lora.safetensors" in text


def test_cli_exit_zero_when_all_refs_resolve(tmp_path, capsys):
    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    code = main(["--comfy-root", str(root), "--no-hash", str(art)])
    assert code == 0
    assert "steps=4" in capsys.readouterr().out


def test_cli_exit_nonzero_and_red_when_a_ref_is_gone(tmp_path, capsys):
    root = fake_comfy_root(tmp_path, files=())
    (root / "models/loras/MiniMax-H3").mkdir(parents=True, exist_ok=True)
    art = write_artifact(tmp_path / "a.png", payload())
    code = main(["--comfy-root", str(root), "--no-hash", str(art)])
    out = capsys.readouterr().out
    assert code == 1
    assert "lora.safetensors" in out
    assert "解析不到" in out or "失效" in out


def test_cli_accepts_multiple_artifacts(tmp_path, capsys):
    root = fake_comfy_root(tmp_path)
    a = write_artifact(tmp_path / "a.png", payload())
    b = write_artifact(tmp_path / "b.png", payload(**{
        "126": {"class_type": "BasicScheduler",
                "inputs": {"scheduler": "simple", "steps": 8, "denoise": 1.0}},
    }))
    code = main(["--comfy-root", str(root), "--no-hash", str(a), str(b)])
    out = capsys.readouterr().out
    assert code == 0
    assert "steps=4" in out and "steps=8" in out


def test_cli_footer_carries_the_ab_discipline(tmp_path, capsys):
    """纪律必须跟着工具走：引用消失的那批数字不得与新数字拼进同一张表。"""
    root = fake_comfy_root(tmp_path)
    art = write_artifact(tmp_path / "a.png", payload())
    main(["--comfy-root", str(root), "--no-hash", str(art)])
    out = capsys.readouterr().out
    assert "孤立观测" in out and "同一张表" in out


def test_help_states_the_ab_discipline():
    """--help 也要带这条纪律（issue #18 交付物 2 明写）。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.suppress(SystemExit):
        with contextlib.redirect_stdout(buf):
            main(["--help"])
    help_text = buf.getvalue()
    assert "孤立观测" in help_text
    assert "同一张表" in help_text
