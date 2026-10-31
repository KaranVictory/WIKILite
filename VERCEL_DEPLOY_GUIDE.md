# NamuLite Wiki — Vercel + Neon 배포 안내

이 버전은 Vercel의 Python/Flask 런타임을 사용합니다. Vercel에서 실행되는 함수의 파일 시스템은 영구 저장소가 아니므로, Vercel에서는 `DATABASE_URL`이 가리키는 PostgreSQL/Neon을 **주 데이터베이스**로 사용합니다. 이미지도 PostgreSQL에 보관하므로 재배포나 함수 재시작만으로 위키 문서·계정·업로드 파일이 사라지지 않습니다.

## 1. Neon 데이터베이스 준비

1. Neon에서 PostgreSQL 프로젝트를 만들거나 기존 프로젝트를 선택합니다.
2. Neon의 **Connect** 메뉴에서 PostgreSQL 연결 문자열을 복사합니다. Vercel 배포에서는 Neon이 제공하는 pooled 연결 문자열을 권장합니다.
3. 연결 문자열을 저장소 파일, 공개된 문서, 소스 코드에 직접 적지 마세요. Vercel 환경 변수에만 입력합니다. 이전 대화나 공개된 곳에 비밀번호를 붙여넣은 적이 있다면 Neon에서 비밀번호를 교체하는 편이 안전합니다.

앱은 최초 실행 때 필요한 테이블을 생성합니다. 수동으로 SQL 테이블을 만들 필요는 없습니다.

## 2. GitHub 저장소 준비

프로젝트 ZIP을 풀고 파일 전체를 GitHub 저장소의 **최상위 폴더**에 올립니다. `app.py`, `namumark.py`, `db_compat.py`, `requirements.txt`, `templates/`, `static/`, `vercel.json`이 최상위 기준으로 있어야 합니다. `DATABASE_URL`이나 실제 비밀번호가 들어 있는 `.env` 파일은 커밋하지 마세요.

## 3. Vercel 환경 변수

Vercel 대시보드에서 **Add New → Project**로 GitHub 저장소를 가져온 뒤 `Settings → Environment Variables`에 아래 값을 추가합니다. Production에 적용하고 Preview 배포에서도 시험하려면 Preview에도 추가하세요.

| 변수 | 값 |
|---|---|
| `DATABASE_URL` | Neon Connect 메뉴의 전체 PostgreSQL 연결 문자열 |
| `SECRET_KEY` | 길고 임의적인 세션 서명 키 |
| `COOKIE_SECURE` | `1` |
| `TRUST_PROXY` | `1` |
| `WIKI_NAME` | 위키 이름 (선택) |

`SECRET_KEY`는 로컬 컴퓨터의 터미널에서 다음 명령으로 만들 수 있습니다. 출력된 문자열을 Vercel 환경 변수 값으로 붙여넣습니다.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`DATABASE_URL`에는 사용자 이름, 비밀번호, 호스트, 데이터베이스 이름, 필요한 SSL 매개변수가 모두 포함되어야 합니다. 비밀번호를 코드나 GitHub에 기록하지 마세요.

## 4. 배포

Vercel의 기본 Build Command와 Output Directory는 비워 두어도 됩니다. Flask 앱은 프로젝트 루트의 `app.py`에 있는 `app` 객체로 감지됩니다. 배포가 완료되면 배포 URL을 열어 회원가입을 합니다. **첫 번째 계정이 위키 소유자이자 관리자**가 됩니다.

환경 변수를 바꾼 경우에는 새 배포를 실행해야 변경된 값이 적용됩니다.

## 5. 로컬 SQLite 백업을 가져오기

기존 NamuLite 로컬 버전에서 소유자 메뉴의 백업 ZIP을 만들고, 배포된 Vercel 위키에 첫 계정으로 로그인한 후 `소유자 설정 → 백업 파일에서 복원`으로 ZIP을 올립니다. PostgreSQL 모드의 복원은 새 형식(`wiki-postgres.json`)뿐 아니라 이전 SQLite 백업의 `wiki.db`와 `uploads/` 파일도 읽어 PostgreSQL로 가져오도록 지원합니다.

**주의:** 복원은 현재 배포 위키의 계정·문서·설정·이미지를 백업 시점으로 덮어씁니다. 새 배포에 복원하기 전에 빈 위키가 맞는지 확인하고, 중요한 데이터라면 현재 위키도 먼저 백업하세요. 백업 파일은 개인 정보가 포함될 수 있으므로 공개 저장소에 올리지 마세요.

## 6. 백업과 복원

소유자 메뉴의 `지금 백업하기`는 PostgreSQL 테이블과 업로드 이미지를 ZIP으로 내보낸 후 PostgreSQL의 `namulite_backups` 테이블에 저장합니다. Vercel의 `/tmp`는 임시 작업 공간으로만 사용합니다. 기본적으로 `DATABASE_URL`의 데이터베이스에 백업도 저장하며, 별도 백업 DB가 필요하면 소유자 설정에서 연결 정보를 지정할 수 있습니다.

백업 보관 기간은 소유자 메뉴에서 설정합니다. 만료된 PostgreSQL 백업은 방문 요청을 처리하며 정리됩니다. Vercel의 임시 파일은 재시작 후 유지되지 않으므로, 로컬 파일 목록 대신 `PostgreSQL에 저장된 백업` 목록을 기준으로 사용하세요.

## 문제 해결

- **앱이 `DATABASE_URL` 또는 `SECRET_KEY` 오류로 시작하지 않음:** Vercel 환경 변수 이름과 값, 적용할 배포 환경(Production/Preview)을 확인하고 다시 배포합니다.
- **Neon 연결 오류:** Neon에서 복사한 전체 연결 문자열을 사용하고 SSL 설정이 포함되어 있는지 확인합니다. 사용자 이름·비밀번호·DB 이름을 일부만 따로 붙여 넣지 말고 전체 URL을 사용합니다.
- **새 배포에서 기존 SQLite 데이터가 보이지 않음:** Vercel과 로컬 SQLite는 별도 저장소입니다. 위의 SQLite 백업 이전 절차로 가져와야 합니다.
- **세션이 자주 풀림:** `SECRET_KEY`를 고정된 환경 변수로 설정했는지 확인합니다. 매번 새로운 값을 만들지 마세요.
- **업로드 이미지가 표시되지 않음:** 앱이 `DATABASE_URL`로 실행 중인지 확인합니다. Vercel에서 이미지 bytes는 PostgreSQL의 `namulite_uploads` 테이블에 저장됩니다.

## 운영 전 확인 사항

Vercel은 Flask 앱을 루트의 `app.py`에서 자동 감지하고 Python 3.13을 지원합니다. 다만 함수 요청/응답 본문 제한이 4.5MB이므로, 이미지 업로드는 3MB 이하로 제한하고 복원 ZIP은 3MB 조각으로 나누어 전송하도록 구성했습니다. 자세한 내용은 [Vercel Flask 배포 안내](https://vercel.com/kb/guide/ship-a-flask-app-on-vercel)와 [함수 제한](https://vercel.com/docs/functions/limitations)을 참고하세요.

이 버전은 Vercel 호환 경로와 PostgreSQL 어댑터를 추가했지만, 여기서 실제 Vercel 배포나 사용자의 Neon 서버로 연결 테스트를 수행한 것은 아닙니다. 배포 후에는 회원가입·로그인·문서 편집·이미지 업로드·백업 다운로드·복원까지 직접 시험하세요. 백업 ZIP과 압축 해제 후 내용은 모두 190MB 이하여야 복원할 수 있습니다. 약관·개인정보 안내 문구는 공개 운영 전에 실제 운영 상황에 맞게 검토해야 합니다.
