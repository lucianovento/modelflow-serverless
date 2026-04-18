"""
ModelFlow — Runpod Serverless Handler

Flujo:
  1. Cold start: levanta ComfyUI desde /runpod-volume/ComfyUI como subprocess
  2. Espera a que /system_stats responda
  3. Recibe job input con { workflow, client_id }
  4. Postea el workflow a ComfyUI :8188 /prompt
  5. Polling a /history/{prompt_id} hasta obtener outputs
  6. Lee la imagen del disco y la devuelve base64

La imagen se devuelve INLINE (base64) para que el cliente no dependa de URLs
de la instancia Runpod que se apaga al terminar el request.
"""
import base64
import json
import pathlib
import subprocess
import sys
import time
import traceback
from urllib.parse import urlencode

import httpx
import runpod

import os

# Runpod Serverless monta Network Volumes en /runpod-volume (no se puede cambiar).
# Permitimos override vía env var por si un día cambia o para testing local.
COMFY_DIR    = os.environ.get("COMFYUI_DIR", "/runpod-volume/ComfyUI")
COMFY_HOST   = "127.0.0.1"
COMFY_PORT   = 8188
COMFY_URL    = f"http://{COMFY_HOST}:{COMFY_PORT}"
OUTPUT_DIR   = f"{COMFY_DIR}/output"
STARTUP_LOG  = "/tmp/comfyui_startup.log"
BOOT_TIMEOUT = 240  # segundos para boot (primera vez carga modelos desde volumen, puede tardar)

_comfy_proc: subprocess.Popen | None = None


# ────────────────────────────────────────────────────────────────
# ComfyUI lifecycle
# ────────────────────────────────────────────────────────────────

def _ensure_comfyui_up() -> None:
    """Arranca ComfyUI si no está corriendo y espera a /system_stats."""
    global _comfy_proc

    # Si ya responde, listo
    if _is_ready():
        return

    # Si el proceso existe pero murió, tirarlo
    if _comfy_proc and _comfy_proc.poll() is not None:
        _comfy_proc = None

    if _comfy_proc is None:
        print(f"[boot] launching ComfyUI from {COMFY_DIR}", flush=True)
        if not pathlib.Path(COMFY_DIR, "main.py").exists():
            raise FileNotFoundError(
                f"No encuentro {COMFY_DIR}/main.py. "
                f"Verificar que el Network Volume esté adjunto y que contenga ComfyUI en la raíz. "
                f"Contenido /runpod-volume: {list(pathlib.Path('/runpod-volume').glob('*')) if pathlib.Path('/runpod-volume').exists() else 'NO MONTADO'}"
            )
        log_fd = open(STARTUP_LOG, "w")
        _comfy_proc = subprocess.Popen(
            [sys.executable, "main.py", "--listen", COMFY_HOST, "--port", str(COMFY_PORT), "--disable-auto-launch"],
            cwd=COMFY_DIR,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
        )

    # Esperar readiness
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        if _is_ready():
            print(f"[boot] ComfyUI ready after {int(time.time() - (deadline - BOOT_TIMEOUT))}s", flush=True)
            return
        if _comfy_proc.poll() is not None:
            raise RuntimeError(f"ComfyUI murió durante el boot. Ver {STARTUP_LOG}")
        time.sleep(2)

    raise TimeoutError(f"ComfyUI no respondió en {BOOT_TIMEOUT}s")


def _is_ready() -> bool:
    try:
        r = httpx.get(f"{COMFY_URL}/system_stats", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────
# Prompt submission + polling
# ────────────────────────────────────────────────────────────────

def _submit_prompt(workflow: dict, client_id: str) -> str:
    with httpx.Client(timeout=60) as c:
        r = c.post(f"{COMFY_URL}/prompt", json={"prompt": workflow, "client_id": client_id})
        if r.status_code >= 400:
            raise RuntimeError(f"ComfyUI rejected prompt ({r.status_code}): {r.text[:500]}")
        return r.json()["prompt_id"]


def _wait_for_image(prompt_id: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    with httpx.Client(timeout=30) as c:
        while time.time() < deadline:
            time.sleep(2)
            r = c.get(f"{COMFY_URL}/history/{prompt_id}")
            if r.status_code != 200:
                continue
            data = r.json()
            if prompt_id not in data:
                continue
            entry = data[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI execution error: {status.get('messages')}")
            outputs = entry.get("outputs", {})
            for node_out in outputs.values():
                imgs = node_out.get("images") or []
                if imgs:
                    return imgs[0]
    raise TimeoutError(f"Timeout esperando imagen ({timeout}s)")


def _read_image_file(img_info: dict) -> bytes:
    filename  = img_info["filename"]
    subfolder = img_info.get("subfolder", "")
    img_type  = img_info.get("type", "output")
    base_dir  = {
        "output": OUTPUT_DIR,
        "temp":   f"{COMFY_DIR}/temp",
        "input":  f"{COMFY_DIR}/input",
    }.get(img_type, OUTPUT_DIR)
    path = pathlib.Path(base_dir) / subfolder / filename
    if not path.exists():
        raise FileNotFoundError(f"Imagen no encontrada: {path}")
    return path.read_bytes()


# ────────────────────────────────────────────────────────────────
# Runpod handler entry point
# ────────────────────────────────────────────────────────────────

def handler(event: dict) -> dict:
    try:
        _ensure_comfyui_up()

        job_input = event.get("input") or {}
        workflow  = job_input.get("workflow")
        client_id = job_input.get("client_id", "modelflow")

        if not isinstance(workflow, dict):
            return {"error": "Falta 'workflow' (dict) en el input."}

        t0 = time.time()
        prompt_id = _submit_prompt(workflow, client_id)
        print(f"[job] queued prompt_id={prompt_id}", flush=True)

        img_info = _wait_for_image(prompt_id)
        img_bytes = _read_image_file(img_info)
        elapsed  = round(time.time() - t0, 1)

        return {
            "prompt_id": prompt_id,
            "filename":  img_info["filename"],
            "image_b64": base64.b64encode(img_bytes).decode("ascii"),
            "elapsed_sec": elapsed,
        }
    except Exception as e:
        return {
            "error":   str(e),
            "trace":   traceback.format_exc(limit=10),
        }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
