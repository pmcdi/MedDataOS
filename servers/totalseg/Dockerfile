# TotalSegmentator as an MCP server, for MedDataOS.
#
#   docker build -t meddataos/totalseg-mcp:2.18.0 servers/totalseg
#
# CPU by default. For a GPU, build on the official TotalSegmentator image, which
# already has CUDA torch and TotalSegmentator; this adds the MCP server:
#
#   docker build --build-arg BASE=wasserth/totalsegmentator:<tag> \
#       -t meddataos/totalseg-mcp:2.18.0-gpu servers/totalseg
#
# The official image's Python must be 3.10 or newer for the mcp package.
#
# Weights are not baked in: mount the "total" weights at /weights. The server
# turns TotalSegmentator's usage statistics off, and MedDataOS runs it with no
# network.

ARG BASE=python:3.12-slim
FROM ${BASE}

ARG TOTALSEG_VERSION=2.18.0

# CPU torch from PyTorch's CPU index (the default wheel brings CUDA libraries),
# unless the base image has torch already.
RUN python -c "import torch" 2>/dev/null \
    || pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision
RUN pip install --no-cache-dir "TotalSegmentator==${TOTALSEG_VERSION}" "mcp>=2.3,<3"

COPY server.py /app/server.py

ENV TOTALSEG_WEIGHTS_PATH=/weights \
    TOTALSEG_HOME_DIR=/tmp/totalseg \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "/app/server.py"]
