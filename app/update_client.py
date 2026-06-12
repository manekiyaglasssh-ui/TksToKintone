from __future__ import annotations

import os
import re
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - dependency is installed in packaged Windows builds.
    requests = None  # type: ignore[assignment]


UPDATE_APP_NAME = "TksToKintone"
UPDATE_KINTONE_DOMAIN = "manekiya.cybozu.com"
UPDATE_KINTONE_APP_ID = "250"
UPDATE_KINTONE_API_TOKEN = "foskzpcU5hS5mPZgWo86UC1rNGrzRCr6bHeKsUKg"
UPDATE_TIMEOUT_SECONDS = 60
UPDATE_DOWNLOAD_SUBDIR = Path("Manekiya") / "TksToKintone" / "updates"


@dataclass(frozen=True)
class UpdateInfo:
    version_name: str
    version_code: int
    file_key: str
    file_name: str
    file_size: int
    release_notes: str = ""


class UpdateClient:
    def check_for_update(self, current_version_code: int) -> UpdateInfo | None:
        if requests is None:
            raise RuntimeError("更新確認には requests が必要です。requirements.txt をインストールしてください。")

        response = requests.get(
            f"https://{UPDATE_KINTONE_DOMAIN}/k/v1/records.json",
            headers={"X-Cybozu-API-Token": UPDATE_KINTONE_API_TOKEN},
            params={
                "app": UPDATE_KINTONE_APP_ID,
                "query": f'アプリ名 in ("{UPDATE_APP_NAME}") order by バージョンコード desc limit 500',
            },
            timeout=UPDATE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        records = data.get("records", []) if isinstance(data, dict) else []
        candidates = [_record_to_update_info(record) for record in records if isinstance(record, dict)]
        newer = [info for info in candidates if info is not None and info.version_code > current_version_code]
        if not newer:
            return None
        return max(newer, key=lambda info: info.version_code)


def default_update_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.environ.get("TMP")
    if base:
        return Path(base) / UPDATE_DOWNLOAD_SUBDIR
    return Path.cwd() / "updates"


def launch_external_update(info: UpdateInfo, update_dir: Path, app_exe_path: Path) -> Path:
    update_dir.mkdir(parents=True, exist_ok=True)
    file_name = _safe_file_name(info.file_name) or f"TksToKintoneSetup_{info.version_code}.exe"
    if not _looks_like_installer(Path(file_name)):
        raise RuntimeError(
            "自動更新には署名済みインストーラが必要です。"
            f"配布管理には setup/installer 名のインストーラを登録してください: {file_name}"
        )
    script_path = _create_external_update_script(info, update_dir, file_name, app_exe_path)
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    return script_path


def _record_to_update_info(record: dict[str, object]) -> UpdateInfo | None:
    version_code = _parse_version_code(_field_value(record, "バージョンコード"))
    if version_code is None:
        return None
    files = _field_value(record, "APKファイル")
    if not isinstance(files, list) or not files:
        return None
    file_info = files[0]
    if not isinstance(file_info, dict):
        return None
    file_key = str(file_info.get("fileKey") or "")
    if not file_key:
        return None
    return UpdateInfo(
        version_name=str(_field_value(record, "バージョン名") or ""),
        version_code=version_code,
        file_key=file_key,
        file_name=str(file_info.get("name") or f"TksToKintone_{version_code}.exe"),
        file_size=_parse_int(file_info.get("size")) or 0,
        release_notes=str(_field_value(record, "リリースノート") or ""),
    )


def _field_value(record: dict[str, object], field_code: str) -> object:
    field = record.get(field_code)
    return field.get("value") if isinstance(field, dict) else None


def _parse_version_code(value: object) -> int | None:
    parsed = _parse_int(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _parse_int(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _safe_file_name(value: str) -> str:
    name = Path(value).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def _download_payload_name(file_name: str) -> str:
    safe_name = _safe_file_name(file_name)
    installer_stem = Path(safe_name).stem or "TksToKintoneSetup"
    return f"{installer_stem}.installer"


def _looks_like_installer(path: Path) -> bool:
    name = path.name.lower()
    return any(keyword in name for keyword in ("setup", "installer", "install"))


def _ps_single_quote(path: Path) -> str:
    return str(path).replace("'", "''")


def _create_external_update_script(info: UpdateInfo, update_dir: Path, file_name: str, app_exe_path: Path) -> Path:
    script_path = update_dir / "run_update.ps1"
    payload_path = update_dir / _download_payload_name(file_name)
    installer_path = payload_path.with_suffix(".exe")
    download_url = f"https://{UPDATE_KINTONE_DOMAIN}/k/v1/file.json"
    script_path.write_text(
        textwrap.dedent(
            f"""
            $ErrorActionPreference = "Stop"

            $downloadUrl = '{_ps_quote(download_url)}'
            $apiToken = '{_ps_quote(UPDATE_KINTONE_API_TOKEN)}'
            $fileKey = '{_ps_quote(info.file_key)}'
            $payload = '{_ps_quote(str(payload_path))}'
            $partial = "$payload.part"
            $installer = '{_ps_quote(str(installer_path))}'
            $appExe = '{_ps_quote(str(app_exe_path))}'
            $log = Join-Path -Path '{_ps_quote(str(update_dir))}' -ChildPath 'update.log'

            Start-Sleep -Seconds 2

            try {{
                "update start $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8 -Append
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $payload) | Out-Null
                Remove-Item -LiteralPath $partial, $payload, $installer -Force -ErrorAction SilentlyContinue

                $uri = $downloadUrl + '?fileKey=' + [Uri]::EscapeDataString($fileKey)
                $curl = Join-Path $env:SystemRoot 'System32\\curl.exe'
                if (Test-Path -LiteralPath $curl) {{
                    & $curl --fail --location --retry 3 --connect-timeout 20 --max-time 600 `
                        --header "X-Cybozu-API-Token: $apiToken" `
                        --output $partial `
                        $uri
                    if ($LASTEXITCODE -ne 0) {{
                        throw "curl failed: exit code $LASTEXITCODE"
                    }}
                }} else {{
                    Invoke-WebRequest -Uri $uri `
                        -Headers @{{'X-Cybozu-API-Token' = $apiToken}} `
                        -OutFile $partial `
                        -UseBasicParsing `
                        -TimeoutSec 600
                }}

                Move-Item -LiteralPath $partial -Destination $payload -Force
                Move-Item -LiteralPath $payload -Destination $installer -Force

                $p = Start-Process -FilePath $installer `
                    -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-' `
                    -PassThru -Wait

                if ($p.ExitCode -ne 0) {{
                    throw "installer failed: exit code $($p.ExitCode)"
                }}

                Start-Sleep -Seconds 2
                Start-Process -FilePath $appExe
                "update complete $(Get-Date -Format s)" | Out-File -FilePath $log -Encoding utf8 -Append
            }} catch {{
                "update failed $(Get-Date -Format s): $($_.Exception.Message)" | Out-File -FilePath $log -Encoding utf8 -Append
                throw
            }}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return script_path


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")
