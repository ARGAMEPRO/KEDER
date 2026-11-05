# syntax=docker/dockerfile:1
#
# Multi-architecture backend image — no per-machine configuration needed.
#
# BuildKit provides TARGETARCH automatically, so the same file produces:
#   * arm64 (Apple Silicon, ARM servers) -> CPU build of torch  (~1.5 GB)
#   * amd64 (x86 servers, NVIDIA boxes)  -> CUDA build of torch (uses the GPU
#                                           when one is passed into the container,
#                                           and falls back to CPU otherwise)
#
# Override with --build-arg TORCH_VARIANT=cpu|cuda when you want to force a variant
# (e.g. a CPU-only x86 server, or CI where downloading 3 GB of CUDA wheels is wasteful).
#
# NOTE ON LAYERS: roughly 1 GB of this image is our own dependencies; the rest is the
# Python/torch runtime. Deleting files that belong to a *base* image layer does not
# shrink the image — those bytes still live in the parent layer.

FROM python:3.11-slim-bookworm

ARG TARGETARCH
ARG TORCH_VERSION=2.3.1
ARG TORCHVISION_VERSION=0.18.1
ARG TORCH_VARIANT=auto

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg      - video/RTSP decoding for OpenCV
# libglib2.0-0- required by OpenCV
# libgomp1    - OpenMP runtime used by scipy/torch
# fonts-dejavu-core - Cyrillic-capable font for PDF reports
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libglib2.0-0 libgomp1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /backend
COPY requirements.txt .

# torch/torchvision first, so ultralytics installs against the right build instead
# of pulling its own.
#
# Why the three branches: torch and torchvision must come from the SAME build.
# Mixing them fails at import time with "operator torchvision::nms does not exist"
# (PyPI's x86_64 torchvision is linked against the CUDA torch, not against torch+cpu).
#   * amd64 + cuda -> both from the cu121 index;
#   * amd64 + cpu  -> both from the CPU index;
#   * aarch64      -> the CPU index ships torch but NOT torchvision for aarch64,
#                     so torchvision has to come from PyPI (where it is built
#                     against the CPU torch, because no CUDA exists there).
RUN set -eux; \
    case "${TORCH_VARIANT}" in \
        cpu)  want=cpu ;; \
        cuda) want=cuda ;; \
        auto) if [ "${TARGETARCH}" = "amd64" ]; then want=cuda; else want=cpu; fi ;; \
        *) echo "TORCH_VARIANT must be auto, cpu or cuda (got '${TORCH_VARIANT}')" >&2; exit 1 ;; \
    esac; \
    echo "==> TARGETARCH=${TARGETARCH}: installing the '${want}' torch build"; \
    if [ "${want}" = "cuda" ]; then \
        pip install "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
            --index-url https://download.pytorch.org/whl/cu121; \
    elif [ "${TARGETARCH}" = "amd64" ]; then \
        pip install "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
            --index-url https://download.pytorch.org/whl/cpu; \
    else \
        pip install "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cpu; \
        pip install "torchvision==${TORCHVISION_VERSION}"; \
    fi

# Ultralytics depends on the full `opencv-python`, while this project wants the
# headless build. Installing both leaves two distributions owning the same `cv2`
# directory, and whichever pip wrote last wins. Install once, then keep exactly
# one (headless): it is smaller and needs no GUI libraries.
RUN pip install -r requirements.txt \
    && pip uninstall -y opencv-python opencv-python-headless \
    && pip install --no-deps "opencv-python-headless~=4.10.0"

COPY . .
RUN mkdir -p /backend/data /backend/uploads/images /backend/uploads/false_positives

# Fail the build early if the vision stack cannot be imported on this architecture.
RUN python -c "import torch, torchvision, cv2, ultralytics, shapely, psycopg; \
print('torch', torch.__version__, '| cuda', torch.cuda.is_available(), '| torchvision', torchvision.__version__)"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
