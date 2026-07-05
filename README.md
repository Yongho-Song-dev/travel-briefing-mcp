# Travel Briefing (트래블 브리핑)

카카오 PlayMCP 에 등록하기 위한 **일본 여행 준비 MCP 서버**. Kakao Tools 사용자가 카카오톡 대화창에서 "다음주 도쿄 가는데 뭐 준비해야 해?" 같은 질문을 하면 비자·환율·안전경보·시즌·관광지를 즉시 답한다.

**대회**: AGENTIC PLAYER 10 (본선 유저 투표: 2026-08-31 ~ 09-28)

## MCP 제공 정보

한국인이 가장 많이 가는 해외여행지인 일본에 특화해서, 여행 준비에 필요한 정보를 한 번에 제공한다.

- **출발 전 준비**: 비자, 전압/플러그, 시차, 통화, 긴급번호, 대사관 연락처
- **현재 안전 상황**: 외교부 여행경보 단계와 발효일
- **환율**: 한국수출입은행 매매기준율
- **항공 시즌 가이드**: 성수기/비수기 판정과 스카이스캐너 비교 링크
- **관광지 큐레이션**: 8개 도시(도쿄·오사카·교토·후쿠오카·삿포로·오키나와·히로시마·나라) × 5개 카테고리(문화·먹거리·자연·쇼핑·온천)
- **D-day 체크리스트**: 위 정보를 종합한 단톡방 붙여넣기용 카드

## 아키텍처 원칙

**얇은 서버**: 추론은 호스트 LLM(Kakao Tools)이 담당하고, 이 서버는 결정론적 조회·계산·렌더링만 처리한다. 서버 내부에서 LLM을 돌리지 않아 운영 비용이 낮다.

**데이터 변동성 3계층 관리**:

| 변동성 | 처리 방식 | 대상 |
|---|---|---|
| 거의 불변 | 코드 내 인메모리 | 전압, 시차, 통화, 긴급번호, 공항코드, 시즌규칙 |
| 가끔 바뀜 | GitHub Raw JSON, 24시간 주기 갱신 | 대사관 연락처, 관광지 큐레이션 |
| 자주 바뀜 | 외부 API + TTL 캐시 | 비자(6h), 여행경보(1h), 환율(24h) |


## MCP 도구 6개

| 도구 | 역할 |
|---|---|
| `get_trip_briefing` | 비자·전압·시차·통화·긴급번호·대사관 정보 (비자는 MOFA 동적, 실패 시 정적 폴백) |
| `get_current_status` | 외교부 여행경보 단계와 발효일 |
| `get_exchange_rate` | 원화 매매기준율 |
| `get_flight_season_guide` | 시즌 판정과 스카이스캐너 비교 링크 |
| `get_destinations` | 도시·카테고리별 관광지 큐레이션 |
| `compose_checklist` | 위 도구들을 종합한 D-day 체크리스트 카드 |

## 프로젝트 구조

```
travel-briefing-mcp/
├── README.md                    # 이 파일
├── CLAUDE.md                    # Claude Code용 프로젝트 헌법
├── STATUS.md                    # 라이브 진행 상황과 결정 기록
├── API_KEYS.md                  # 외부 API 키 발급 가이드
├── travel_briefing_mcp.py       # MCP 서버 본체
├── destinations_jp.json         # 일본 관광지 큐레이션 데이터
├── embassies.json               # 대사관 연락처 (작성 예정)
├── requirements.txt             # Python 의존성
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
pip install -r requirements.txt
cp .env.example .env
```

### 2. API 키 발급

`API_KEYS.md` 참조. 2개 필요:

- 외교부 (공공데이터포털): 입국허가요건 + 여행경보 (인증키 공유)
- 한국수출입은행: 환율

발급받은 키를 `.env` 에 채운다.

### 3. 로컬 실행

```bash
python travel_briefing_mcp.py
```

기본 포트로 Streamable HTTP 서버가 기동된다.

### 4. MCP Inspector 검증

배포 전 PlayMCP 가이드 준수 여부를 검증한다.

```bash
npx @modelcontextprotocol/inspector python travel_briefing_mcp.py
```

## 개발 규칙

프로젝트에 코드를 추가·수정할 때는 아래를 준수한다.

- 서버명·툴명에 "kakao" 를 어떤 형태로도 넣지 않는다 (등록 반려 사유)
- 툴 개수는 6개를 유지한다 (가이드 권장 3~10 범위)
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
- 관광지 큐레이션: 프로젝트 자체 편찬 (수정·기여 환영)

## 로드맵

**v1** (진행 중): 일본 단일 국가, 도구 6개
**v2**: 동남아 확장 (베트남·태국·필리핀·싱가포르·말레이시아·인도네시아)
**v3**: 환율 30일 추이, 도시별 이벤트 정보

## 기여

관광지 큐레이션 수정·추가는 `destinations_jp.json` 을 편집해 PR 을 올린다. 편집 규칙은 `.claude/rules/curation.md` 참조. 서버는 24시간 안에 변경사항을 자동 반영한다.
