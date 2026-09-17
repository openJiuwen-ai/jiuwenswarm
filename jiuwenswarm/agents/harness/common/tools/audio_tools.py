import base64
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import tempfile
import time
import wave
from urllib.parse import urlparse

from fastmcp import FastMCP
from mutagen import File as MutagenFile
from openai import OpenAI
from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.runner import Runner
import requests

from jiuwenswarm.agents.harness.common.tools.multimodal_config import apply_audio_model_config_from_yaml
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify


logger = logging.getLogger(__name__)
ACR_ACCESS_KEY = os.environ.get("ACR_ACCESS_KEY", "")
ACR_ACCESS_SECRET = os.environ.get("ACR_ACCESS_SECRET", "")
ACR_BASE_URL = os.environ.get(
    "ACR_BASE_URL", "https://identify-ap-southeast-1.acrcloud.com/v1/identify"
)
HTTP_TIMEOUT = 20
MAX_AUDIO_BYTES = 25 * 1024 * 1024
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

mcp = FastMCP("audio-mcp-server")

_AUDIO_EXT_MAP = {
    ".mp3": "mp3", ".wav": "wav", ".m4a": "m4a",
    ".aac": "aac", ".ogg": "ogg", ".flac": "flac", ".wma": "wma",
}

_MIME_TO_FORMAT = {"mpeg": "mp3", "wav": "wav", "wave": "wav"}


def _resolve_audio_extension(source_url: str, header_content_type: str = None) -> str:
    parsed = urlparse(source_url)
    path_lower = parsed.path.lower()
    for ext in _AUDIO_EXT_MAP:
        if path_lower.endswith(ext):
            return ext
    if header_content_type:
        ct_lower = header_content_type.lower()
        for mime_key, fmt in _MIME_TO_FORMAT.items():
            if mime_key in ct_lower:
                return f".{fmt}"
        for ext_key in _AUDIO_EXT_MAP:
            if ext_key[1:] in ct_lower:
                return ext_key
    return ".mp3"


def _compute_audio_length_seconds(file_path: str) -> float:
    try:
        with contextlib.closing(wave.open(file_path, "rb")) as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            secs = frames / float(rate)
            if secs > 0:
                return secs
    except (wave.Error, OSError):
        logger.debug("wave module failed to read %s, trying mutagen", file_path)
    try:
        audio_obj = MutagenFile(file_path)
        if audio_obj is not None and hasattr(audio_obj, "info"):
            if hasattr(audio_obj.info, "length"):
                secs = float(audio_obj.info.length)
                if secs > 0:
                    return secs
    except Exception:
        logger.debug("mutagen failed to read %s", file_path, exc_info=True)
    raise ValueError("Unable to determine audio duration")


def _load_audio_as_base64(file_path: str) -> tuple[str, str]:
    with open(file_path, "rb") as f:
        raw_bytes = f.read()
    b64_str = base64.b64encode(raw_bytes).decode("utf-8")
    guessed_mime, _ = mimetypes.guess_type(file_path)
    if guessed_mime and guessed_mime.startswith("audio/"):
        mime_suffix = guessed_mime.split("/")[-1]
        fmt = _MIME_TO_FORMAT.get(mime_suffix, "mp3")
    else:
        fmt = "mp3"
    return b64_str, fmt


def _download_audio_to_tempfile(url: str) -> str:
    hdrs = {"User-Agent": DEFAULT_USER_AGENT}
    resp = requests.get(url, headers=hdrs, timeout=HTTP_TIMEOUT, stream=True, verify=get_requests_verify())
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    ext = _resolve_audio_extension(url, ct)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    tmp.close()
                    os.remove(tmp.name)
                    raise ValueError("Audio file exceeds size limit (25MB).")
                tmp.write(chunk)
    except Exception:
        tmp.close()
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
        raise
    tmp.close()
    return tmp.name


def _create_openai_client_for_audio():
    key = os.environ.get("AUDIO_API_KEY") or os.environ.get("API_KEY", "")
    base = os.environ.get("AUDIO_API_BASE") or os.environ.get("API_BASE", "")
    return key, base, OpenAI(api_key=key, base_url=base) if key else None


def _build_sandbox_unavailable_msg(tool_name: str) -> str:
    return (
        f"The {tool_name} tool cannot access to sandbox file, "
        "please use the local path provided by original instruction"
    )


def _build_missing_key_msg(tool_name: str) -> str:
    return (
        f"[ERROR]: AUDIO_API_KEY or API_KEY is not configured "
        f"for {tool_name}."
    )


@tool(
    name="audio_question_answering",
    description=(
        "Answer questions based on audio content. Use this tool when the user provides "
        "an audio file and asks questions about the audio content, such as 'what is "
        "discussed in this audio', 'how many speakers', or any analysis requiring "
        "understanding of the audio."
    ),
)
async def audio_question_answering(audio_path_or_url: str, question: str) -> str:
    from jiuwenswarm.common.config import get_config
    try:
        apply_audio_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply audio model config from yaml", exc_info=True)

    api_key, api_base, client = _create_openai_client_for_audio()
    if not api_key:
        return _build_missing_key_msg("audio question answering")

    audio_model = os.environ.get("AUDIO_MODEL_NAME", "gpt-4o-audio-preview")
    logger.info("[audio_question_answering] using model: %s (api_base: %s)", audio_model, api_base)

    try:
        prompt_text = f"Answer the following question based on the given audio information:\n\n{question}"
        cleanup_needed = False
        target_path = audio_path_or_url

        if os.path.exists(audio_path_or_url):
            pass
        elif "home/user" in audio_path_or_url:
            return _build_sandbox_unavailable_msg("audio_question_answering")
        else:
            target_path = _download_audio_to_tempfile(audio_path_or_url)
            cleanup_needed = True

        try:
            b64_data, fmt = _load_audio_as_base64(target_path)
            duration = _compute_audio_length_seconds(target_path)
        finally:
            if cleanup_needed and os.path.exists(target_path):
                os.remove(target_path)

        if not b64_data or not fmt:
            return (
                "[ERROR]: Audio question answering failed: Failed to encode audio file.\n"
                "Note: Files from sandbox are not available. "
                "You should use local path given in the instruction.\n"
                "URLs must include the proper scheme (e.g., 'https://') "
                "and be publicly accessible. The file should be in a common audio "
                "format such as MP3.\n"
                "Note: YouTube video URL is not supported."
            )

        resp = client.chat.completions.create(
            model=audio_model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant specializing in audio analysis."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "input_audio", "input_audio": {"data": b64_data, "format": fmt}},
                    ],
                },
            ],
        )
        answer = resp.choices[0].message.content
        return f"{answer}\n\nAudio duration: {duration} seconds"
    except Exception as err:
        return (
            f"[ERROR]: Audio question answering failed when calling OpenAI API: {err}\n"
            "Note: Files from sandbox are not available. "
            "You should use local path given in the instruction. "
            "The file should be in a common audio format such as MP3, WAV, or M4A.\n"
            "Note: YouTube video URL is not supported."
        )


@tool(
    name="audio_metadata",
    description="Identify the metadata (name, author, year) of the given audio file using the ACRCloud API.",
)
async def audio_metadata(audio_path_or_url: str) -> str:
    cleanup = False
    local_path = audio_path_or_url

    try:
        if os.path.exists(audio_path_or_url):
            pass
        elif "home/user" in audio_path_or_url:
            return (
                "The audio_question_answering tool cannot access to sandbox file, "
                "please use a local path instead. If the audio file has been preprocessed "
                "in the sandbox, please download the file to your local machine and use "
                "the local path instead."
            )
        else:
            local_path = _download_audio_to_tempfile(audio_path_or_url)
            cleanup = True

        duration = _compute_audio_length_seconds(local_path)

        if not ACR_ACCESS_KEY or not ACR_ACCESS_SECRET:
            return (
                f"Duration (seconds): {duration:.2f}\n"
                "Note: Title/artist identification is disabled because ACR credentials are not provided."
            )

        if duration > 15:
            return (
                "The audio_metadata tool is better used to process audio file with less than 15 seconds, "
                "please cut your audio file to a small one and try again."
            )

        ts = time.time()
        sig_base = (
            "POST\n/v1/identify\n" + ACR_ACCESS_KEY + "\naudio\n1\n" + str(ts)
        )
        sig = base64.b64encode(
            hmac.new(
                ACR_ACCESS_SECRET.encode("ascii"),
                sig_base.encode("ascii"),
                digestmod=hashlib.sha1,
            ).digest()
        ).decode("ascii")

        fname = os.path.basename(local_path)
        fsize = os.path.getsize(local_path)
        mime_guess, _ = mimetypes.guess_type(local_path)
        if mime_guess and mime_guess.startswith("audio/"):
            mime_suffix = mime_guess.split("/")[-1]
            upload_fmt = _MIME_TO_FORMAT.get(mime_suffix, "mp3")
        else:
            upload_fmt = "mp3"

        files_payload = [("sample", (fname, open(local_path, "rb"), upload_fmt))]
        form_data = {
            "access_key": ACR_ACCESS_KEY,
            "sample_bytes": fsize,
            "timestamp": str(ts),
            "signature": sig,
            "data_type": "audio",
            "signature_version": "1",
        }

        r = requests.post(
            ACR_BASE_URL,
            files=files_payload,
            data=form_data,
            timeout=HTTP_TIMEOUT,
            verify=get_requests_verify(),
        )
        r.encoding = "utf-8"
        parsed = json.loads(r.text)

        meta = parsed.get("metadata", {})
        if "humming" in meta:
            items = meta["humming"]
            scored = []
            for itm in items:
                scored.append((
                    itm.get("duration_ms"),
                    itm.get("title"),
                    itm.get("artists", [{}])[0].get("name"),
                    itm.get("release_date"),
                    itm.get("score"),
                ))
            scored.sort(key=lambda x: x[0] or 0, reverse=True)
            best = scored[0]
            return f"Name: {best[1]}, Artist: {best[2]}, Release Date: {best[3]}. Note: score={best[4]}"
        elif "music" in meta:
            itm = meta["music"][0]
            return f"Name: {itm['title']}, Artist: {itm['artists'][0]['name']}, Release Date: {itm['release_date']}."
        else:
            return f"Duration (seconds): {duration:.2f}\nACR: No metadata found for the given audio file."

    except Exception as err:
        return f"[ERROR]: Audio metadata identification failed: {err}\n"

    finally:
        if cleanup and os.path.exists(local_path):
            os.remove(local_path)
