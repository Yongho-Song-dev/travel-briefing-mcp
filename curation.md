# 큐레이션 데이터 편집 규칙

`destinations_jp.json` 및 향후 `embassies.json` 편집 시 반드시 지킬 것.

## 스키마 (destinations_jp.json)

```json
{
  "country": "JP",
  "schema_version": 1,
  "last_reviewed": "YYYY-MM-DD",
  "cities": {
    "<city_key>": {
      "name_ko": "한글명",
      "name_local": "現地語",
      "spots": [
        {
          "name_ko": "...",
          "name_local": "...",
          "category": "culture|food|nature|shopping|onsen",
          "one_liner": "20자 내외 짧은 설명",
          "best_season": ["spring"|"summer"|"autumn"|"winter"|"all"],
          "stability": "landmark|popular",
          "status": "active|seasonal|under_renovation",
          "verified_date": "YYYY-MM-DD",
          "search_query": "카카오맵 검색용"
        }
      ]
    }
  }
}
```

## 편집 원칙

### 어떤 스팟을 넣나

- ✅ **넣기**: 수십 년 안정 랜드마크(사찰·신사·성·박물관), 대형 상권/거리, 공원·자연명소, 유명 온천지, 시장 전체
- ❌ **넣지 않기**: 개별 라멘집·카페·이자카야, 프랜차이즈 매장, 특정 호텔·료칸, 개인 갤러리

이유: 개별 가게는 폐업 리스크가 크고 유지보수 부담이 배가된다.

### stability 필드

- `landmark`: 수십 년 안정, 폐업/이전 리스크 매우 낮음 (예: 청수사, 도다이지, 오사카성)
- `popular`: 유명 상권/거리이지만 리뉴얼·재개발 가능성 있음 (예: 나카스 야타이, 아메리칸 빌리지)

`popular` 는 분기별로 verified_date 재확인 대상.

### status 필드

- `active`: 정상 운영
- `seasonal`: 특정 계절만 (예: 눈축제, 벚꽃 시즌 한정)
- `under_renovation`: 개보수/재건 중 (예: 슈리성, 홋카이도청 붉은벽돌) — 렌더링 시 자동 🚧 뱃지

### one_liner

- **20자 내외** 강제. 카톡 카드에 들어가야 함.
- 형식: `<특징>, <추가 정보>` (콤마로 두 조각)
- 예: `"교토 대표 사찰, 시내 조망"`, `"1200마리 사슴이 뛰노는 공원"`

### search_query

- 카카오맵에서 정확히 검색되는 문자열
- 한글 지명 + 도시명 조합 권장 (예: `"청수사 교토"`)
- 브랜드명 단독 금지 (동명 여러 곳 나옴)

## 갱신 워크플로

1. JSON 수정
2. `last_reviewed` 갱신
3. 개별 스팟 수정 시 해당 스팟의 `verified_date` 갱신
4. `git commit -m "curation: update <city> <spot>"` 형태
5. GitHub push → 서버는 24h 안에 자동 반영 (재배포 불필요)

## 신규 도시 추가

v1 은 일본 8개 도시로 확정. 새 도시 추가 시:

1. 도시 키는 소문자 로마자 (예: `nagoya`)
2. 카테고리 5개 (culture/food/nature/shopping/onsen) 중 도시에 없는 것은 그냥 비움 (강제 채우기 X)
3. `_render_destinations_md` 는 자동으로 빈 카테고리 스킵함

## 국가 확장 (v2)

- v2: 동남아 확장 (VN/TH/PH/SG/MY/ID)
- 국가별 별도 JSON 파일 (`destinations_vn.json` 등)
- 로더도 국가별 함수 분리 예정
