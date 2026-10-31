## v0.16
- 문서 삭제 전에 편집 이력을 별도 보관해 삭제 후에도 문서 역사에서 확인할 수 있도록 변경
- 문서 삭제자와 삭제 시각을 별도 이벤트로 저장하고 최근 변경 목록에 `삭제` 항목으로 표시
- 삭제된 문서의 역사 페이지를 열 수 있으며, 삭제 후 재생성된 문서도 이전 삭제·보존 이력을 함께 표시
- 이전 버전에서 남긴 삭제 표식(tombstone)을 최초 실행 시 삭제 이벤트로 복원 (이미 cascade 삭제된 과거 편집 본문은 복구 불가)
- 사이드바의 `임의 문서`를 `랜덤 문서`로 변경

## v0.15
- 사용자 관리 표에서 각 계정의 차단 동작 바로 아래에 경고 버튼을 배치하고, 별도 경고 발행 영역을 제거했습니다.
- 중첩된 `{{{...}}}` 구문을 균형 있게 파싱하도록 수정했습니다.
- `{{{#밝은색,#어두운색 내용}}}` 문법을 추가해 라이트/다크 테마별 글자색을 지원합니다.
- 편집 미리보기 내부에서만 밝은 모드와 검은 모드를 전환할 수 있도록 했습니다. 실제 사이트 테마는 변경되지 않습니다.

# 변경 기록

## v0.14 — 테마 전환·로그인 경고 팝업·사용자 관리 통합
- 로그인 여부와 관계없이 사용할 수 있는 라이트/다크 모드 전환 버튼 추가. 브라우저에 선택값을 저장해 다음 방문에도 유지합니다.
- 기존 모든 페이지 상단 경고 배너를 제거하고, 미확인 경고가 있는 사용자는 로그인 직후 한 번 팝업으로 내용을 확인하도록 변경했습니다. 닫기만 하면 경고는 미확인 상태로 남아 다음 로그인 때 다시 나타납니다.
- 관리자 메뉴의 별도 경고 관리 탭을 제거하고 사용자 관리에 경고 발행 UI와 사용자별 경고 총 횟수 및 최근 내역을 통합했습니다. 과거 `?tab=warnings` 주소도 사용자 관리로 연결합니다.
- 사용자별 계정 차단 입력 UI를 접이식 버튼으로 바꿔 목록 화면의 높이를 줄였습니다. 차단 실행과 기존 차단 해제 흐름은 유지합니다.
- 기존 경고 데이터와 사용자 계정 데이터의 DB 구조 변경은 없습니다.

## v0.13 — 경고·가입 제한·하위 문서 개선
- 관리자 경고 발행과 사용자 경고함/확인 기능 추가
- IP 주소당 최대 5개 계정 가입 제한 및 누적 카운터 마이그레이션 추가
- 개인 문서 하위 문서 생성·편집 금지
- 하위 문서 목록을 접힌 `하위 문서 찾기` 메뉴로 바꾸고 모든 단계의 후손 문서 표시
- 신규 테이블을 PostgreSQL 백업과 복원 범위에 포함

## v0.12 — 한글 문서 제목 URL 인코딩 복구

- URL에 `%EB...` 형태로 들어온 문서 제목을 디코딩해 화면과 편집 기능에서 한글 제목으로 사용합니다.
- 이전 배포에서 URL 인코딩 문자열 자체로 저장된 문서를 찾으면, 기존 페이지 ID와 수정 이력을 유지하면서 제목을 한글로 자동 정리합니다.
- 문서 보기, 편집, 삭제, ACL, 역사, 토론 경로에 동일한 제목 정규화를 적용합니다.

## v0.11 - Vercel + PostgreSQL persistent mode

- Added Vercel configuration for the Python/Flask app, including Python 3.13 and explicit template/static file inclusion.
- Added a PostgreSQL compatibility adapter so the existing wiki schema and queries can use `DATABASE_URL` as the primary database while local development continues to use SQLite by default.
- Stored Vercel image uploads and site logos in PostgreSQL `namulite_uploads` instead of the deployment filesystem.
- Added PostgreSQL-native ZIP backups containing a JSON table snapshot, including uploaded image bytes, with restore support.
- Added an import path for prior SQLite-based NamuLite ZIP backups so they can be restored into a `DATABASE_URL` PostgreSQL deployment.
- Added chunked backup ZIP upload to work around Vercel Function's 4.5 MB request-body limit; streamed PostgreSQL backup downloads.
- Required a stable `SECRET_KEY` and a primary `DATABASE_URL` for Vercel mode to avoid ephemeral sessions or SQLite data loss.
- Added a Vercel deployment guide and focused tests for PostgreSQL SQL translation.
- Verified Python compilation, 7 database-adapter unit tests, and 15 NamuMark regression tests. A live Vercel/Neon deployment test was not possible in the development environment.


## v0.8.1 — 하위 문서 버튼 표시 개선

- 일반 문서에서 하위 문서가 아직 없어도 하위 문서 영역을 항상 표시합니다.
- 비로그인 사용자에게는 로그인 후 생성 화면으로 이어지는 버튼을 표시합니다.
- 로그인했지만 편집 ACL이 허용하지 않는 경우에는 생성할 수 없는 이유를 표시합니다.
- 기존의 서버 측 로그인·ACL 검사는 그대로 유지합니다.


## v0.7 — 사용자 문서 및 회원가입 IP 로그

- 새 계정마다 `사용자:계정명` 문서 공간을 자동으로 준비합니다. 기존 계정도 업그레이드 시 문서 항목이 자동 생성됩니다.
- 사용자 본인과 관리자만 해당 사용자의 사용자 문서 및 `사용자:계정명/하위문서`를 편집할 수 있습니다. 문서는 기본적으로 공개 열람되지만 읽기 ACL을 적용할 수 있습니다.
- 헤더에 내 사용자 문서 바로가기를 추가하고, 비어 있는 사용자 문서에는 작성 안내와 하위 문서 생성 기능을 표시합니다.
- 회원가입 성공 시 가입 IP·계정명·시각을 `registration_logs`에 별도 기록하고, 관리자 전용 `회원가입 IP 기록` 탭을 추가합니다. 이전 계정은 기존 `registered_ip`가 남아 있는 경우 최근 로그 화면으로 복원합니다.
- 회원가입 감사 로그는 최대 180일 보관하며, 앱 요청 처리 시 하루에 한 번 만료 기록을 정리합니다. 사용자 관리용 `registered_ip`는 기존 차단 시스템을 위해 계정 정보에 계속 저장됩니다.
- 임의 문서 기능은 실제 편집 기록이 있는 문서 중 읽기 권한이 있는 문서만 무작위 선택하도록 바꿔, 아직 비어 있는 사용자 문서에 랜덤 이동하지 않도록 했습니다.

## v0.6 — 검색에서 문서 생성 유도

- 사이드바의 `문서 만들기` 항목을 제거했습니다.
- 검색어와 일치하는 문서가 실제로 없으면 `해당 문서는 없습니다. 그 문서를 만드실 건가요?` 안내와 `문서 만들기` 버튼을 표시합니다.
- ACL 때문에 열람할 수 없는 검색 결과가 있을 때는 새 문서 생성 프롬프트를 잘못 표시하지 않도록 했습니다.
- 직접 존재하지 않는 문서로 이동했을 때에도 같은 안내 문구를 사용합니다.

## v0.5 — IP 로그인 기록 및 약관 동의

- 성공한 로그인 이벤트마다 IP 주소와 시각을 기록하고, 관리자 전용 `로그인 IP 기록` 탭을 추가했습니다.
- 로그인 이벤트 기록은 180일 보관하며 만료 기록을 하루에 한 번 정리합니다. 실패한 로그인 시도의 IP는 별도 기록하지 않습니다.
- 회원가입 때 이용약관과 개인정보 처리 안내에 각각 동의해야 하며, 동의 시각과 버전을 계정에 저장합니다.
- `/terms`, `/privacy` 안내 페이지를 추가하고 로그인 IP 수집 목적·보관기간·관리자 열람 범위를 설명합니다.
- 문서 저장 시 CC-BY-NC-SA 2.0 KR 기여 라이선스 및 생성형 AI 정책 동의를 요구하며, 각 편집 기록에 동의 시각과 정책 버전을 남깁니다.
- 미리 보기와 이미지 업로드는 문서 저장 동의를 요구하지 않도록 분리했습니다.
- 기존 SQLite 데이터베이스에 동의 기록 및 로그인 로그용 열/테이블을 자동 추가합니다.

## v0.4 — ACL 및 차단 관리

- 읽기·편집·토론에 대한 전역 기본 ACL 설정을 추가했습니다.
- 문서마다 ACL을 재정의하거나 기본 설정을 상속할 수 있습니다.
- 허용 기준: 누구나, 로그인 사용자, 가입 후 경과 일수, 문서 기여자(문서별), 관리자만.
- 계정 가입 IP와 마지막 접속 IP를 기록하고, 계정 차단 시 확인된 두 IP에 같은 기간의 차단을 자동 추가합니다.
- 수동 IPv4/IPv6 차단, 차단 기간, 사유, 만료와 해제 이력을 관리자 탭에서 관리할 수 있습니다.
- 기존 SQLite 데이터베이스에 필요한 IP 열을 자동 추가합니다.
- 검색 결과·최근 변경·분류·역사·변경 비교에서도 읽기 ACL을 확인합니다.
- 새 환경변수 `TRUST_PROXY`를 추가했으며, 프록시 신뢰 설정과 공유 IP 주의사항을 문서화했습니다.

## 제한 사항

- 모바일/신원 인증 기반 ACL 및 CIDR 대역 차단은 구현하지 않았습니다.
- 계정 차단은 편집·토론·가입을 막고, 읽기는 별도 읽기 ACL에 따릅니다.
- IP 주소는 개인 정보가 될 수 있으므로 관리자만 열람할 수 있습니다. 차단 사유와 기간을 신중하게 설정하세요.


## v0.8 — 계층형 하위 문서

- 일반 문서에서 `DDS/DDS의 장비` 형태의 하위 문서를 생성할 수 있도록 추가
- 문서 화면에 읽을 수 있는 상위 문서 경로와 직접 하위 문서 목록 표시
- 하위 문서를 만들 때 상위 문서 읽기/편집 ACL을 검사하고, 하위 문서 자체는 기존 편집 ACL 및 저장 동의 절차 적용


## v0.8.2
- 상단 사이트 로고와 사이드바의 `대문` 버튼이 `/w/대문` 문서 경로로 이동하도록 변경
- 오류 화면의 `대문으로` 버튼도 같은 경로를 사용하도록 변경


## v0.9.0 — 소유자·문서 삭제·백업

- `is_owner` 역할 추가. 첫 가입 계정을 소유자이자 관리자로 지정하고, 기존 버전은 가장 먼저 생성된 관리자 계정을 소유자로 승격하는 마이그레이션을 추가했습니다.
- 소유자 전용 `/owner` 설정 화면을 추가했습니다. PostgreSQL 접속 설정, 비밀번호 암호화 저장, 연결 테스트(`SELECT 1`), SSL 모드 선택 기능이 있습니다.
- 소유자가 보관 기간을 1~3650일로 설정하고 수동 백업을 만들 수 있도록 했습니다. 백업 ZIP에는 SQLite 스냅샷과 업로드 이미지가 포함되고, 만료 백업은 하루 한 번 자동 정리합니다.
- 관리자/소유자가 문서 화면에서 문서를 삭제하도록 했습니다. 문서 역사·토론·ACL은 함께 삭제되며 계층형 하위 문서는 별도 문서로 유지됩니다. 삭제 표시는 사용자 문서가 앱 재시작 시 자동 재생성되지 않게 보존합니다.
- 사용자 관리에서 소유자 지정/해제를 소유자만 할 수 있도록 했으며, 마지막 소유자를 보호합니다.
- **명시적 제한**: PostgreSQL 설정은 현재 연결 확인용이며, 기본 위키 저장소를 PostgreSQL로 전환하지는 않습니다. 실제 저장소 전환은 별도 마이그레이션 작업입니다.
- 상세 설명: `OWNER_AND_BACKUP_GUIDE.md`

## v0.10 - PostgreSQL backup storage and restore
- Manual ZIP backups are now also stored in PostgreSQL `namulite_backups` (`BYTEA`) when the owner-configured connection is available.
- Owner page lists PostgreSQL backups and offers download and restore actions.
- Added owner-only restore from an uploaded NamuLite ZIP backup, with archive path checks and SQLite integrity/schema validation before replacement.
- Retention cleanup applies to local ZIP backups and PostgreSQL backup records.
- PostgreSQL remains a backup store only; live wiki data stays in SQLite.

