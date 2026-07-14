# Travel Briefing MCP — streamable-http 서버
# 빌드:  docker build -t travel-briefing-mcp .
# 실행:  docker run -d --name travel-briefing -p 8000:8000 --env-file .env travel-briefing-mcp
FROM python:3.12-slim

# uv 바이너리만 복사 (공식 배포 이미지)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 의존성 레이어 분리 — 코드 수정 시 재설치 없이 캐시 재사용
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 애플리케이션 코드·설정 (API 키는 절대 이미지에 넣지 않음 — 실행 시 --env-file 주입)
# *.py: travel_briefing_mcp + tb_config/tb_api/tb_helpers/tb_scheduler (test 류는 .dockerignore 제외)
COPY *.py ./
COPY config/ config/
COPY data/ data/
COPY destinations_jp.json embassies.json ./

# 비루트 사용자로 실행 (컨테이너 탈취 시 피해 최소화)
RUN useradd --create-home --shell /usr/sbin/nologin app && chown -R app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    TB_HOST=0.0.0.0 \
    TB_PORT=8000

EXPOSE 8000

# MCP 엔드포인트 TCP 응답 확인 (curl 없는 slim 이미지 대응)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=2)"

CMD ["python", "travel_briefing_mcp.py"]
