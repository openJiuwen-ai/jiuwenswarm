# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""generate_image 图像编辑/自愈链路的单元测试。

对应 PR 审查意见：新增 _resolve_edit_images、_try_auto_resize_from_dimension_error
与 _build_image_gen_kwargs 编辑分支需覆盖 happy path + 异常 + 边界：

- _resolve_edit_images：本地内联 / magic bytes 白名单 / 20MB 上限 / 沙箱拦截 /
  data URL 校验 / URL 直传 / 多图 / 空与非字符串输入
- _try_auto_resize_from_dimension_error：尺寸错误正则解析 / 等比缩放 /
  极端长宽比放弃 / Pillow 缺失降级 / decode 失败 / 无关错误
- _build_image_gen_kwargs：extra_body.image 拼接 / size 语义 / n 强制
- 工具 schema：LLM 可见性
"""

from __future__ import annotations

import base64
import io
import sys

import pytest
from PIL import Image

from jiuwenswarm.agents.harness.common.tools import image_tools as it

MAAS_BASE = "https://api.modelarts-maas.com/v1"

_DIM_ERROR = (
    "Error code: 400 - {'error': {'code': 'ModelArts.81001', 'message': "
    "'Value error, Param image Error: qwen-image-edit-2509 allowed area "
    "range is:[512x512,3072x3072]. Current area: 358 x 117'}}"
)


def _png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(width: int, height: int, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 30, 30)).save(buf, format=fmt)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _decode_data_url(item: str) -> Image.Image:
    assert item.startswith("data:")
    raw = base64.b64decode(item.partition(",")[2])
    return Image.open(io.BytesIO(raw))


# ---------------------------------------------------------------------------
# _resolve_edit_images：happy path
# ---------------------------------------------------------------------------


def test_resolve_local_file_becomes_data_url(tmp_path) -> None:
    p = tmp_path / "watermark.png"
    p.write_bytes(_png_bytes(358, 117))
    resolved = it._resolve_edit_images({"image": str(p)})
    assert resolved is not None and len(resolved) == 1
    assert resolved[0].startswith("data:image/png;base64,")
    assert _decode_data_url(resolved[0]).size == (358, 117)


def test_resolve_mime_from_real_content_not_extension(tmp_path) -> None:
    # JPEG 内容存成 .png：sniff 以真实内容为准
    buf = io.BytesIO()
    Image.new("RGB", (600, 600)).save(buf, format="JPEG")
    p = tmp_path / "actually_jpeg.png"
    p.write_bytes(buf.getvalue())
    resolved = it._resolve_edit_images({"image": str(p)})
    assert resolved is not None
    assert resolved[0].startswith("data:image/jpeg;base64,")


def test_resolve_data_url_image_passthrough() -> None:
    item = _data_url(600, 600)
    assert it._resolve_edit_images({"image": item}) == [item]


def test_resolve_http_url_passthrough() -> None:
    url = "https://example.com/photo.png"
    assert it._resolve_edit_images({"image": url}) == [url]


def test_resolve_multi_image_list(tmp_path) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(_png_bytes(600, 600))
    resolved = it._resolve_edit_images({"image": [str(p), "https://example.com/b.jpg"]})
    assert resolved is not None and len(resolved) == 2
    assert resolved[0].startswith("data:image/png;base64,")
    assert resolved[1] == "https://example.com/b.jpg"


# ---------------------------------------------------------------------------
# _resolve_edit_images：异常与边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", [], [""], ["  ", ""]])
def test_resolve_empty_image_returns_none(raw) -> None:
    assert it._resolve_edit_images({"image": raw}) is None


def test_resolve_non_string_items_report_not_found() -> None:
    with pytest.raises(ValueError, match="not found"):
        it._resolve_edit_images({"image": [123, ""]})


def test_resolve_missing_file_raises(tmp_path) -> None:
    with pytest.raises(ValueError, match="not found"):
        it._resolve_edit_images({"image": str(tmp_path / "missing.png")})


def test_resolve_sandbox_path_dedicated_error() -> None:
    # 与 visual_question_answering 的沙箱报错语义对齐
    with pytest.raises(ValueError, match="sandbox"):
        it._resolve_edit_images({"image": "home/user/uploaded/a.png"})


def test_resolve_non_image_file_rejected_without_exfil(tmp_path) -> None:
    # 伪装 .png 的文本文件（如私钥/配置）不得被内联外发
    p = tmp_path / "evil.png"
    p.write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nnope\n")
    with pytest.raises(ValueError, match="not a recognized image"):
        it._resolve_edit_images({"image": str(p)})


def test_resolve_non_image_data_url_rejected() -> None:
    item = "data:image/png;base64," + base64.b64encode(b"plain text").decode()
    with pytest.raises(ValueError, match="not a recognized image"):
        it._resolve_edit_images({"image": item})


def test_resolve_invalid_b64_data_url_rejected() -> None:
    # b64 解码失败同样落入白名单拒绝，不外发
    with pytest.raises(ValueError, match="not a recognized image"):
        it._resolve_edit_images({"image": "data:image/png;base64,@@@not-b64@@@"})


def test_resolve_oversized_file_raises(tmp_path) -> None:
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (20 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        it._resolve_edit_images({"image": str(p)})


# ---------------------------------------------------------------------------
# _try_auto_resize_from_dimension_error
# ---------------------------------------------------------------------------


def test_auto_resize_upscales_small_image() -> None:
    out = it._try_auto_resize_from_dimension_error(
        Exception(_DIM_ERROR), [_data_url(358, 117)]
    )
    assert out is not None
    im = _decode_data_url(out[0])
    assert im.size[0] >= 512 and im.size[1] >= 512
    assert im.size[0] / im.size[1] == pytest.approx(358 / 117, rel=0.02)


def test_auto_resize_downscales_large_image() -> None:
    out = it._try_auto_resize_from_dimension_error(
        Exception(_DIM_ERROR), [_data_url(5000, 3000)]
    )
    assert out is not None
    im = _decode_data_url(out[0])
    assert max(im.size) <= 3072
    assert im.size[0] / im.size[1] == pytest.approx(5 / 3, rel=0.02)


def test_auto_resize_extreme_aspect_gives_up() -> None:
    # 10000x100 等比缩放无法落入 [512,3072]：放弃自愈，不拉伸失真
    assert it._try_auto_resize_from_dimension_error(
        Exception(_DIM_ERROR), [_data_url(10000, 100)]
    ) is None


def test_auto_resize_in_range_image_no_retry() -> None:
    assert it._try_auto_resize_from_dimension_error(
        Exception(_DIM_ERROR), [_data_url(2048, 1024)]
    ) is None


@pytest.mark.parametrize(
    "exc,msg",
    [
        (None, None),
        (Exception("Error code: 401 - invalid api key"), "unrelated error"),
        (Exception("some other 400"), "unrelated error"),
    ],
)
def test_auto_resize_unrelated_errors_give_up(exc, msg) -> None:
    assert it._try_auto_resize_from_dimension_error(exc, [_data_url(100, 100)]) is None


def test_auto_resize_http_url_gives_up() -> None:
    assert it._try_auto_resize_from_dimension_error(
        Exception(_DIM_ERROR), ["https://example.com/a.png"]
    ) is None


def test_auto_resize_without_pillow_gives_up(monkeypatch) -> None:
    # Pillow 缺失：降级放弃自愈，让服务端错误原样透传。
    # data URL 需在禁用 PIL 前构造——Pillow 的 PNG 编码器在 save 时才
    # 惰性 import PIL 包，构造阶段就需要可用的 PIL。
    item = _data_url(358, 117)
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert it._try_auto_resize_from_dimension_error(Exception(_DIM_ERROR), [item]) is None


# ---------------------------------------------------------------------------
# _build_image_gen_kwargs：编辑语义
# ---------------------------------------------------------------------------


def test_kwargs_edit_images_extra_body_and_size_none() -> None:
    kw = it._build_image_gen_kwargs(
        "OpenAI", {}, size=None, n=1, api_base=MAAS_BASE,
        edit_images=["data:image/png;base64,AA==", "https://x.com/b.jpg"],
    )
    # openjiuwen 的 size 形参默认 '1664*928'（* 分隔）会触发华为云 400；
    # 显式 None 让请求体省略 size 字段，服务端按输入图尺寸生成。
    assert kw.get("size", "MISSING") is None
    assert kw["extra_body"]["image"] == "data:image/png;base64,AA==,https://x.com/b.jpg"
    assert kw["n"] == 1 and kw["response_format"] == "b64_json"


def test_kwargs_edit_explicit_size_normalized() -> None:
    kw = it._build_image_gen_kwargs(
        "OpenAI", {}, size="1024*1024", n=1, api_base=MAAS_BASE,
        edit_images=["data:image/png;base64,AA=="],
    )
    assert kw["size"] == "1024x1024"


def test_kwargs_edit_invalid_size_dropped() -> None:
    kw = it._build_image_gen_kwargs(
        "OpenAI", {}, size="auto", n=1, api_base=MAAS_BASE,
        edit_images=["data:image/png;base64,AA=="],
    )
    assert kw.get("size", "MISSING") is None


def test_kwargs_text2img_default_unchanged() -> None:
    kw = it._build_image_gen_kwargs("OpenAI", {}, size="1024x1024", n=2, api_base=MAAS_BASE)
    assert kw["size"] == "1024x1024" and kw["n"] == 1
    assert "extra_body" not in kw
    kw2 = it._build_image_gen_kwargs("OpenAI", {}, size="1024x1024", n=2, api_base="")
    assert kw2["n"] == 2


def test_kwargs_seed_forwarded() -> None:
    kw = it._build_image_gen_kwargs(
        "OpenAI", {"seed": 42}, size="1024x1024", n=1, api_base=MAAS_BASE,
    )
    assert kw["seed"] == 42


# ---------------------------------------------------------------------------
# 工具 schema：LLM 必须能看到 inputs 的每个键
# （历史缺陷：docstring 嵌套子键 "Dict with keys:" 解析丢失，LLM 只见空描述）
# ---------------------------------------------------------------------------


def test_tool_schema_exposes_all_inputs_keys() -> None:
    p = it.generate_image.card.input_params
    inner = p["properties"]["inputs"]["properties"]
    assert sorted(inner.keys()) == sorted(
        ["prompt", "image", "size", "quality", "n",
         "negative_prompt", "prompt_extend", "seed", "save_dir"]
    )
    assert p["required"] == ["inputs"]
    assert p["properties"]["inputs"]["required"] == ["prompt"]


def test_tool_schema_image_param_has_full_description() -> None:
    inner = it.generate_image.card.input_params["properties"]["inputs"]["properties"]
    image = inner["image"]
    assert image["type"] == ["string", "array"]
    desc = image["description"]
    assert "watermark removal" in desc
    assert "local file path" in desc
