FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /source
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts
COPY smoke /smoke
ENV UV_CACHE_DIR=/opt/uv-cache UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
# Warm the complete real resolver cache while building. Test runs have no network.
RUN python -c 'import pathlib,tomllib; p=tomllib.loads(pathlib.Path("uv.lock").read_text())["package"]; pathlib.Path("/opt/constraints.txt").write_text("\n".join(x["name"]+"=="+x["version"] for x in p if x.get("source",{}).get("registry")))' && \
    uv build --wheel --out-dir /opt/wheels && \
    for wheel in /opt/wheels/*.whl; do \
      UV_TOOL_DIR=/opt/prewarm/tools UV_TOOL_BIN_DIR=/opt/prewarm/bin \
      uv tool install --python /usr/local/bin/python --constraint /opt/constraints.txt "$wheel[slack,telegram,web]"; \
    done
ENV UV_OFFLINE=1
CMD ["python", "/smoke/run.py"]
