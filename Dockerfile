# ModelFlow Serverless Worker — Runpod
#
# La imagen NO lleva ComfyUI ni modelos: todo vive en tu Network Volume
# montado en /workspace/ComfyUI por Runpod. Así la imagen pesa ~2 GB
# (solo Python + deps del handler + deps core de ComfyUI).
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COMFYUI_DIR=/workspace/ComfyUI

WORKDIR /

# Handler deps
COPY requirements.txt /requirements.txt
RUN pip install --upgrade pip && pip install -r /requirements.txt

# ComfyUI base deps (las que vienen en su requirements.txt).
# Tu Network Volume YA tiene ComfyUI + custom_nodes + modelos, pero cuando
# cold-start arranca con Python del contenedor, algunos paquetes tienen
# que estar presentes en la imagen para que ComfyUI importe OK.
RUN pip install \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 || true

RUN pip install \
    transformers \
    safetensors \
    aiohttp \
    einops \
    torchsde \
    kornia \
    spandrel \
    soundfile \
    sentencepiece \
    gguf \
    comfyui-frontend-package \
    tqdm psutil pyyaml Pillow scipy

# Handler
COPY handler.py /handler.py

# Runpod serverless start
CMD ["python", "-u", "/handler.py"]
