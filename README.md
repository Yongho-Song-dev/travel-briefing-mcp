# Travel Briefing (트래블 브리핑)

PlayMCP에 등록하기 위한 **해외여행 준비 MCP 서버**. 일본을 중심으로 중국·대만·동남아 6개국까지 비자·환율·안전경보·시즌·관광지·맞춤 일정과 국가별 문화·현지 생활 팁을 제공한다.

**대회**: AGENTIC PLAYER 10 (본선 유저 투표: 2026-08-31 ~ 09-28)

## MCP 제공 정보

지원 국가: 일본(JP), 중국(CN), 대만(TW), 베트남(VN), 태국(TH), 필리핀(PH), 싱가포르(SG), 말레이시아(MY), 인도네시아(ID).

- **출발 전 준비**: 비자, 전압/플러그, 시차, 통화, 긴급번호, 대사관 연락처
- **현지 문화·생활 팁**: 국가별 예절, 소지품, 교통, 결제, 음식·물, 날씨 대응 요령
- **현재 안전 상황**: 외교부 여행경보 단계와 발효일
- **환율**: 한국수출입은행 매매기준율
- **항공 시즌 가이드**: 성수기/비수기 판정과 스카이스캐너 비교 링크
- **관광지 큐레이션**: 8개 도시(도쿄·오사카·교토·후쿠오카·삿포로·오키나와·히로시마·나라) × 5개 카테고리(문화·먹거리·자연·쇼핑·온천)
- **D-day 체크리스트**: 위 정보를 종합한 단톡방 붙여넣기용 카드
- **맞춤 일정 추천**: 일정·예산·인원·목적과 정제된 여행 후기 기반 추천

## 아키텍처 원칙

**얇은 서버**: 추론은 호스트 LLM(Kakao Tools)이 담당하고, 이 서버는 결정론적 조회·계산·렌더링만 처리한다. 서버 내부에서 LLM을 돌리지 않아 운영 비용이 낮다.

**데이터 변동성 3계층 관리**:

| 변동성 | 처리 방식 | 대상 |
|---|---|---|
| 거의 불변 | 국가별 JSON에서 인메모리 로드 | 전압, 시차, 통화, 긴급번호, 공항코드, 시즌규칙, 문화·생활 팁 |
| 가끔 바뀜 | GitHub Raw JSON, 24시간 주기 갱신 | 대사관 연락처, 관광지 큐레이션 |
| 자주 바뀜 | 외부 API + TTL 캐시 | 비자(13h), 여행경보(1h), 환율(24h), 블로그 검색(7h) |


## MCP 도구 7개

| 도구 | 역할 |
|---|---|
| `get_trip_briefing` | 비자·전압·시차·통화·긴급번호·대사관·상세 현지 팁 (비자는 MOFA 동적, 실패 시 정적 폴백) |
| `get_current_status` | 외교부 여행경보 단계·발효일과 간결한 현지 행동 요령 |
| `get_exchange_rate` | 원화 매매기준율 |
| `get_flight_season_guide` | 시즌 판정과 스카이스캐너 비교 링크 |
| `get_destinations` | 도시·카테고리별 관광지 큐레이션 |
| `compose_checklist` | 위 도구들을 종합한 D-day 체크리스트 카드 |
| `recommend_itinerary` | 일정·예산·목적별 맞춤 여행 추천 |

## 프로젝트 구조

```
travel-briefing-mcp/
├── README.md                    # 이 파일
├── travel_briefing_mcp.py       # MCP 서버 본체
├── tb_config.py                 # 설정·국가 데이터 로더
├── tb_api.py                    # 외부 API·TTL 캐시
├── tb_helpers.py                # 쿼리·마크다운 렌더러
├── tb_scheduler.py              # 캐시 워밍 스케줄러
├── destinations_jp.json         # 일본 관광지 큐레이션 데이터
├── embassies.json               # 대사관 연락처 스냅샷
├── config/                      # API·TTL·어휘 설정
├── data/                        # 9개국 정적 데이터
├── test_playmcp_compliance.py   # 등록 가이드 자동 검증
├── pyproject.toml / uv.lock     # Python 의존성
├── Dockerfile                   # 배포 이미지
├── .env.example                 # 환경변수 템플릿
├── .gitignore
└── .claude/
    └── rules/
        ├── playmcp-guide.md     # PlayMCP 등록 가이드 요약
        ├── mcp-schema.md        # 외부 API 스키마와 응답 매핑
        └── curation.md          # 큐레이션 데이터 편집 규칙
```

## 시작하기

### 1. 환경 준비

```bash
git clone <repo-url>
cd travel-briefing-mcp
uv sync --frozen
cp .env.example .env
```

### 2. API 키 발급

다음 API 키를 `.env`에 설정한다. 키가 없거나 외부 API가 실패하면 가능한 범위에서 정적 정보 또는 안내 문구로 폴백한다.

- 외교부 (공공데이터포털): 입국허가요건 + 여행경보 (인증키 공유)
- 한국수출입은행: 환율
- 네이버 검색 API: 맞춤 일정의 여행 후기 검색

발급받은 키를 `.env` 에 채운다.

### 3. 로컬 실행

```bash
uv run python travel_briefing_mcp.py
```

`http://127.0.0.1:8000/mcp`에서 stateless Streamable HTTP 서버가 기동된다. stdio와 SSE 전송은 제공하지 않는다.

### 4. MCP Inspector 검증

배포 전 PlayMCP 가이드 준수 여부를 검증한다.

먼저 서버를 실행한 뒤 다른 터미널에서 `npx @modelcontextprotocol/inspector`를 실행한다. Inspector에서 Transport를 `Streamable HTTP`, URL을 `http://127.0.0.1:8000/mcp`로 지정하고 `Initialize`와 `List Tools`를 확인한다.

저장소에 포함된 자동 점검도 실행한다.

```bash
uv run python -m unittest -v test_playmcp_compliance.py
```

## 개발 규칙

프로젝트에 코드를 추가·수정할 때는 아래를 준수한다.

- 서버명·툴명에 "kakao" 를 어떤 형태로도 넣지 않는다 (등록 반려 사유)
- 툴 개수는 7개를 유지한다 (가이드 권장 3~10 범위)
- 툴 `annotations` 5종(`title`, `readOnlyHint`, `destructiveHint`, `openWorldHint`, `idempotentHint`)을 전부 지정한다
- 툴 description 은 영문으로 작성하고 "Travel Briefing(트래블 브리핑)" 을 병기한다
- 응답은 정제된 마크다운으로 반환하고, 외부 API raw JSON 을 그대로 돌려주지 않는다
- 외부 API 호출은 반드시 TTL 캐시를 통한다 (p99 3,000ms 요건)
- 서버 내부에서 LLM 을 실행하지 않는다
- 응답에 광고 유도를 넣지 않는다

세부 규칙은 `.claude/rules/` 의 개별 파일을 참조한다.

## 배포

카카오 클라우드에 컨테이너로 배포하며, 공개 HTTPS 도메인이 필요하다.

환경변수는 `.env` 파일 대신 **카카오 클라우드의 환경변수/시크릿 매니저**로 주입한다. Docker 이미지에 `.env` 를 포함시키지 않는다.

배포 후 PlayMCP 개발자 콘솔에서 등록하고 심사를 받는다.

## 라이선스와 데이터 출처

- 외교부 여행경보/입국허가요건: 공공저작물 출처표시 (제1유형)
- 한국수출입은행 환율: 이용허락범위 제한 없음
- 관광지 큐레이션·국가별 문화 및 생활 팁: 프로젝트 자체 편찬 (외교부·각국 정부/관광청 안내 교차 검토, 수정·기여 환영)

## 로드맵

- **v1**: 일본 중심 여행 준비, 도구 6개
- **v2** (현재): 중국·대만·동남아 확장과 맞춤 일정 추천, 도구 7개
- **v3**: 환율 30일 추이, 도시별 이벤트 정보

## 기여

관광지 큐레이션 수정·추가는 대상 국가의 `curation/destinations_{국가코드}.json` 을 편집해 PR 을 올린다. 일본 파일은 외부 JSON 갱신 주기에 따라 반영되며, 그 외 국가의 번들 큐레이션은 재배포 시 반영된다.
