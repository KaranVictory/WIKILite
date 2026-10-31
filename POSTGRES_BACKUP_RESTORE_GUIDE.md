# PostgreSQL 백업 및 복원 안내 (v0.11)

NamuLite는 두 가지 저장 모드를 지원합니다.

- **로컬 모드:** 주 데이터는 `instance/wiki.db`의 SQLite에 저장됩니다. 소유자 메뉴의 수동 백업은 SQLite DB와 `static/uploads` 이미지를 ZIP으로 만들어 로컬 백업 폴더와 PostgreSQL 백업 테이블에 저장합니다.
- **Vercel/PostgreSQL 모드:** `DATABASE_URL` 환경 변수가 있으면 PostgreSQL/Neon이 주 데이터베이스가 됩니다. 문서·계정·수정 이력·설정·업로드 이미지를 PostgreSQL에 저장하고, ZIP 백업도 PostgreSQL의 `namulite_backups` 테이블에 보관합니다. Vercel의 로컬 파일 시스템은 임시 작업 공간으로만 씁니다.

## PostgreSQL/Neon 모드 설정

1. `DATABASE_URL` 환경 변수에 PostgreSQL/Neon의 전체 연결 문자열을 설정합니다. Vercel에서는 Neon이 제공하는 pooled 연결 문자열을 권장합니다.
2. Vercel에서는 `SECRET_KEY`도 고정된 환경 변수로 설정해야 세션이 재시작 후에도 유효합니다.
3. 앱이 최초 실행될 때 필요한 테이블을 생성합니다. 별도의 SQL 테이블을 수동으로 만들 필요는 없습니다.
4. 소유자 메뉴에서 `지금 백업하기`를 누르면 현재 PostgreSQL 데이터의 ZIP 백업을 만들고 `namulite_backups`에 저장합니다. 별도 PostgreSQL 백업 DB를 쓰려면 소유자 연결 설정에 별도 자격 증명을 저장할 수 있습니다. 연결 설정을 비워 두면 `DATABASE_URL`을 백업 저장소에도 사용합니다.

## 백업 및 복원

- PostgreSQL 모드 ZIP에는 `wiki-postgres.json`이 포함됩니다. 이 파일은 사용자·문서·수정 이력·설정·이미지 데이터를 포함할 수 있습니다.
- 소유자 메뉴에서 저장된 PostgreSQL 백업을 다운로드하거나 **복원** 버튼으로 적용할 수 있습니다.
- 이전 SQLite 버전에서 만든 ZIP(`wiki.db`와 `uploads/`)도 PostgreSQL 모드에서 복원할 수 있습니다. 이때 SQLite 데이터를 PostgreSQL로 옮기고 업로드 이미지도 PostgreSQL에 저장합니다.
- 복원은 기존 문서·계정·설정·이미지를 백업 시점으로 덮어씁니다. 복원 직후에는 세션이 초기화되므로 다시 로그인해야 합니다.
- Vercel은 요청 본문 크기를 제한하므로 업로드 ZIP은 3MB 단위 요청으로 나누어 전송됩니다. 다운로드는 스트리밍 응답을 사용합니다. Vercel 함수의 요청/응답 크기 제한은 플랫폼에 따라 바뀔 수 있으므로 실제 배포 후 큰 백업 파일로 시험하세요.
- 백업에는 비밀번호 해시와 IP 기록 등 개인정보가 포함될 수 있습니다. 백업 파일과 데이터베이스 자격 증명을 비공개로 유지하세요.

만료된 백업은 요청이 처리될 때 정리됩니다. 요청이 없는 동안 정확한 시각에 실행되는 별도 스케줄러는 아닙니다. 자세한 환경 변수 및 Vercel 배포 절차는 [VERCEL_DEPLOY_GUIDE.md](VERCEL_DEPLOY_GUIDE.md)를 참고하세요.
