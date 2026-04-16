# ModelFlow Runpod Serverless Worker

Worker que corre en Runpod Serverless. Recibe un workflow JSON de ComfyUI (el mismo que usamos local) + metadata, ejecuta ComfyUI (montado desde tu Network Volume en `/workspace/ComfyUI`) y devuelve la imagen como base64.

## Archivos

- `handler.py` — entrypoint Runpod. Levanta ComfyUI, encola el prompt, espera, devuelve imagen.
- `Dockerfile` — imagen minimalista (~2 GB, sin modelos; los toma del Network Volume).
- `requirements.txt` — deps del handler.

## Deploy (pasos una sola vez)

### 1. Subir imagen a Docker Hub (o GHCR)

```bash
# En tu máquina con Docker instalado
docker build -t tuusuario/modelflow-worker:latest .
docker push tuusuario/modelflow-worker:latest
```

### 2. Crear Serverless Endpoint en Runpod

- https://www.runpod.io/console/serverless → **New Endpoint**
- **Template**:
  - Container Image: `tuusuario/modelflow-worker:latest`
  - Container Disk: `5 GB`
  - Volume: **Attach tu Network Volume existente** → Mount Path: `/workspace`
  - Expose HTTP Port: `8188` (opcional, para debug)
- **Workers**:
  - GPU: **RTX 4090** (24 GB VRAM, $0.00034/s)
  - Max Workers: `3` (permite 3 renders paralelos máximo)
  - Active Workers: `0` (scale-to-zero, ahorra plata cuando no hay jobs)
  - Idle Timeout: `5` segundos (apaga worker 5s después de inactividad)
  - Execution Timeout: `900` segundos (15 min por render, sobra para FLUX)
- **Networking**: default está bien

Clickear **Deploy**. Te da:
- `Endpoint ID` (algo tipo `xyz123abc`)
- `API Key` (desde https://www.runpod.io/console/user/settings → API Keys → Create)

### 3. Probar el endpoint

```bash
curl -X POST https://api.runpod.ai/v2/ENDPOINT_ID/runsync \
  -H "Authorization: Bearer TU_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "workflow": { ...tu workflow JSON aquí... },
      "client_id": "modelflow-test"
    }
  }'
```

Respuesta esperada:
```json
{
  "id": "...",
  "status": "COMPLETED",
  "output": {
    "prompt_id": "...",
    "filename": "ComfyUI_00001_.png",
    "image_b64": "iVBORw0KGgo..."
    "elapsed_sec": 78.3
  }
}
```

### 4. Integrar en ModelFlow (paso siguiente)

Después de confirmar que el endpoint responde, cargamos:
- `runpod_api_key` y `runpod_endpoint_id` en /admin/options
- Un `RunpodService` en Python AI que llame al endpoint igual que `ComfyUIService` llama a localhost
- Routing: si Runpod está activo como motor principal → usa Runpod. Si falla → fallback a ComfyUI local (si está up). Si todo falla → marca Post como failed.

## Cold start y costo

- **Scale-to-zero** (default): paga $0 cuando no hay jobs. Primer request después de inactividad = ~15-25 s más (tiempo de que el worker cargue el contenedor + ComfyUI inicialice los modelos desde el volumen).
- **1 Active Worker**: ~$1.50-2/día de idle, pero **sin cold start**. Vale la pena si hacés muchos jobs espaciados.
- **Costo por render FLUX** (1024×800, 40 steps, 4090): **~$0.03-0.06**.

## Debug

Si el endpoint falla:
1. Runpod UI → tu endpoint → **Requests** → ver el request fallido → **Logs**
2. El handler escribe ComfyUI logs en `/tmp/comfyui_startup.log` dentro del container (no persistente; si el endpoint no arranca, el log se pierde al apagarse el worker — mirar el stderr del container en la UI)

## Notas importantes

- Tu Network Volume **DEBE** tener `/workspace/ComfyUI/main.py` (path esperado por el handler). Si está en otra ruta, ajustar `COMFY_DIR` en `handler.py`.
- El handler NO corre `comfyui-manager` ni nada — solo el `main.py` directo. Todos los custom nodes deben estar ya en `/workspace/ComfyUI/custom_nodes/` del volumen.
- El modelo pesado (FLUX Q5, t5xxl, etc.) carga desde el volumen. Primera vez después de cold start = ~20-30 s extra de IO.
