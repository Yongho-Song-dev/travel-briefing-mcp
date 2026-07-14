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

# TB_PORT 미지정 시 호스팅이 주입하는 PORT 를 따르므로 여기서 TB_PORT 를 고정하지 않는다.
# (PlayMCP 콘솔에서 TB_PORT 를 넣으면 그 값이 최우선)
# 한국 사용자 대상 서비스 — 컨테이너가 UTC 면 D-day 가 하루 틀리고(00~09시 KST),
# 수출입은행 고시일(KST 기준) 조회도 하루 어긋난다. 반드시 KST 로 고정.
ENV PATH="/app/.venv/bin:$PATH" \
    TZ=Asia/Seoul \
    TB_HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# MCP 엔드포인트 TCP 응답 확인 (curl 없는 slim 이미지 대응)
# 실제 바인딩 포트(TB_PORT > PORT > 8000)를 그대로 검사해야 포트 변경 시 오탐하지 않는다
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; p=int(os.getenv('TB_PORT') or os.getenv('PORT') or 8000); socket.create_connection(('127.0.0.1', p), timeout=2)"

CMD ["python", "travel_briefing_mcp.py"]
